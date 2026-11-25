"""Shared helpers for knowledge-source connectors."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import KnowledgeSource, SourceDocument
from ..seed.nih_tmd_seed import SOURCES


@dataclass
class DocPayload:
    """A document as returned by a connector or importer, before it is stored."""

    external_id: str
    title: str
    doc_type: str  # structured_cde | dictionary | ehr_dictionary | text
    uri: Optional[str] = None
    records: list[dict[str, Any]] = field(default_factory=list)  # structured docs
    text: str = ""  # unstructured docs
    meta: dict[str, Any] = field(default_factory=dict)

    def content(self) -> str:
        return json.dumps(self.records, ensure_ascii=False) if self.records else self.text


def ensure_sources(db: Session) -> dict[str, KnowledgeSource]:
    """Create/refresh the knowledge-source registry. EHR dictionaries get a higher weight once EHR data exist."""
    existing = {s.key: s for s in db.scalars(select(KnowledgeSource))}
    for spec in SOURCES:
        src = existing.get(spec["key"])
        weight = spec["weight"]
        if spec["key"] == "ehr_cdm" and settings.ehr_available:
            weight = 0.9
        if src is None:
            src = KnowledgeSource(**{**spec, "weight": weight})
            db.add(src)
            existing[spec["key"]] = src
        else:
            src.weight = weight
    db.commit()
    return existing


def upsert_document(db: Session, source: KnowledgeSource, payload: DocPayload) -> tuple[SourceDocument, bool]:
    """Insert a document, or refresh it if the same (source, external_id) exists. Returns (doc, created)."""
    doc = db.scalar(
        select(SourceDocument).where(
            SourceDocument.source_id == source.id, SourceDocument.external_id == payload.external_id
        )
    )
    created = doc is None
    if created:
        doc = SourceDocument(source=source, external_id=payload.external_id, status="new")
        db.add(doc)
    new_content = payload.content()
    if not created and doc.content != new_content:
        doc.status = "new"  # content changed -> re-extract
    doc.title = payload.title[:500]
    doc.uri = payload.uri
    doc.doc_type = payload.doc_type
    doc.content = new_content
    doc.meta = payload.meta or {}
    db.flush()
    return doc, created


def document_records(doc: SourceDocument) -> list[dict]:
    if doc.doc_type in {"structured_cde", "dictionary", "ehr_dictionary"} and doc.content:
        try:
            data = json.loads(doc.content)
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []
    return []
