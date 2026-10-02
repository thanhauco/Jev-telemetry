"""The judge interface. Everything above L1 talks to this, never to a vendor client directly.

Keeping Jev behind this protocol is the main vendor-maturity guardrail: an LLM, a local classifier
or the offline heuristic judge can be swapped in without touching the pipeline.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..models import Answer, Question


class JudgeError(RuntimeError):
    def __init__(self, message: str, retryable: bool = True, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


@dataclass
class Usage:
    input_tokens: int = 0
    requests: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.requests += other.requests


@dataclass
class Judgment:
    answers: dict[str, Answer]
    usage: Usage = field(default_factory=Usage)
    latency_ms: float = 0.0


@runtime_checkable
class Judge(Protocol):
    name: str
    model_version: str

    async def ask(self, state: dict[str, Any] | str, questions: Sequence[Question]) -> Judgment:
        """Answer every question against the same state. Questions must not see each other."""
        ...

    async def aclose(self) -> None: ...
