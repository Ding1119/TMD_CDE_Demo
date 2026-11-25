"""Extraction Agent: extract CDE candidates (concepts, definitions, attributes) from sources (slide 7, agent 2)."""
from __future__ import annotations

from typing import Optional

import json
import re

from sqlalchemy import select

from ..knowledge.base import document_records
from ..models import DATA_TYPES, DOMAINS, EXPECTED_SOURCES, Candidate, SourceDocument
from .base import Agent, AgentContext, AgentResult
from .heuristics import coerce_domain, keyword_hits, predict_capturability, predict_expected_source

SKIP_VARIABLES = re.compile(r"^(record_id|redcap_\w+|\w+_complete|subject_?id|participant_?id|dbgap_\w+)$", re.I)

# Rule-based extraction from free text: pattern -> candidate template (names aligned with seed CDEs so they merge)
TEXT_PATTERNS: list[tuple[str, dict]] = [
    (r"\bmaximum (unassisted |mouth |jaw )?opening\b|\bMUO\b|\bmaximal mouth opening\b",
     dict(name="Maximum unassisted opening", data_type="integer", unit="mm", domain="Jaw Function", expected_source="exam")),
    (r"\bpain[- ]free (mouth )?opening\b",
     dict(name="Pain-free opening", data_type="integer", unit="mm", domain="Jaw Function", expected_source="exam")),
    (r"\bjoint (sounds?|noises?)\b|\bclicking\b|\bcrepitus\b",
     dict(name="Examiner-detected TMJ noise", data_type="choice", domain="TMD Clinical Features", expected_source="exam")),
    (r"\b(closed|open) lock(ing)?\b|\bjaw lock(ing)?\b",
     dict(name="History of closed locking", data_type="boolean", domain="TMD Clinical Features", expected_source="pro")),
    (r"\bGraded Chronic Pain Scale\b|\bGCPS\b",
     dict(name="GCPS characteristic pain intensity", data_type="integer", domain="Pain Characteristics",
          expected_source="pro", instrument="GCPS v2.0")),
    (r"\bJFLS-?8\b",
     dict(name="JFLS-8 global score", data_type="float", domain="Functional Outcomes", expected_source="pro", instrument="JFLS-8")),
    (r"\bJFLS-?20\b|\bJaw Functional Limitation Scale\b",
     dict(name="JFLS-20 global score", data_type="float", domain="Jaw Function", expected_source="pro", instrument="JFLS-20")),
    (r"\bOral Behaviou?rs? Checklist\b|\bOBC\b",
     dict(name="OBC-21 total score", data_type="integer", domain="Psychosocial Factors", expected_source="pro", instrument="OBC-21")),
    (r"\bPHQ-?9\b", dict(name="PHQ-9 total score", data_type="integer", domain="Psychosocial Factors", expected_source="pro",
                         instrument="PHQ-9")),
    (r"\bGAD-?7\b", dict(name="GAD-7 total score", data_type="integer", domain="Psychosocial Factors", expected_source="pro",
                         instrument="GAD-7")),
    (r"\bPHQ-?15\b", dict(name="PHQ-15 total score", data_type="integer", domain="Psychosocial Factors",
                          expected_source="pro", instrument="PHQ-15")),
    (r"\bPain Catastrophi[sz]ing Scale\b",
     dict(name="PCS-6 total score", data_type="integer", domain="Psychosocial Factors", expected_source="pro", instrument="PCS-6")),
    (r"\bpressure pain thresholds?\b",
     dict(name="Masseter pressure pain threshold", data_type="float", unit="kPa", domain="Biomarkers", expected_source="biomarker",
          instrument="QST (OPPERA)")),
    (r"\bocclusal (splint|appliance|guard)s?\b|\bstabili[sz]ation splint\b",
     dict(name="Occlusal splint therapy", data_type="boolean", domain="Treatments", expected_source="structured_ehr")),
    (r"\barthrocentesis\b",
     dict(name="TMJ arthrocentesis", data_type="boolean", domain="Treatments", expected_source="structured_ehr")),
    (r"\bsleep bruxism\b", dict(name="Sleep bruxism diagnosis", data_type="boolean", domain="Sleep", expected_source="structured_ehr")),
    (r"\bCBCT\b|\bcone[- ]beam\b",
     dict(name="CBCT condylar degenerative findings", data_type="multi_choice", domain="Imaging", expected_source="imaging")),
    (r"\bMRI\b", dict(name="TMJ MRI disc position", data_type="choice", domain="Imaging", expected_source="imaging")),
    (r"\bPROMIS\b[^.]{0,40}\bphysical function",
     dict(name="PROMIS Physical Function 6b T-score", data_type="float", domain="Functional Outcomes", expected_source="pro",
          instrument="PROMIS PF 6b")),
    (r"\bfibromyalgia|irritable bowel|overlapping (chronic )?pain",
     dict(name="Comorbid chronic overlapping pain conditions", data_type="multi_choice", domain="Diagnoses & Comorbidities",
          expected_source="structured_ehr")),
]

LLM_SYSTEM = (
    "You are a biomedical data-standards curator building Common Data Elements (CDEs) for temporomandibular "
    "disorder (TMD) research. Extract only data elements (variables that a study would collect), not findings."
)
LLM_PROMPT = """From the document below, list the data elements it measures or recommends collecting.
Return a JSON array (max 25 items); each item has:
  name (short, specific), definition (one sentence in your own words), question_text (or null),
  data_type (one of {types}), unit (or null), permissible_values (list of {{"code","label"}} or []),
  instrument (or null), domain (one of {domains}), expected_source (one of {sources}),
  recall_period (or null), evidence (a quote of at most 15 words from the document).
Return [] if the document contains no data elements.

DOCUMENT TITLE: {title}
DOCUMENT:
{text}"""


def _evidence(text: str, match: re.Match, width: int = 90) -> str:
    s, e = max(0, match.start() - width), min(len(text), match.end() + width)
    return re.sub(r"\s+", " ", text[s:e]).strip()


class ExtractionAgent(Agent):
    name = "extraction"
    title = "Extraction Agent"
    description = "Turns prioritized documents into CDE candidates (structured parsing, LLM, or rule-based text mining)."

    def _add(self, ctx: AgentContext, doc: SourceDocument, rec: dict, method: str, confidence: float) -> Optional[Candidate]:
        name = (rec.get("name") or "").strip()
        if not name:
            return None
        hint = rec.get("variable_hint")
        if hint and SKIP_VARIABLES.match(str(hint)):
            return None
        definition = rec.get("definition")
        instrument = rec.get("instrument")
        expected = rec.get("expected_source")
        if expected not in EXPECTED_SOURCES:
            expected = predict_expected_source(name, definition, instrument, rec.get("unit"))
        capt = rec.get("ehr_capturability") or predict_capturability(expected, instrument)
        dtype = rec.get("data_type") if rec.get("data_type") in DATA_TYPES else "text"
        if keyword_hits(f"{name} {definition or ''} {instrument or ''}"):
            confidence = min(1.0, confidence + 0.1)
        citation = (doc.meta or {}).get("citation") or doc.title
        cand = Candidate(
            document=doc, run_id=ctx.run.id, name=name[:300], definition=definition, question_text=rec.get("question_text"),
            data_type=dtype, unit=rec.get("unit"), min_value=rec.get("min_value"), max_value=rec.get("max_value"),
            permissible_values=rec.get("permissible_values") or [], instrument=instrument,
            domain=coerce_domain(rec.get("domain"), f"{name} {definition or ''}"), tier_hint=rec.get("tier_hint"),
            expected_source=expected, ehr_capturability=capt, recall_period=rec.get("recall_period"),
            confidence=round(confidence, 2), method=method, status="new",
            meta={
                "variable_hint": hint, "mappings": rec.get("mappings") or [], "copyright": rec.get("copyright"),
                "notes": rec.get("notes"), "evidence": rec.get("evidence"), "citation": citation,
                "uri": rec.get("external_ref") or doc.uri, "source_key": doc.source.key,
                # REDCap form names / dbGaP table ids are not instrument identities
                "from_dictionary": doc.doc_type == "dictionary",
            },
        )
        ctx.db.add(cand)
        return cand

    def _from_text_llm(self, ctx: AgentContext, doc: SourceDocument) -> Optional[list[dict]]:
        prompt = LLM_PROMPT.format(types=", ".join(DATA_TYPES), domains=", ".join(DOMAINS),
                                   sources=", ".join(EXPECTED_SOURCES), title=doc.title, text=doc.content[:12000])
        data = ctx.llm.complete_json(LLM_SYSTEM, prompt)
        return data if isinstance(data, list) else None

    @staticmethod
    def _from_text_rules(doc: SourceDocument) -> list[dict]:
        out, seen = [], set()
        for pattern, template in TEXT_PATTERNS:
            m = re.search(pattern, doc.content, flags=re.I)
            if m and template["name"] not in seen:
                seen.add(template["name"])
                out.append({**template, "evidence": _evidence(doc.content, m),
                            "definition": f"{template['name']} (mentioned in source; definition to be curated)."})
        return out

    def run(self, ctx: AgentContext) -> AgentResult:
        db = ctx.db
        docs = db.scalars(
            select(SourceDocument).where(SourceDocument.status == "prioritized", SourceDocument.doc_type != "ehr_dictionary")
        ).all()
        stats = {"documents": 0, "structured": 0, "llm": 0, "rule": 0}
        for doc in docs:
            if doc.doc_type in {"structured_cde", "dictionary"}:
                base_conf = 0.95 if doc.doc_type == "structured_cde" else 0.5
                for rec in document_records(doc):
                    if self._add(ctx, doc, rec, "structured", base_conf):
                        stats["structured"] += 1
            elif doc.doc_type == "text":
                items = self._from_text_llm(ctx, doc) if ctx.llm.enabled and ctx.options.get("use_llm", True) else None
                method = "llm" if items is not None else "rule"
                items = items if items is not None else self._from_text_rules(doc)
                for rec in items:
                    if self._add(ctx, doc, rec, method, 0.6 if method == "llm" else 0.45):
                        stats[method] += 1
            doc.status = "extracted"
            stats["documents"] += 1
        db.commit()
        ctx.log(self.name, f"Extracted {stats['structured'] + stats['llm'] + stats['rule']} candidates from "
                           f"{stats['documents']} documents", **stats)
        return AgentResult(self.name, stats)


def candidate_as_json(c: Candidate) -> str:  # small helper for templates/debugging
    return json.dumps({"name": c.name, "domain": c.domain, "type": c.data_type}, ensure_ascii=False)
