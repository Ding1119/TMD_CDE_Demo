"""Runtime settings, read from environment variables (see README).

Every external service is optional. With no API keys and TMD_CDE_ONLINE=0 the
platform runs fully offline on the bundled NIH/DC-TMD seed knowledge, using
rule-based agents instead of an LLM.
"""
from __future__ import annotations

from typing import Optional

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines, '#' comments); real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split(" #", 1)[0].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if value.strip():
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")
DATA_DIR = Path(os.getenv("TMD_CDE_DATA_DIR", str(BASE_DIR / "data")))
SEED_DIR = Path(__file__).resolve().parent / "seed"


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    db_url: str
    # LLM: "auto" uses Claude when ANTHROPIC_API_KEY is set, "none" forces rule-based agents
    llm_provider: str
    llm_model: str
    # Allow calls to public APIs (PubMed E-utilities, NLM Clinical Tables, EBI OLS, UMLS, NLM CDE)
    online: bool
    # Whether a TMD EHR extract / concept set is available yet (changes source weights)
    ehr_available: bool
    ncbi_api_key: Optional[str]
    ncbi_email: Optional[str]
    umls_api_key: Optional[str]
    nlm_cde_search_url: str
    # Delphi consensus rule: share of raters scoring 7-9 (on a 1-9 scale) needed for consensus
    delphi_threshold: float
    delphi_min_raters: int
    # shown in the page header
    org_name: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_url=os.getenv("TMD_CDE_DB_URL", f"sqlite:///{DATA_DIR / 'tmd_cde.db'}"),
            llm_provider=os.getenv("TMD_CDE_LLM_PROVIDER", "auto").lower(),
            llm_model=os.getenv("TMD_CDE_LLM_MODEL", "claude-sonnet-5-5"),
            online=_flag("TMD_CDE_ONLINE"),
            ehr_available=_flag("TMD_CDE_EHR_AVAILABLE"),
            ncbi_api_key=os.getenv("NCBI_API_KEY") or None,
            ncbi_email=os.getenv("NCBI_EMAIL") or None,
            umls_api_key=os.getenv("UMLS_API_KEY") or None,
            # Best-effort endpoint; if NLM changes it, export from cde.nlm.nih.gov and upload the file instead.
            nlm_cde_search_url=os.getenv("NLM_CDE_SEARCH_URL", "https://cde.nlm.nih.gov/server/de/search"),
            delphi_threshold=float(os.getenv("TMD_CDE_DELPHI_THRESHOLD", "0.70")),
            delphi_min_raters=int(os.getenv("TMD_CDE_DELPHI_MIN_RATERS", "3")),
            org_name=os.getenv("TMD_CDE_ORG_NAME", "Biomedical Data Science Core"),
        )


settings = Settings.from_env()
