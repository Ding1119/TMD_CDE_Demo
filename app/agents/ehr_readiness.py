"""Phenotype & EHR-Readiness Agent (added to the slide-7 design for the no-EHR phase).

Builds the implementation layer for every CDE *before* EHR data exist:
* OMOP / PCORnet bindings with value sets for structurally capturable elements
* i2b2 bindings when i2b2 ontology metadata (no patient data) has been uploaded
* NLP lexicons for elements that live in clinical notes
* REDCap bindings for prospective collection
and maintains the draft TMD computable phenotype.
"""
from __future__ import annotations

import re

from sqlalchemy import select

from ..knowledge.base import document_records
from ..knowledge.ehr_cdm import DEMOGRAPHIC_TARGETS, OMOP_TARGETS
from ..models import CDE, Implementation, Phenotype, SourceDocument
from ..seed.nih_tmd_seed import NLP_LEXICON, TMD_ICD10CM_CODES, TMD_PHENOTYPE
from .base import Agent, AgentContext, AgentResult

NLP_CONCEPT_RULES = {
    "joint_noise": r"noise|click|crepitus|pop",
    "locking": r"lock",
    "limited_opening": r"limit\w* (mouth )?opening",
    "mouth_opening_mm": r"\bopening\b",
    "bruxism": r"bruxism|oral behavio|clench|grind",
    "splint": r"splint|occlusal",
    "headache_temporal": r"headache",
    "myalgia": r"myalgia|masseter|temporalis|myofascial",
    "arthralgia": r"arthralgia|tmj palpation|tmj noise",
    "disc_displacement": r"disc|intra-articular",
    "jaw_pain": r"\bpain\b",
}


def nlp_terms_for(cde: CDE) -> list[str]:
    text = f"{cde.name} {cde.definition or ''}".lower()
    terms: list[str] = []
    for concept, pattern in NLP_CONCEPT_RULES.items():
        if re.search(pattern, text):
            terms += [t for t in NLP_LEXICON[concept] if t not in terms]
    return terms[:25]


class EHRReadinessAgent(Agent):
    name = "ehr_readiness"
    title = "Phenotype & EHR-Readiness Agent"
    description = "Drafts OMOP/PCORnet/i2b2/REDCap/NLP bindings and the TMD computable phenotype so EHR data can be plugged in later."

    @staticmethod
    def _i2b2_index(ctx: AgentContext) -> dict[str, list[str]]:
        idx: dict[str, list[str]] = {}
        for doc in ctx.db.scalars(select(SourceDocument).where(SourceDocument.doc_type == "ehr_dictionary")):
            if (doc.meta or {}).get("model") != "i2b2":
                continue
            for rec in document_records(doc):
                code = str(rec.get("basecode") or "")
                if ":" in code:
                    sysname, val = code.split(":", 1)
                    idx.setdefault(f"{sysname.upper().replace('-', '')}:{val}", []).append(rec.get("path"))
        return idx

    @staticmethod
    def _value_set(cde: CDE) -> list[dict]:
        vs = [{"system": m.system, "code": m.code, "display": m.display}
              for m in cde.mappings if m.code and m.relation != "gap" and m.status != "rejected"]
        vs += [{"system": p.concept_system, "code": p.concept_code, "display": p.label}
               for p in cde.permissible_values if p.concept_code]
        if any(v["system"] == "ICD10CM" and v["code"] in {"M26.6", "M79.11"} for v in vs) and cde.tier == "core" \
                and cde.data_type == "code":
            vs += [{"system": "ICD10CM", "code": c, "display": d} for c, d in TMD_ICD10CM_CODES]
        seen, out = set(), []
        for v in vs:
            k = (v["system"], v["code"])
            if k not in seen:
                seen.add(k)
                out.append(v)
        return out

    def run(self, ctx: AgentContext) -> AgentResult:
        db = ctx.db
        i2b2 = self._i2b2_index(ctx)
        stats = {"OMOP": 0, "PCORnet": 0, "i2b2": 0, "NLP": 0, "REDCap": 0, "no_value_set": 0}
        for cde in db.scalars(select(CDE).where(CDE.status != "retired")).all():
            have = {i.target for i in cde.implementations}
            vs = self._value_set(cde)

            def add(target: str, **kw) -> None:
                if target not in have:
                    db.add(Implementation(cde=cde, target=target, **kw))
                    have.add(target)
                    stats[target] += 1

            if cde.ehr_capturability == "structured":
                demo = DEMOGRAPHIC_TARGETS.get(cde.variable_name)
                systems = [v["system"] for v in vs if v["system"] in OMOP_TARGETS]
                if demo:
                    add("OMOP", table_name=demo[0], field_name=demo[1], value_set=[], logic="Demographic field")
                    add("PCORnet", table_name=demo[2], field_name=demo[3], value_set=[], logic="Demographic field")
                elif systems:
                    o_tab, o_fld, p_tab, p_fld = OMOP_TARGETS[systems[0]]
                    logic = "Any record with a code in the value set; earliest date = first occurrence"
                    add("OMOP", table_name=o_tab, field_name=o_fld, value_set=vs, logic=logic)
                    add("PCORnet", table_name=p_tab, field_name=p_fld, value_set=vs, logic=logic)
                    paths = [p for v in vs for p in i2b2.get(f"{v['system']}:{v['code']}", [])]
                    if paths:
                        add("i2b2", table_name="concept_dimension", field_name="concept_path",
                            value_set=[{"system": "i2b2", "code": p} for p in paths[:200]],
                            logic="Concepts matched by c_basecode in uploaded i2b2 ontology metadata")
                else:
                    stats["no_value_set"] += 1
            proxy_systems = [v["system"] for v in vs if v["system"] in OMOP_TARGETS]
            if cde.ehr_capturability == "note_nlp" and proxy_systems:
                o_tab, o_fld, p_tab, p_fld = OMOP_TARGETS[proxy_systems[0]]
                logic = "Proxy codes (less specific than the CDE definition); confirm with NLP / chart review"
                add("OMOP", table_name=o_tab, field_name=o_fld, value_set=vs, logic=logic)
                add("PCORnet", table_name=p_tab, field_name=p_fld, value_set=vs, logic=logic)
            if cde.ehr_capturability == "note_nlp" or cde.expected_source in {"clinical_note", "imaging"}:
                terms = nlp_terms_for(cde)
                if terms:
                    add("NLP", table_name="note_nlp", field_name="lexical_variant", nlp_terms=terms,
                        logic="Match lexicon in notes; exclude negated / historical mentions (term_exists, term_temporal)")
            if cde.expected_source in {"pro", "exam", "derived", "biomarker"}:
                form = re.sub(r"[^a-z0-9]+", "_", (cde.instrument or cde.domain).lower()).strip("_")
                add("REDCap", table_name=form[:50], field_name=cde.variable_name,
                    logic="Prospective collection via REDCap data dictionary export")
            if cde.ehr_validation_status != "validated":
                cde.ehr_validation_status = "not_applicable" if cde.ehr_capturability == "unlikely" else "pending"
        self._upsert_phenotype(ctx)
        db.commit()
        ctx.log(self.name, "Implementation layer drafted: " + ", ".join(f"{k} {v}" for k, v in stats.items()), **stats)
        return AgentResult(self.name, stats)

    @staticmethod
    def _upsert_phenotype(ctx: AgentContext) -> None:
        ph = ctx.db.scalar(select(Phenotype).where(Phenotype.key == TMD_PHENOTYPE["key"]))
        definition = {
            **TMD_PHENOTYPE["definition"],
            "value_sets": {
                "TMD_ICD10CM": [{"code": c, "display": d} for c, d in TMD_ICD10CM_CODES],
                "TMD_NLP": {k: v for k, v in NLP_LEXICON.items() if k != "mouth_opening_mm"},
            },
        }
        if ph is None:
            ctx.db.add(Phenotype(key=TMD_PHENOTYPE["key"], name=TMD_PHENOTYPE["name"],
                                 description=TMD_PHENOTYPE["description"], definition=definition))
        elif ph.status != "validated":
            ph.definition = definition
