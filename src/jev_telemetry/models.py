"""Core data types shared by every layer of the pipeline."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

LEVELS = ["TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"]


def level_rank(level: str) -> int:
    level = (level or "INFO").upper()
    if level == "WARNING":
        level = "WARN"
    if level == "CRITICAL":
        level = "FATAL"
    return LEVELS.index(level) if level in LEVELS else LEVELS.index("INFO")


def normalize_level(level: str) -> str:
    return LEVELS[level_rank(level)]


class QType(str, Enum):
    CHOICE = "choice"
    SCORE = "score"
    NOUL = "noul"


@dataclass(frozen=True)
class Question:
    """One typed Jev question. Questions are asked in isolation, so each must stand alone."""

    key: str
    type: QType
    instructions: str
    # Choice: {option_key: description}. Score: ordered list of level descriptions. Noul: None.
    criteria: dict[str, str] | tuple[str, ...] | None = None

    def to_payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"type": self.type.value, "instructions": self.instructions}
        if self.type is QType.CHOICE:
            body["criteria"] = dict(self.criteria or {})
        elif self.type is QType.SCORE:
            body["criteria"] = list(self.criteria or ())
        return body

    @property
    def options(self) -> list[str]:
        if self.type is QType.CHOICE:
            return list((self.criteria or {}).keys())
        if self.type is QType.SCORE:
            return [str(i) for i in range(len(self.criteria or ()))]
        return ["yes", "no"]

    def fingerprint(self) -> str:
        """Stable hash of the question text and options; part of every cache key."""
        raw = json.dumps({"key": self.key, **self.to_payload()}, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class Answer:
    key: str
    type: QType
    # Choice: option key. Score: level index. Noul: P(yes).
    value: str | int | float
    probabilities: dict[str, float] = field(default_factory=dict)
    confidence: float | None = None
    source: str = "jev"  # jev | cache | heuristic | fallback

    @property
    def p_yes(self) -> float:
        if self.type is not QType.NOUL:
            raise TypeError(f"{self.key} is a {self.type.value}, not a noul")
        return float(self.value)

    def expected_score(self) -> float:
        """Probability-weighted score level (E[level]); falls back to the argmax."""
        if self.type is not QType.SCORE:
            raise TypeError(f"{self.key} is not a score")
        if self.probabilities:
            return sum(int(k) * p for k, p in self.probabilities.items())
        return float(self.value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "type": self.type.value,
            "value": self.value,
            "probabilities": self.probabilities,
            "confidence": self.confidence,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Answer:
        return cls(
            key=d["key"],
            type=QType(d["type"]),
            value=d["value"],
            probabilities=d.get("probabilities") or {},
            confidence=d.get("confidence"),
            source=d.get("source", "jev"),
        )


@dataclass
class LogRecord:
    ts: datetime
    service: str
    level: str
    body: str
    trace_id: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass
class Cluster:
    """A mined log template plus its aggregate statistics. This is what Jev judges, not raw lines."""

    template_id: str
    service: str
    template: str
    level: str
    count: int = 0
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    examples: list[str] = field(default_factory=list)
    trace_ids: list[str] = field(default_factory=list)
    level_counts: dict[str, int] = field(default_factory=dict)
    sensitive_hits: dict[str, int] = field(default_factory=dict)

    def to_state(self, max_examples: int = 3) -> dict[str, Any]:
        """Text-only JSON state sent to the judge. Examples are already scrubbed at L0."""
        return {
            "service": self.service,
            "level": self.level,
            "template": self.template,
            "occurrences": self.count,
            "first_seen": self.first_seen.isoformat() if self.first_seen else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "examples": self.examples[:max_examples],
        }


@dataclass
class Change:
    """A deploy or config change, used for change correlation (L3)."""

    ts: datetime
    service: str
    summary: str
    change_id: str = ""
