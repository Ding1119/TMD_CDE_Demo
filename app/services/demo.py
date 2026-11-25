"""Synthetic demo data for trying the review workflow (never use for real decisions)."""
from __future__ import annotations

import random

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CDE, Review
from .consensus import delphi_summary_for
from .versioning import record_version

PANEL = [("Expert A", "Orofacial pain dentist"), ("Expert B", "Pain psychologist"),
         ("Expert C", "Clinical informatician"), ("Expert D", "Patient partner")]


def add_demo_reviews(db: Session, seed: int = 7) -> int:
    """One synthetic Delphi round: core elements rated higher, EHR-capturable ones more feasible."""
    rng = random.Random(seed)
    cdes = db.scalars(select(CDE).where(CDE.status != "retired")).all()
    for c in cdes:
        if c.reviews:
            continue
        for who, role in PANEL:
            base = 8 if c.tier == "core" else 6
            feas = 7 if c.ehr_capturability == "structured" else 5 if c.ehr_capturability == "note_nlp" else 4
            db.add(Review(cde=c, round=1, reviewer=who, role=role, comment="synthetic demo rating",
                          importance=max(1, min(9, base + rng.randint(-2, 1))),
                          feasibility=max(1, min(9, feas + rng.randint(-2, 2)))))
    db.commit()
    return len(cdes)


def apply_demo_decisions(db: Session) -> dict:
    """Move rated CDEs to under_review and approve core CDEs that reached consensus without QA errors."""
    counts = {"under_review": 0, "approved": 0}
    for c in db.scalars(select(CDE).where(CDE.status == "draft")).all():
        if not c.reviews:
            continue
        has_error = any(q.severity == "error" and not q.resolved for q in c.qa_issues)
        if c.tier == "core" and not has_error and delphi_summary_for(c)["decision"] == "include":
            c.status = "approved"
            record_version(db, c, "Approved after Delphi round 1 (synthetic demo panel)", author="demo panel", part="minor")
            counts["approved"] += 1
        else:
            c.status = "under_review"
            record_version(db, c, "Sent to expert review", author="demo panel")
            counts["under_review"] += 1
    db.commit()
    return counts
