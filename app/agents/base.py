"""Agent base class and shared context."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from ..models import AgentLog, PipelineRun
from .llm import LLMClient


@dataclass
class AgentContext:
    db: Session
    run: PipelineRun
    llm: LLMClient
    options: dict[str, Any] = field(default_factory=dict)

    def log(self, agent: str, message: str, level: str = "info", **data: Any) -> None:
        self.db.add(AgentLog(run_id=self.run.id, agent=agent, level=level, message=message, data=data))
        self.db.commit()


@dataclass
class AgentResult:
    agent: str
    stats: dict[str, Any] = field(default_factory=dict)
    ok: bool = True


class Agent:
    name = "agent"
    title = "Agent"
    description = ""

    def run(self, ctx: AgentContext) -> AgentResult:  # pragma: no cover - interface
        raise NotImplementedError
