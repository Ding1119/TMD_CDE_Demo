"""Terminology Mapping Agent: map candidates to standard terms (ICD-10-CM, LOINC, SNOMED CT, HPO/NCIT) (slide 7, agent 3)."""
from __future__ import annotations

from sqlalchemy import select

from ..config import settings
from ..knowledge.terminology import EXPECTED_SYSTEMS, lexicon_lookup, online_lookup
from ..models import Candidate
from .base import Agent, AgentContext, AgentResult

LLM_SYSTEM = "You are a clinical terminology specialist (ICD-10-CM, LOINC, SNOMED CT, HPO, NCIT)."
LLM_PROMPT = """Suggest up to 3 standard codes for this research data element. Only suggest codes you are confident exist.
Return a JSON array of objects: system (ICD10CM|LOINC|SNOMEDCT|HPO|NCIT), code, display, relation (exact|broad|narrow|related).
Return [] if unsure.

Element: {name}
Definition: {definition}
Instrument: {instrument}"""


class TerminologyMappingAgent(Agent):
    name = "terminology_mapping"
    title = "Terminology Mapping Agent"
    description = "Proposes mappings from a curated lexicon, public terminology APIs (online) and the LLM; records gaps explicitly."

    def run(self, ctx: AgentContext) -> AgentResult:
        cands = ctx.db.scalars(select(Candidate).where(Candidate.status == "new")).all()
        stats = {"candidates": len(cands), "seed": 0, "lexicon": 0, "api": 0, "llm": 0, "gaps": 0}
        use_online = settings.online and ctx.options.get("online", True)
        use_llm = ctx.llm.enabled and ctx.options.get("use_llm", True) and ctx.options.get("llm_mapping", True)
        for c in cands:
            maps = [dict(m, method=m.get("method") or "seed") for m in (c.meta or {}).get("mappings", [])]
            stats["seed"] += len(maps)
            have = {(m["system"], m["code"]) for m in maps}

            def add(items: list[dict], key: str) -> None:
                for mm in items:
                    if (mm["system"], mm["code"]) not in have:
                        have.add((mm["system"], mm["code"]))
                        maps.append(mm)
                        stats[key] += 1

            add(lexicon_lookup(f"{c.name} {c.instrument or ''}"), "lexicon")  # name only: fewer false positives
            has_exact = any(m.get("relation") == "exact" for m in maps)
            if use_online and not has_exact:
                add(online_lookup(c.name, c.expected_source), "api")
            if use_llm and not maps:
                data = ctx.llm.complete_json(LLM_SYSTEM, LLM_PROMPT.format(name=c.name, definition=c.definition,
                                                                          instrument=c.instrument))
                if isinstance(data, list):
                    add([dict(system=d.get("system", ""), code=d.get("code", ""), display=d.get("display"),
                              relation=d.get("relation", "related"), confidence=0.4, method="llm",
                              note="LLM suggestion - verify in the source terminology")
                         for d in data if isinstance(d, dict) and d.get("code")], "llm")
            expected = EXPECTED_SYSTEMS.get(c.expected_source or "")
            value_level = any(pv.get("concept_code") for pv in c.permissible_values or [])
            if expected and not value_level and not any(m["system"] == expected for m in maps):
                maps.append(dict(system=expected, code="", display=f"No {expected} code identified", relation="gap",
                                 confidence=0.0, method="agent",
                                 note="Terminology gap: consider requesting a new code or documenting as local concept"))
                stats["gaps"] += 1
            c.meta = {**(c.meta or {}), "mappings": maps}
            c.status = "mapped"
        ctx.db.commit()
        ctx.log(self.name, f"Mapped {len(cands)} candidates: {stats['lexicon']} lexicon, {stats['api']} API, "
                           f"{stats['llm']} LLM suggestions; {stats['gaps']} terminology gaps recorded", **stats)
        return AgentResult(self.name, stats)
