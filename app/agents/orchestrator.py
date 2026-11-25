"""Orchestrator Agent: task planning, agent coordination and quality control (slide 7, top)."""
from __future__ import annotations

from typing import Optional

import traceback

from ..db import SessionLocal
from ..models import PipelineRun, QAIssue, utcnow
from .base import AgentContext
from .ehr_readiness import EHRReadinessAgent
from .extraction import ExtractionAgent
from .harmonization import HarmonizationAgent
from .llm import LLMClient
from .quality_assurance import QualityAssuranceAgent
from .source_discovery import SourceDiscoveryAgent
from .terminology_mapping import TerminologyMappingAgent

AGENTS = [
    SourceDiscoveryAgent(),
    ExtractionAgent(),
    TerminologyMappingAgent(),
    HarmonizationAgent(),
    EHRReadinessAgent(),
    QualityAssuranceAgent(),
]
AGENT_BY_NAME = {a.name: a for a in AGENTS}


def plan(options: dict) -> list[str]:
    """Steps to run, in dependency order. QA always runs last so every run ends with a quality gate."""
    requested = options.get("agents") or [a.name for a in AGENTS]
    steps = [a.name for a in AGENTS if a.name in requested]
    if "quality_assurance" not in steps:
        steps.append("quality_assurance")
    return steps


def create_run(db, options: dict) -> PipelineRun:
    run = PipelineRun(status="queued", config=options)
    db.add(run)
    db.commit()
    return run


def execute_run(run_id: int) -> None:
    """Run a queued pipeline in its own DB session (safe for background tasks)."""
    db = SessionLocal()
    try:
        run = db.get(PipelineRun, run_id)
        options = run.config or {}
        llm = LLMClient(enabled=bool(options.get("use_llm", True)))
        ctx = AgentContext(db=db, run=run, llm=llm, options=options)
        steps = plan(options)
        run.status = "running"
        db.commit()
        ctx.log("orchestrator", f"Plan: {' -> '.join(steps)} (LLM: {'on, ' + llm.model if llm.enabled else 'off'})")
        summary: dict = {}
        for step in steps:
            agent = AGENT_BY_NAME[step]
            try:
                result = agent.run(ctx)
                summary[step] = result.stats
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                ctx.log("orchestrator", f"{agent.title} failed: {exc}", level="error", trace=traceback.format_exc()[-2000:])
                run.status, run.summary, run.finished_at = "failed", summary, utcnow()
                db.commit()
                return
        open_errors = db.query(QAIssue).filter(QAIssue.severity == "error", QAIssue.resolved.is_(False)).count()
        run.status = "needs_review" if open_errors else "completed"
        summary["quality_gate"] = {"open_errors": open_errors}
        run.summary, run.finished_at = summary, utcnow()
        db.commit()
        ctx.log("orchestrator", f"Run finished: {run.status} ({open_errors} open QA errors)")
    finally:
        db.close()


def run_pipeline(options: Optional[dict] = None) -> int:
    """Synchronous helper used by the CLI and tests."""
    db = SessionLocal()
    try:
        run = create_run(db, options or {})
        run_id = run.id
    finally:
        db.close()
    execute_run(run_id)
    return run_id
