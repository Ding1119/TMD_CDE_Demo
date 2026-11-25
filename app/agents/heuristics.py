"""Rule-based helpers shared by agents (used alone in offline mode, or to check LLM output)."""
from __future__ import annotations

from typing import Optional

import re
from difflib import SequenceMatcher

from ..models import CORE_DOMAINS, DOMAINS
from ..seed.nih_tmd_seed import TMD_KEYWORDS

DOMAIN_RULES: list[tuple[str, str]] = [
    (r"\b(age|sex|gender|race|ethnic|education|employ|income|marital|zip)\b", "Demographics"),
    (r"\b(mri|cbct|cone[- ]beam|radiograph|imaging|ct scan)\b", "Imaging"),
    (r"\b(biomarker|saliva|serum|plasma|blood|cortisol|genotyp|qst|pressure pain threshold|algometer)\b", "Biomarkers"),
    (r"\b(sleep|bruxism|insomnia)\b", "Sleep"),
    (r"\b(splint|medication|drug|therapy|treatment|injection|surgery|arthrocentesis|botulinum|opioid|mme)\b", "Treatments"),
    (r"\b(icd|diagnos\w*|comorbid\w*|fibromyalgia|migraine|irritable bowel)\b", "Diagnoses & Comorbidities"),
    (r"\b(depress\w*|anxi\w*|catastroph\w*|stress|somat\w*|phq|gad|pcs|oral behavio\w*|obc|substance|alcohol|tobacco)\b",
     "Psychosocial Factors"),
    (r"\b(opening|excursion|protrusion|range of motion|mastication|chewing|jfls|mobility)\b", "Jaw Function"),
    (r"\b(palpation|click\w*|crepitus|joint noise|lock\w*|deviation)\b", "TMD Clinical Features"),
    (r"\b(disability|interference|quality of life|physical function\w*|grade)\b", "Functional Outcomes"),
    (r"\b(change|follow[- ]up|recurrence|onset|chronic\w*|progression|pgic)\b", "Longitudinal Outcomes"),
    (r"\b(pain|ache|intensity|headache)\b", "Pain Characteristics"),
]

PRO_INSTRUMENTS = re.compile(r"\b(phq|gad|promis|peg|bpi|pcs|jfls|obc|gcps|screener|questionnaire|scale|taps|pgic|sq)\b", re.I)
EHR_SCREENING_INSTRUMENTS = {"phq-9", "phq-2", "gad-7", "gad-2"}  # often stored in EHR flowsheets


def guess_domain(text: str) -> str:
    t = (text or "").lower()
    for pattern, domain in DOMAIN_RULES:
        if re.search(pattern, t):
            return domain
    return "TMD Clinical Features"


def coerce_domain(domain: Optional[str], fallback_text: str) -> str:
    if domain:
        for d in DOMAINS:
            if d.lower() == domain.strip().lower():
                return d
    return guess_domain(f"{domain or ''} {fallback_text}")


def default_tier(domain: str, hint: Optional[str]) -> str:
    if hint in {"core", "extension"}:
        return hint
    return "core" if domain in CORE_DOMAINS else "extension"


def predict_expected_source(name: str, definition: Optional[str] = None, instrument: Optional[str] = None,
                            unit: Optional[str] = None) -> str:
    t = f"{name} {definition or ''}".lower()
    if re.search(r"\b(icd|diagnosis code|procedure code|prescri\w*|medication|cdt|cpt)\b", t):
        return "structured_ehr"
    if re.search(r"\b(mri|cbct|imaging|radiograph)\b", t):
        return "imaging"
    if re.search(r"\b(saliva|serum|blood|genotyp\w*|threshold|algometer|qst)\b", t):
        return "biomarker"
    if re.search(r"\b(exam\w*|palpation|measured|ruler)\b", t) or (unit or "").lower() == "mm":
        return "exam"
    if re.search(r"\b(derived|computed|algorithm)\b", t):
        return "derived"
    if PRO_INSTRUMENTS.search(f"{instrument or ''} {name}") or re.search(r"\b(score|self-report\w*|in the (last|past))\b", t):
        return "pro"
    return "pro"


def predict_capturability(expected_source: str, instrument: Optional[str] = None) -> str:
    if expected_source == "structured_ehr":
        return "structured"
    if expected_source in {"clinical_note", "exam", "imaging"}:
        return "note_nlp"
    if expected_source == "pro" and (instrument or "").lower() in EHR_SCREENING_INSTRUMENTS:
        return "structured"
    return "unlikely"


def keyword_hits(text: str) -> int:
    t = (text or "").lower()
    return sum(1 for k in TMD_KEYWORDS if k in t)


# ---------------------------------------------------------------------------
# Name normalization and similarity for harmonization
# ---------------------------------------------------------------------------
ABBREVIATIONS = {
    "tmj": "temporomandibular joint", "tmd": "temporomandibular disorder", "muo": "maximum unassisted opening",
    "mao": "maximum assisted opening", "rom": "range of motion", "jfls": "jaw functional limitation scale",
    "obc": "oral behaviors checklist", "gcps": "graded chronic pain scale", "cpi": "characteristic pain intensity",
    "pgic": "patient global impression of change", "ppt": "pressure pain threshold", "hx": "history",
    "dx": "diagnosis", "tx": "treatment", "yrs": "years", "max": "maximum",
    "phq": "patient health questionnaire", "gad": "generalized anxiety disorder", "pcs": "pain catastrophizing scale",
    "bpi": "brief pain inventory",
}
STOPWORDS = {"the", "of", "and", "in", "a", "an", "to", "for", "on", "with", "or", "at", "by", "is", "was", "during"}


def normalize_tokens(text: str) -> list[str]:
    t = (text or "").lower().replace("-", " ").replace("/", " ")
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    out = []
    for tok in t.split():
        out.extend(ABBREVIATIONS.get(tok, tok).split())
    return [w for w in out if w not in STOPWORDS]


def instrument_key(instrument: Optional[str]) -> str:
    if not instrument:
        return ""
    t = " ".join(normalize_tokens(instrument))
    t = t.replace("jaw functional limitation scale", "jfls")
    return re.sub(r"[^a-z0-9]", "", t)


def name_similarity(a: str, b: str) -> float:
    """Blend of token Jaccard and character similarity. Names where each side has its own distinguishing
    words (e.g. 'maximum UNASSISTED opening' vs 'maximum ASSISTED opening') are penalized."""
    ta, tb = normalize_tokens(a), normalize_tokens(b)
    if not ta or not tb:
        return 0.0
    sa, sb = set(ta), set(tb)
    jacc = len(sa & sb) / len(sa | sb)
    seq = SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()
    sim = 0.5 * jacc + 0.5 * seq
    if (sa - sb) and (sb - sa) and jacc < 0.8:
        sim *= 0.8
    return round(sim, 3)


def numeric_tokens(text: str) -> set[str]:
    return {t for t in normalize_tokens(text) if t.isdigit()}


def slugify(text: str, max_tokens: int = 4) -> str:
    toks = [t for t in normalize_tokens(text) if t not in {"score", "total"}][:max_tokens]
    return "_".join(toks) or "element"


def make_variable_name(hint: Optional[str], name: str, taken: set[str]) -> str:
    base = (hint or slugify(name)).lower()
    base = re.sub(r"[^a-z0-9_]", "_", base)
    base = re.sub(r"_+", "_", base).strip("_") or "element"
    if not base[0].isalpha():
        base = f"v_{base}"
    base = base[:26]
    candidate, i = base, 2
    while candidate in taken:
        suffix = f"_{i}"
        candidate = f"{base[:26 - len(suffix)]}{suffix}"
        i += 1
    taken.add(candidate)
    return candidate
