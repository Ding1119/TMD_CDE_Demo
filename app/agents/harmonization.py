"""Harmonization Agent: deduplicate, normalize and align concepts and domains (slide 7, agent 4)."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select

from ..models import CDE, Candidate, PermissibleValue, Provenance, TerminologyMapping
from ..services.versioning import record_version
from .base import Agent, AgentContext, AgentResult
from .heuristics import (default_tier, instrument_key, keyword_hits, make_variable_name, name_similarity,
                         normalize_tokens, numeric_tokens)

CREATE_THRESHOLD = 0.6
MERGE_SIMILARITY = 0.78


def can_merge(cand: Candidate, cde: CDE) -> tuple[bool, float]:
    if numeric_tokens(cand.name) != numeric_tokens(cde.name):  # e.g. PHQ-2 vs PHQ-9, JFLS-8 vs JFLS-20
        return False, 0.0
    ik_c, ik_d = instrument_key(cand.instrument), instrument_key(cde.instrument)
    different_instrument = bool(ik_c and ik_d and ik_c != ik_d and not (cand.meta or {}).get("from_dictionary"))
    if different_instrument:
        # same concept may live in differently named CRFs; only near-identical, specific names merge
        sim = name_similarity(cand.name, cde.name)
        return (sim >= 0.95 and len(normalize_tokens(cand.name)) >= 3), sim
    hint = (cand.meta or {}).get("variable_hint")
    if hint and hint.lower() == cde.variable_name:
        return True, 1.0
    if cand.document_id in {p.document_id for p in cde.provenance}:
        return False, 0.0  # elements of one source document are distinct by construction
    sim = name_similarity(cand.name, cde.name)
    if sim >= MERGE_SIMILARITY:
        return True, sim
    cand_exact = {(m["system"], m["code"]) for m in (cand.meta or {}).get("mappings", []) if m.get("relation") == "exact"}
    cde_exact = {(m.system, m.code) for m in cde.mappings if m.relation == "exact"}
    if cand_exact & cde_exact and sim >= 0.6:
        return True, sim
    return False, sim


class HarmonizationAgent(Agent):
    name = "harmonization"
    title = "Harmonization Agent"
    description = "Clusters candidates into CDEs, merges provenance and mappings, assigns domain/tier and variable names."

    def _next_code(self, ctx: AgentContext) -> str:
        n = ctx.db.scalar(select(func.count(CDE.id))) or 0
        while True:
            n += 1
            code = f"TMD-CDE-{n:04d}"
            if not ctx.db.scalar(select(CDE.id).where(CDE.cde_code == code)):
                return code

    @staticmethod
    def _merge_mappings(ctx: AgentContext, cde: CDE, cand: Candidate) -> int:
        existing = {(m.system, m.code) for m in cde.mappings}
        has_real = {m.system for m in cde.mappings if m.relation != "gap"}
        added = 0
        for m in (cand.meta or {}).get("mappings", []):
            key = (m["system"], m.get("code", ""))
            if key in existing or (m.get("relation") == "gap" and m["system"] in has_real):
                continue
            ctx.db.add(TerminologyMapping(cde=cde, system=m["system"], code=m.get("code", ""), display=m.get("display"),
                                          relation=m.get("relation", "related"), confidence=m.get("confidence", 0.5),
                                          method=m.get("method", "seed"), note=m.get("note")))
            existing.add(key)
            added += 1
        # a real code for a system resolves an earlier "gap" placeholder
        for m in list(cde.mappings):
            if m.relation == "gap" and any(x[0] == m.system and x[1] for x in existing):
                cde.mappings.remove(m)  # delete-orphan cascade removes it
        return added

    @staticmethod
    def _add_provenance(ctx: AgentContext, cde: CDE, cand: Candidate) -> None:
        meta = cand.meta or {}
        ctx.db.add(Provenance(cde=cde, source_key=meta.get("source_key", "unknown"), document_id=cand.document_id,
                              candidate_id=cand.id, citation=meta.get("citation"), uri=meta.get("uri")))

    @staticmethod
    def _draft_definition(ctx: AgentContext, cand: Candidate) -> Optional[str]:
        if cand.definition or not ctx.llm.enabled or not ctx.options.get("use_llm", True):
            return cand.definition
        data = ctx.llm.complete_json(
            "You write precise, one-sentence ISO/IEC 11179-style definitions for clinical research data elements.",
            f'Return {{"definition": "..."}} for the TMD research data element "{cand.name}" '
            f"(instrument: {cand.instrument}; type: {cand.data_type}; unit: {cand.unit}).")
        if isinstance(data, dict) and data.get("definition"):
            cand.meta = {**(cand.meta or {}), "notes": "Definition drafted by LLM - review before approval."}
            return str(data["definition"])
        return None

    def _create(self, ctx: AgentContext, cand: Candidate, taken: set[str]) -> CDE:
        cand.definition = self._draft_definition(ctx, cand)
        meta = cand.meta or {}
        cde = CDE(
            cde_code=self._next_code(ctx), variable_name=make_variable_name(meta.get("variable_hint"), cand.name, taken),
            name=cand.name, definition=cand.definition, question_text=cand.question_text, data_type=cand.data_type or "text",
            unit=cand.unit, min_value=cand.min_value, max_value=cand.max_value, instrument=cand.instrument,
            domain=cand.domain, tier=default_tier(cand.domain, cand.tier_hint), recall_period=cand.recall_period,
            expected_source=cand.expected_source, ehr_capturability=cand.ehr_capturability,
            ehr_validation_status="not_applicable" if cand.ehr_capturability == "unlikely" else "pending",
            status="draft", version="0.1.0", copyright_note=meta.get("copyright"), notes=meta.get("notes"),
        )
        ctx.db.add(cde)
        for i, p in enumerate(cand.permissible_values or []):
            ctx.db.add(PermissibleValue(cde=cde, code=str(p.get("code", i)), label=str(p.get("label", p.get("code", ""))),
                                        sort_order=i, concept_system=p.get("concept_system"),
                                        concept_code=p.get("concept_code")))
        ctx.db.flush()
        return cde

    @staticmethod
    def _fill_gaps(cde: CDE, cand: Candidate) -> list[str]:
        filled = []
        for field in ("definition", "question_text", "unit", "min_value", "max_value", "recall_period", "instrument"):
            if getattr(cde, field) in (None, "") and getattr(cand, field) not in (None, ""):
                setattr(cde, field, getattr(cand, field))
                filled.append(field)
        return filled

    def run(self, ctx: AgentContext) -> AgentResult:
        db = ctx.db
        cands = db.scalars(select(Candidate).where(Candidate.status == "mapped")).all()
        cands.sort(key=lambda c: -c.confidence)  # best candidate becomes the cluster representative
        pool = db.scalars(select(CDE).where(CDE.status != "retired")).all()
        taken = set(db.scalars(select(CDE.variable_name)))
        stats = {"candidates": len(cands), "merged": 0, "created": 0, "unmatched": 0}
        for cand in cands:
            best, best_sim = None, 0.0
            for cde in pool:
                ok, sim = can_merge(cand, cde)
                if ok and sim > best_sim:
                    best, best_sim = cde, sim
            if best is not None:
                self._merge_mappings(ctx, best, cand)
                self._add_provenance(ctx, best, cand)
                filled = self._fill_gaps(best, cand)
                if filled and best.status != "approved":
                    record_version(db, best, f"Harmonization filled {', '.join(filled)} from candidate #{cand.id}",
                                   author="harmonization_agent")
                cand.status, cand.cde_id = "merged", best.id
                stats["merged"] += 1
                continue
            relevant = keyword_hits(f"{cand.name} {cand.definition or ''}") > 0
            if cand.confidence >= CREATE_THRESHOLD or (relevant and cand.method != "rule"):
                cde = self._create(ctx, cand, taken)
                self._merge_mappings(ctx, cde, cand)
                self._add_provenance(ctx, cde, cand)
                db.flush()
                db.refresh(cde)
                record_version(db, cde, f"Created from candidate #{cand.id} ({cand.method})",
                               author="harmonization_agent", part=None)
                pool.append(cde)
                cand.status, cand.cde_id = "merged", cde.id
                stats["created"] += 1
            else:
                cand.status = "unmatched"
                stats["unmatched"] += 1
        db.commit()
        ctx.log(self.name, f"{stats['created']} new CDEs, {stats['merged']} candidates merged into existing CDEs, "
                           f"{stats['unmatched']} left for manual review", **stats)
        return AgentResult(self.name, stats)


def promote_candidate(db, cand: Candidate, author: str = "reviewer") -> CDE:
    """Manually promote an unmatched candidate to a draft CDE (used from the UI)."""
    from .base import AgentContext  # local import to avoid cycles
    from .llm import LLMClient
    from ..models import PipelineRun

    run = PipelineRun(status="completed", config={"manual": "promote"})
    db.add(run)
    db.flush()
    ctx = AgentContext(db=db, run=run, llm=LLMClient(enabled=False))
    agent = HarmonizationAgent()
    taken = set(db.scalars(select(CDE.variable_name)))
    cde = agent._create(ctx, cand, taken)
    agent._merge_mappings(ctx, cde, cand)
    agent._add_provenance(ctx, cde, cand)
    db.flush()
    db.refresh(cde)
    record_version(db, cde, f"Promoted manually from candidate #{cand.id}", author=author, part=None)
    cand.status, cand.cde_id = "merged", cde.id
    db.commit()
    return cde

