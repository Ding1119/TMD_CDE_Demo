"""Quality Assurance Agent: check completeness, consistency and provenance (slide 7, agent 5)."""
from __future__ import annotations

import re

from sqlalchemy import select

from ..models import CDE, DATA_TYPES, DOMAINS, QAIssue
from ..services.consensus import delphi_summary_for
from .base import Agent, AgentContext, AgentResult
from .heuristics import name_similarity, numeric_tokens

VAR_RE = re.compile(r"^[a-z][a-z0-9_]*$")
LICENSED = re.compile(r"\b(bpi|pcs|promis)\b", re.I)


def check_cde(cde: CDE) -> list[tuple[str, str, str]]:
    """Return (severity, rule, message) for each failed check, plus the number of checks run."""
    issues: list[tuple[str, str, str]] = []

    def fail(sev: str, rule: str, msg: str) -> None:
        issues.append((sev, rule, msg))

    if not cde.definition:
        fail("error", "definition.missing", "Definition is missing.")
    elif len(cde.definition) < 25:
        fail("warning", "definition.short", "Definition is very short; make it unambiguous.")
    if cde.data_type not in DATA_TYPES:
        fail("error", "datatype.invalid", f"Unknown data type '{cde.data_type}'.")
    if cde.domain not in DOMAINS:
        fail("warning", "domain.invalid", f"Domain '{cde.domain}' is not in the framework domain list.")
    pvs = cde.permissible_values
    if cde.data_type in {"choice", "multi_choice", "boolean"} and len(pvs) < 2:
        fail("error", "pv.missing", "Categorical element needs at least two permissible values.")
    codes = [p.code for p in pvs]
    if len(codes) != len(set(codes)):
        fail("error", "pv.duplicate", "Permissible value codes are not unique.")
    if cde.data_type in {"integer", "float"}:
        if cde.min_value is None or cde.max_value is None:
            fail("warning", "range.missing", "Numeric element has no min/max range.")
        elif cde.min_value >= cde.max_value:
            fail("error", "range.invalid", "Minimum must be smaller than maximum.")
        if cde.expected_source in {"exam", "biomarker"} and not cde.unit:
            fail("warning", "unit.missing", "Measured element has no unit of measure.")
    if not VAR_RE.match(cde.variable_name or ""):
        fail("error", "variable.pattern", "Variable name must start with a letter and use a-z, 0-9, _ only.")
    elif len(cde.variable_name) > 26:
        fail("warning", "variable.length", "Variable name longer than 26 characters (REDCap recommendation).")
    if not cde.provenance:
        fail("error", "provenance.missing", "No source provenance recorded.")
    real = [m for m in cde.mappings if m.relation != "gap" and m.status != "rejected"]
    gaps = [m for m in cde.mappings if m.relation == "gap"]
    value_level = any(p.concept_code for p in pvs)
    if not real and not value_level and not gaps:
        fail("warning", "terminology.none", "No terminology mapping; map to a standard code or document a gap.")
    for g in gaps:
        fail("info", "terminology.gap", f"Recorded gap: {g.display}.")
    if cde.ehr_capturability is None or cde.expected_source is None:
        fail("warning", "ehr.source_missing", "Expected data source / EHR capturability not set.")
    targets = {i.target: i for i in cde.implementations}
    if cde.ehr_capturability == "structured" and not (
        ("OMOP" in targets and (targets["OMOP"].value_set or targets["OMOP"].logic == "Demographic field"))
    ):
        fail("warning", "ehr.value_set_missing", "Structured-EHR element has no OMOP binding with a value set.")
    if cde.ehr_capturability == "note_nlp" and not ("NLP" in targets and targets["NLP"].nlp_terms):
        fail("warning", "ehr.nlp_missing", "Note-based element has no NLP lexicon yet.")
    if LICENSED.search(cde.instrument or "") and not cde.copyright_note:
        fail("info", "copyright.note", "Instrument may be licensed; add a copyright/licensing note.")
    if cde.expected_source == "pro" and not cde.question_text:
        fail("info", "question.missing", "No question text (import item-level CDEs from the official CRF if needed).")
    if cde.status == "approved":
        s = delphi_summary_for(cde)
        if s["decision"] != "include":
            fail("warning", "delphi.no_consensus", "Approved without Delphi consensus to include.")
    return issues


TOTAL_CHECKS = 16


class QualityAssuranceAgent(Agent):
    name = "quality_assurance"
    title = "Quality Assurance Agent"
    description = "Rule checks for completeness, consistency, provenance, terminology gaps and EHR readiness; scores every CDE."

    def run(self, ctx: AgentContext) -> AgentResult:
        db = ctx.db
        cdes = db.scalars(select(CDE).where(CDE.status != "retired")).all()
        counts = {"error": 0, "warning": 0, "info": 0}
        for cde in cdes:
            for old in [q for q in cde.qa_issues if not q.resolved]:
                cde.qa_issues.remove(old)
            issues = check_cde(cde)
            for sev, rule, msg in issues:
                db.add(QAIssue(cde=cde, run_id=ctx.run.id, severity=sev, rule=rule, message=msg))
                counts[sev] += 1
            penalties = sum(1.0 if s == "error" else 0.5 if s == "warning" else 0.0 for s, _, _ in issues)
            cde.qa_score = round(max(0.0, 1 - penalties / TOTAL_CHECKS), 3)
        # consistency across CDEs: probable duplicates the harmonizer kept apart
        dupes = 0
        for i, a in enumerate(cdes):
            for b in cdes[i + 1:]:
                if numeric_tokens(a.name) == numeric_tokens(b.name) and name_similarity(a.name, b.name) >= 0.9:
                    for x, y in ((a, b), (b, a)):
                        db.add(QAIssue(cde=x, run_id=ctx.run.id, severity="warning", rule="duplicate.suspect",
                                       message=f"Possible duplicate of {y.cde_code} ({y.name})."))
                    dupes += 1
                    counts["warning"] += 2
        db.commit()
        ctx.log(self.name, f"Checked {len(cdes)} CDEs: {counts['error']} errors, {counts['warning']} warnings, "
                           f"{counts['info']} notes; {dupes} suspected duplicate pairs", **counts)
        return AgentResult(self.name, {**counts, "cdes": len(cdes), "duplicate_pairs": dupes})
