"""Terminology services for the Terminology Mapping Agent.

Order of use: curated local lexicon (offline) -> public APIs (online mode) -> LLM suggestion.
Public APIs used (no key needed unless noted):
* NLM Clinical Table Search Service: ICD-10-CM and LOINC (https://clinicaltables.nlm.nih.gov)
* EBI Ontology Lookup Service (OLS4): HPO, NCIT and other OBO ontologies
* UMLS Terminology Services: SNOMED CT (needs a free UMLS_API_KEY)
Every API suggestion is stored as "proposed" and must be accepted by a reviewer.
"""
from __future__ import annotations

from typing import Optional

import logging
import re
from functools import lru_cache

import httpx

from ..config import settings

log = logging.getLogger(__name__)

# (regex on CDE name/definition, mapping). Only codes verified against the official code systems.
LOCAL_LEXICON: list[tuple[str, dict]] = [
    (r"\btemporomandibular (joint )?disorders?\b|\btmj disorders?\b",
     dict(system="ICD10CM", code="M26.6", display="Temporomandibular joint disorders", relation="broad", confidence=0.8)),
    (r"\barthralgia\b", dict(system="ICD10CM", code="M26.62", display="Arthralgia of temporomandibular joint",
                             relation="broad", confidence=0.75)),
    (r"\bmyalgia\b|\bmyofascial\b", dict(system="ICD10CM", code="M79.11", display="Myalgia of mastication muscle",
                                        relation="related", confidence=0.7)),
    (r"\bdisc displacement\b|\barticular disc\b", dict(system="ICD10CM", code="M26.63",
                                                       display="Articular disc disorder of temporomandibular joint",
                                                       relation="broad", confidence=0.7)),
    (r"\bdegenerative joint\b|\barthropathy\b", dict(system="ICD10CM", code="M26.65",
                                                    display="Arthropathy of temporomandibular joint",
                                                    relation="related", confidence=0.5)),
    (r"\bankylosis\b|\badhesions?\b", dict(system="ICD10CM", code="M26.61",
                                          display="Adhesions and ankylosis of temporomandibular joint",
                                          relation="broad", confidence=0.7)),
    (r"\bsleep[- ](related )?bruxism\b", dict(system="ICD10CM", code="G47.63", display="Sleep related bruxism",
                                             relation="exact", confidence=0.9)),
    (r"\bfibromyalgia\b", dict(system="ICD10CM", code="M79.7", display="Fibromyalgia", relation="exact", confidence=0.9)),
    (r"\bphq-?9\b.*\btotal\b", dict(system="LOINC", code="44261-6",
                                    display="Patient Health Questionnaire 9 item (PHQ-9) total score [Reported]",
                                    relation="exact", confidence=0.95)),
    (r"\bphq-?2\b.*\btotal\b", dict(system="LOINC", code="55758-7",
                                    display="Patient Health Questionnaire 2 item (PHQ-2) total score [Reported]",
                                    relation="exact", confidence=0.95)),
    (r"\bgad-?7\b.*\btotal\b", dict(system="LOINC", code="70274-6",
                                    display="Generalized anxiety disorder 7 item (GAD-7) total score [Reported.PHQ]",
                                    relation="exact", confidence=0.95)),
    (r"\bocclusal (splint|orthotic|appliance)", dict(system="CDT", code="D7880", display="TMD orthotic appliance (code only; descriptor (c) ADA)",
                                                    relation="related", confidence=0.8)),
    (r"\barthrocentesis\b", dict(system="CDT", code="D7870", display="TMJ arthrocentesis (code only; descriptor (c) ADA)", relation="related", confidence=0.8)),
]

# Element families where a code system is expected; absence is recorded as an explicit "gap".
EXPECTED_SYSTEMS = {
    "pro": "LOINC",
    "derived": "LOINC",
    "structured_ehr": "ICD10CM",
}


def lexicon_lookup(text: str) -> list[dict]:
    t = (text or "").lower()
    seen, out = set(), []
    for pattern, mapping in LOCAL_LEXICON:
        if re.search(pattern, t) and (mapping["system"], mapping["code"]) not in seen:
            seen.add((mapping["system"], mapping["code"]))
            out.append({**mapping, "method": "lexicon"})
    return out


@lru_cache(maxsize=512)
def _get_json(url: str, params_key: tuple) -> Optional[object]:
    try:
        r = httpx.get(url, params=dict(params_key), timeout=20.0)
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.info("Terminology API call failed: %s %s", url, exc)
        return None


def icd10cm_search(term: str, n: int = 3) -> list[dict]:
    data = _get_json("https://clinicaltables.nlm.nih.gov/api/icd10cm/v3/search",
                     tuple(sorted({"terms": term, "sf": "code,name", "df": "code,name", "maxList": n}.items())))
    if not isinstance(data, list) or len(data) < 4:
        return []
    return [dict(system="ICD10CM", code=row[0], display=row[1], relation="related", confidence=0.5, method="api")
            for row in data[3][:n]]


def loinc_search(term: str, n: int = 3) -> list[dict]:
    data = _get_json("https://clinicaltables.nlm.nih.gov/api/loinc_items/v3/search",
                     tuple(sorted({"terms": term, "df": "LOINC_NUM,LONG_COMMON_NAME", "maxList": n}.items())))
    if not isinstance(data, list) or len(data) < 4:
        return []
    return [dict(system="LOINC", code=row[0], display=row[1], relation="related", confidence=0.5, method="api")
            for row in data[3][:n]]


def ols_search(term: str, ontologies: str = "hp,ncit", n: int = 3) -> list[dict]:
    data = _get_json("https://www.ebi.ac.uk/ols4/api/search",
                     tuple(sorted({"q": term, "ontology": ontologies, "rows": n}.items())))
    docs = (data or {}).get("response", {}).get("docs", []) if isinstance(data, dict) else []
    out = []
    for d in docs[:n]:
        system = {"hp": "HPO", "ncit": "NCIT"}.get(d.get("ontology_name", ""), d.get("ontology_name", "OBO").upper())
        out.append(dict(system=system, code=d.get("obo_id") or d.get("short_form") or "", display=d.get("label"),
                        relation="related", confidence=0.45, method="api"))
    return [o for o in out if o["code"]]


def umls_snomed_search(term: str, n: int = 3) -> list[dict]:
    if not settings.umls_api_key:
        return []
    data = _get_json("https://uts-ws.nlm.nih.gov/rest/search/current",
                     tuple(sorted({"string": term, "sabs": "SNOMEDCT_US", "returnIdType": "code",
                                   "pageSize": n, "apiKey": settings.umls_api_key}.items())))
    results = ((data or {}).get("result") or {}).get("results", []) if isinstance(data, dict) else []
    return [dict(system="SNOMEDCT", code=r["ui"], display=r.get("name"), relation="related", confidence=0.5, method="api")
            for r in results[:n] if r.get("ui") and r["ui"] != "NONE"]


def online_lookup(name: str, expected_source: Optional[str]) -> list[dict]:
    """Query public terminology APIs; which ones depends on the kind of element."""
    if not settings.online:
        return []
    term = re.sub(r"\b(score|total|subscale|item|in last \d+ days)\b", "", name, flags=re.I).strip()
    out: list[dict] = []
    if expected_source in {"pro", "derived"}:
        out += loinc_search(name)
    elif expected_source == "structured_ehr":
        out += icd10cm_search(term)
    out += umls_snomed_search(term)
    if expected_source in {"exam", "clinical_note", "imaging"}:
        out += ols_search(term)
    return out
