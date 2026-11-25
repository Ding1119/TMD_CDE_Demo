"""Connector for the bundled NIH HEAL / DC-TMD / curated seed knowledge."""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..seed.nih_tmd_seed import DOCUMENTS
from .base import DocPayload, ensure_sources, upsert_document
from .ehr_cdm import ehr_cdm_payloads


def seed_payloads() -> list[tuple[str, DocPayload]]:
    out = []
    for d in DOCUMENTS:
        out.append(
            (
                d["source_key"],
                DocPayload(
                    external_id=d["external_id"],
                    title=d["title"],
                    uri=d.get("uri"),
                    doc_type="structured_cde",
                    records=d["records"],
                    meta=d.get("meta", {}),
                ),
            )
        )
    return out


def load_seed_documents(db: Session) -> dict[str, int]:
    """Load seed CDE documents and public EHR data-model dictionaries. Idempotent."""
    sources = ensure_sources(db)
    created = updated = 0
    for key, payload in seed_payloads() + [("ehr_cdm", p) for p in ehr_cdm_payloads()]:
        _, is_new = upsert_document(db, sources[key], payload)
        created += int(is_new)
        updated += int(not is_new)
    db.commit()
    return {"created": created, "refreshed": updated}
