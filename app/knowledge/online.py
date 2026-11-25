"""Online connectors (used only when TMD_CDE_ONLINE=1).

* PubMed via NCBI E-utilities (esearch + efetch)
* NIH CDE Repository live search (best-effort; endpoint configurable via NLM_CDE_SEARCH_URL)
"""
from __future__ import annotations

import logging
from xml.etree import ElementTree

import httpx

from ..config import settings
from .base import DocPayload
from .importers import parse_nlm_cde_json

log = logging.getLogger(__name__)
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
DEFAULT_PUBMED_QUERY = (
    '"Temporomandibular Joint Disorders"[MeSH] AND (assessment[tiab] OR outcome*[tiab] OR questionnaire[tiab] '
    'OR "common data element*"[tiab] OR "core outcome set"[tiab]) AND hasabstract'
)


def _ncbi_params(extra: dict) -> dict:
    p = {"tool": "tmd-cde-platform", **extra}
    if settings.ncbi_email:
        p["email"] = settings.ncbi_email
    if settings.ncbi_api_key:
        p["api_key"] = settings.ncbi_api_key
    return p


def fetch_pubmed(query: str = DEFAULT_PUBMED_QUERY, max_results: int = 20, timeout: float = 30.0) -> list[DocPayload]:
    with httpx.Client(timeout=timeout) as client:
        r = client.get(f"{EUTILS}/esearch.fcgi",
                       params=_ncbi_params({"db": "pubmed", "term": query, "retmax": max_results, "retmode": "json",
                                            "sort": "relevance"}))
        r.raise_for_status()
        ids = r.json().get("esearchresult", {}).get("idlist", [])
        if not ids:
            return []
        r = client.get(f"{EUTILS}/efetch.fcgi",
                       params=_ncbi_params({"db": "pubmed", "id": ",".join(ids), "rettype": "abstract", "retmode": "xml"}))
        r.raise_for_status()
    root = ElementTree.fromstring(r.content)
    out = []
    for art in root.iter("PubmedArticle"):
        pmid = art.findtext(".//MedlineCitation/PMID") or ""
        title = "".join(art.find(".//ArticleTitle").itertext()) if art.find(".//ArticleTitle") is not None else ""
        parts = []
        for ab in art.findall(".//Abstract/AbstractText"):
            label = ab.get("Label")
            text = "".join(ab.itertext()).strip()
            parts.append(f"{label}: {text}" if label else text)
        journal = art.findtext(".//Journal/Title") or ""
        year = art.findtext(".//JournalIssue/PubDate/Year") or ""
        out.append(DocPayload(
            external_id=f"PMID:{pmid}", title=title or f"PMID {pmid}", doc_type="text",
            uri=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", text=f"{title}\n\n" + "\n".join(parts),
            meta={"pmid": pmid, "journal": journal, "year": year, "query": query},
        ))
    return out


def search_nlm_cde(term: str, max_results: int = 25, timeout: float = 30.0) -> list[DocPayload]:
    """Best-effort live search of the NIH CDE Repository. Falls back to [] on any API change."""
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(settings.nlm_cde_search_url, json={"searchTerm": term, "page": 1, "resultPerPage": max_results})
            r.raise_for_status()
            body = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("NLM CDE live search unavailable (%s). Export results from cde.nlm.nih.gov and upload instead.", exc)
        return []
    objs = body.get("cdes") or body.get("elts") or (body if isinstance(body, list) else [])
    records = parse_nlm_cde_json(objs[:max_results])
    if not records:
        return []
    return [DocPayload(external_id=f"nlm-search:{term}", title=f"NIH CDE Repository search: {term}",
                       doc_type="structured_cde", uri="https://cde.nlm.nih.gov/cde/search", records=records,
                       meta={"query": term})]
