"""Exports: JSON (both layers), flat CSV, REDCap data dictionary, OMOP concept-set CSV + SQL,
NLP lexicon, phenotype, and the Aim 3 coverage report."""
from __future__ import annotations

from typing import Optional

import csv
import io
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CDE, Phenotype, QAIssue
from .consensus import delphi_summary_for
from .versioning import snapshot


def _cdes(db: Session, status: Optional[str] = None) -> list[CDE]:
    q = select(CDE).where(CDE.status != "retired").order_by(CDE.cde_code)
    if status:
        q = q.where(CDE.status == status)
    return db.scalars(q).all()


def cde_full(cde: CDE) -> dict:
    return {
        **snapshot(cde),
        "provenance": [{"source": p.source_key, "citation": p.citation, "uri": p.uri} for p in cde.provenance],
        "implementation": [
            {"target": i.target, "table": i.table_name, "field": i.field_name, "value_set": i.value_set,
             "nlp_terms": i.nlp_terms, "logic": i.logic, "status": i.status}
            for i in cde.implementations
        ],
        "qa_score": cde.qa_score,
        "delphi": delphi_summary_for(cde),
    }


def export_json(db: Session, status: Optional[str] = None) -> dict:
    return {"framework": "TMD CDE Framework", "layers": ["definition", "implementation"],
            "cdes": [cde_full(c) for c in _cdes(db, status)]}


def export_csv(db: Session, status: Optional[str] = None) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["cde_code", "variable_name", "name", "definition", "data_type", "unit", "min", "max", "permissible_values",
                "instrument", "domain", "tier", "expected_source", "ehr_capturability", "status", "version",
                "mappings", "sources"])
    for c in _cdes(db, status):
        w.writerow([c.cde_code, c.variable_name, c.name, c.definition, c.data_type, c.unit, c.min_value, c.max_value,
                    " | ".join(f"{p.code}={p.label}" for p in c.permissible_values), c.instrument, c.domain, c.tier,
                    c.expected_source, c.ehr_capturability, c.status, c.version,
                    "; ".join(f"{m.system}:{m.code}" for m in c.mappings if m.code),
                    "; ".join(sorted({p.source_key for p in c.provenance}))])
    return buf.getvalue()


REDCAP_HEADER = [
    "Variable / Field Name", "Form Name", "Section Header", "Field Type", "Field Label",
    "Choices, Calculations, OR Slider Labels", "Field Note", "Text Validation Type OR Show Slider Number",
    "Text Validation Min", "Text Validation Max", "Identifier?", "Branching Logic (Show field only if...)",
    "Required Field?", "Custom Alignment", "Question Number (surveys only)", "Matrix Group Name", "Matrix Ranking?",
    "Field Annotation",
]


def _fmt_num(v: Optional[float]) -> str:
    if v is None:
        return ""
    return str(int(v)) if float(v).is_integer() else str(v)


def export_redcap(db: Session, status: Optional[str] = None, tier: Optional[str] = None) -> str:
    """REDCap data dictionary (one form per domain) for prospective data collection."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(REDCAP_HEADER)
    w.writerow(["record_id", "tmd_cde_core", "", "text", "Record ID", "", "", "", "", "", "", "", "", "", "", "", "", ""])
    cdes = [c for c in _cdes(db, status) if not tier or c.tier == tier]
    cdes.sort(key=lambda c: (c.tier != "core", c.domain, c.cde_code))
    last_section = None
    for c in cdes:
        form = ("tmd_cde_core" if c.tier == "core" else "tmd_cde_" + c.domain.lower().replace(" & ", "_")
                .replace(" ", "_"))[:50]
        section = c.domain if (form, c.domain) != last_section else ""
        last_section = (form, c.domain)
        ftype, choices, validation = "text", "", ""
        if c.data_type == "choice":
            ftype = "radio"
        elif c.data_type == "multi_choice":
            ftype = "checkbox"
        elif c.data_type == "boolean":
            ftype = "yesno" if {p.code for p in c.permissible_values} <= {"0", "1"} else "radio"
        elif c.data_type == "integer":
            validation = "integer"
        elif c.data_type == "float":
            validation = "number"
        elif c.data_type == "date":
            validation = "date_ymd"
        if ftype in {"radio", "checkbox"}:
            choices = " | ".join(f"{p.code}, {p.label}" for p in c.permissible_values)
        note = " ".join(x for x in [f"Unit: {c.unit}." if c.unit else "", f"Recall: {c.recall_period}." if c.recall_period else ""] if x)
        codes = " ".join(f"{m.system}:{m.code}" for m in c.mappings if m.code and m.status != "rejected")
        annotation = f"@CDE={c.cde_code} v{c.version} {codes}".strip()
        w.writerow([c.variable_name, form, section, ftype, c.question_text or c.name, choices, note, validation,
                    _fmt_num(c.min_value) if validation else "", _fmt_num(c.max_value) if validation else "",
                    "", "", "", "", "", "", "", annotation])
    return buf.getvalue()


def export_concept_sets(db: Session) -> str:
    """Source-code concept sets per CDE; resolve to OMOP concept_ids with the SQL from concept_set_sql()."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["cde_code", "variable_name", "target", "vocabulary_id", "concept_code", "display"])
    vocab = {"ICD10CM": "ICD10CM", "LOINC": "LOINC", "CPT": "CPT4", "CDT": "CDT", "ATC": "ATC", "RxNorm": "RxNorm",
             "SNOMEDCT": "SNOMED"}
    for c in _cdes(db):
        for i in c.implementations:
            if i.target != "OMOP":
                continue
            for v in i.value_set or []:
                w.writerow([c.cde_code, c.variable_name, f"{i.table_name}.{i.field_name}",
                            vocab.get(v["system"], v["system"]), v["code"], v.get("display")])
    return buf.getvalue()


def concept_set_sql() -> str:
    return """-- Resolve exported source codes to OMOP standard concepts (run against your OMOP vocabulary schema).
-- 1) load omop_concept_sets.csv into a table tmd_cde_codes(cde_code, variable_name, target, vocabulary_id, concept_code, display)
-- 2) codes ending without a full child (e.g. ICD10CM 'M26.6', ATC 'M01A') are expanded with LIKE / concept_ancestor.
SELECT t.cde_code, t.vocabulary_id, t.concept_code, c.concept_id AS source_concept_id,
       c2.concept_id AS standard_concept_id, c2.concept_name AS standard_concept_name
FROM tmd_cde_codes t
JOIN concept c
  ON c.vocabulary_id = t.vocabulary_id
 AND (c.concept_code = t.concept_code OR c.concept_code LIKE t.concept_code || '%')
LEFT JOIN concept_relationship cr ON cr.concept_id_1 = c.concept_id AND cr.relationship_id = 'Maps to'
                                 AND cr.invalid_reason IS NULL
LEFT JOIN concept c2 ON c2.concept_id = cr.concept_id_2;
"""


def export_nlp_lexicon(db: Session) -> dict:
    return {c.variable_name: {"cde_code": c.cde_code, "name": c.name, "terms": i.nlp_terms}
            for c in _cdes(db) for i in c.implementations if i.target == "NLP"}


def export_phenotype(db: Session) -> list[dict]:
    return [{"key": p.key, "name": p.name, "version": p.version, "status": p.status, "description": p.description,
             "definition": p.definition} for p in db.scalars(select(Phenotype))]


def coverage_report(db: Session) -> dict:
    """Aim 3-style metrics that can be computed before EHR data exist, and re-run after."""
    cdes = _cdes(db)
    n = len(cdes) or 1

    def pct(k: int) -> float:
        return round(100 * k / n, 1)

    mapped = sum(1 for c in cdes if any(m.code and m.relation != "gap" for m in c.mappings)
                 or any(p.concept_code for p in c.permissible_values))
    ehr_ready = sum(1 for c in cdes if any(i.target in {"OMOP", "NLP"} for i in c.implementations))
    open_issues = Counter(q.severity for q in db.scalars(select(QAIssue).where(QAIssue.resolved.is_(False))))
    return {
        "n_cdes": len(cdes),
        "by_tier": dict(Counter(c.tier for c in cdes)),
        "by_domain": dict(Counter(c.domain for c in cdes)),
        "by_status": dict(Counter(c.status for c in cdes)),
        "by_expected_source": dict(Counter(c.expected_source for c in cdes)),
        "by_ehr_capturability": dict(Counter(c.ehr_capturability for c in cdes)),
        "ehr_validation_status": dict(Counter(c.ehr_validation_status for c in cdes)),
        "terminology_coverage_pct": pct(mapped),
        "ehr_binding_coverage_pct": pct(ehr_ready),
        "multi_source_pct": pct(sum(1 for c in cdes if len({p.source_key for p in c.provenance}) > 1)),
        "mean_qa_score": round(sum(c.qa_score or 0 for c in cdes) / n, 3),
        "open_qa_issues": dict(open_issues),
        "delphi_decisions": dict(Counter(delphi_summary_for(c)["decision"] for c in cdes)),
    }
