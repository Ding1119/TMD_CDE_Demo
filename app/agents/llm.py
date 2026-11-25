"""Thin, optional LLM wrapper (Anthropic Claude). Agents fall back to rules when disabled."""
from __future__ import annotations

from typing import Optional

import json
import logging
import os
import re

from ..config import settings

log = logging.getLogger(__name__)


class LLMClient:
    def __init__(self, enabled: Optional[bool] = None):
        want = settings.llm_provider != "none" if enabled is None else enabled
        self._client = None
        self.model = settings.llm_model
        if want and os.getenv("ANTHROPIC_API_KEY"):
            try:
                from anthropic import Anthropic

                self._client = Anthropic()
            except ImportError:  # pragma: no cover
                log.warning("anthropic package not installed; using rule-based agents")

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def complete_json(self, system: str, prompt: str, max_tokens: int = 4000):
        """Ask for JSON and parse it; returns None on any failure so callers can fall back."""
        if not self._client:
            return None
        try:
            msg = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system + "\nRespond with valid JSON only, no prose and no code fences.",
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(getattr(b, "text", "") for b in msg.content).strip()
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
            return json.loads(text)
        except Exception as exc:  # noqa: BLE001 - any API/parse error -> fallback
            log.warning("LLM call failed (%s); falling back to rules", exc)
            return None
