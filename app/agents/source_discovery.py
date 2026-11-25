"""Source Discovery Agent: identify and prioritize relevant CDE sources (slide 7, agent 1)."""
from __future__ import annotations

from sqlalchemy import select

from ..config import settings
from ..knowledge.base import ensure_sources, upsert_document
from ..knowledge.online import DEFAULT_PUBMED_QUERY, fetch_pubmed, search_nlm_cde
from ..knowledge.seed_loader import load_seed_documents
from ..models import SourceDocument
from .base import Agent, AgentContext, AgentResult
from .heuristics import keyword_hits


class SourceDiscoveryAgent(Agent):
    name = "source_discovery"
    title = "Source Discovery Agent"
    description = "Loads the knowledge-source registry, pulls online sources (optional) and ranks documents by relevance."

    def run(self, ctx: AgentContext) -> AgentResult:
        db, opts = ctx.db, ctx.options
        seeded = load_seed_documents(db)
        ctx.log(self.name, f"Seed knowledge loaded: {seeded['created']} new, {seeded['refreshed']} refreshed documents")
        sources = ensure_sources(db)

        online = settings.online and opts.get("online", True)
        pulled = 0
        if online and opts.get("pubmed", True):
            try:
                for p in fetch_pubmed(opts.get("pubmed_query") or DEFAULT_PUBMED_QUERY, int(opts.get("pubmed_max", 20))):
                    upsert_document(db, sources["pubmed"], p)
                    pulled += 1
            except Exception as exc:  # noqa: BLE001
                ctx.log(self.name, f"PubMed retrieval failed: {exc}", level="warning")
        if online and opts.get("nlm_cde", True):
            for term in opts.get("nlm_terms") or ["temporomandibular", "orofacial pain", "jaw"]:
                for p in search_nlm_cde(term):
                    upsert_document(db, sources["nlm_cde"], p)
                    pulled += 1
        if not online:
            ctx.log(self.name, "Offline mode: skipped PubMed / NIH CDE live search (set TMD_CDE_ONLINE=1 to enable)")
        db.commit()

        threshold = float(opts.get("min_relevance", 0.35))
        counts = {"prioritized": 0, "skipped": 0}
        by_kind: dict[str, int] = {}
        docs = db.scalars(select(SourceDocument).where(SourceDocument.status.in_(["new", "prioritized", "skipped"])))
        for doc in docs:
            kw = min(1.0, keyword_hits(f"{doc.title} {doc.content[:20000]}") / 5)
            doc.relevance = round(doc.source.weight * (0.5 + 0.5 * kw), 3)
            # EHR data-model dictionaries are binding targets, always kept for the EHR-readiness agent
            keep = doc.doc_type == "ehr_dictionary" or doc.relevance >= threshold
            doc.status = "prioritized" if keep else "skipped"
            counts[doc.status] += 1
            by_kind[doc.source.kind] = by_kind.get(doc.source.kind, 0) + int(keep)
        db.commit()
        ctx.log(self.name, f"Ranked documents: {counts['prioritized']} prioritized, {counts['skipped']} skipped "
                           f"(EHR available: {settings.ehr_available})", by_kind=by_kind)
        return AgentResult(self.name, {**counts, "online_documents": pulled, "by_kind": by_kind})
