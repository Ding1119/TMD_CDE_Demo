"""JSON API (interactive docs at /docs)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..agents.orchestrator import create_run, execute_run
from ..db import get_db
from ..models import CDE, PipelineRun, Review
from ..services.exporters import cde_full, coverage_report

router = APIRouter(prefix="/api", tags=["api"])


class RunOptions(BaseModel):
    agents: Optional[list[str]] = Field(None, description="Subset of agents; default = all")
    use_llm: bool = True
    online: bool = True
    min_relevance: float = 0.35
    pubmed_query: Optional[str] = None
    pubmed_max: int = 20


class ReviewIn(BaseModel):
    reviewer: str
    role: Optional[str] = None
    round: int = 1
    importance: int = Field(ge=1, le=9)
    feasibility: int = Field(ge=1, le=9)
    comment: Optional[str] = None


def _get_cde(db: Session, key: str) -> CDE:
    cde = db.scalar(select(CDE).where(or_(CDE.cde_code == key, CDE.variable_name == key)))
    if cde is None and key.isdigit():
        cde = db.get(CDE, int(key))
    if cde is None:
        raise HTTPException(404, f"CDE {key} not found")
    return cde


@router.get("/stats")
def stats(db: Session = Depends(get_db)):
    return coverage_report(db)


@router.get("/cdes")
def list_cdes(q: Optional[str] = None, domain: Optional[str] = None, tier: Optional[str] = None, status: Optional[str] = None,
              db: Session = Depends(get_db)):
    stmt = select(CDE).order_by(CDE.cde_code)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(CDE.name.ilike(like), CDE.definition.ilike(like), CDE.variable_name.ilike(like)))
    for col, val in ((CDE.domain, domain), (CDE.tier, tier), (CDE.status, status)):
        if val:
            stmt = stmt.where(col == val)
    return [cde_full(c) for c in db.scalars(stmt)]


@router.get("/cdes/{key}")
def get_cde(key: str, db: Session = Depends(get_db)):
    return cde_full(_get_cde(db, key))


@router.post("/cdes/{key}/reviews", status_code=201)
def add_review(key: str, body: ReviewIn, db: Session = Depends(get_db)):
    cde = _get_cde(db, key)
    db.add(Review(cde=cde, **body.model_dump()))
    db.commit()
    return cde_full(cde)["delphi"]


@router.post("/pipeline/run", status_code=202)
def start_run(opts: RunOptions, background: BackgroundTasks, db: Session = Depends(get_db)):
    run = create_run(db, opts.model_dump(exclude_none=True))
    background.add_task(execute_run, run.id)
    return {"run_id": run.id, "status": run.status}


@router.get("/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(PipelineRun, run_id)
    if run is None:
        raise HTTPException(404)
    return {"id": run.id, "status": run.status, "config": run.config, "summary": run.summary,
            "logs": [{"agent": l.agent, "level": l.level, "message": l.message, "at": l.created_at.isoformat()}
                     for l in run.logs]}
