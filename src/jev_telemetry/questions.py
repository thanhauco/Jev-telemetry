"""Question packs: load from YAML, plus the built-in pairwise questions used by L3."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml

from .models import QType, Question

MAX_CHOICE_OPTIONS = 255


@dataclass(frozen=True)
class QuestionPack:
    questions: tuple[Question, ...]
    version: int = 1

    def __iter__(self):
        return iter(self.questions)

    def __len__(self) -> int:
        return len(self.questions)

    def get(self, key: str) -> Question:
        for q in self.questions:
            if q.key == key:
                return q
        raise KeyError(key)

    def keys(self) -> list[str]:
        return [q.key for q in self.questions]

    def fingerprint(self) -> str:
        return hashlib.sha256("".join(q.fingerprint() for q in self.questions).encode()).hexdigest()[:12]


def parse_question(key: str, spec: dict) -> Question:
    qtype = QType(spec["type"])
    criteria = spec.get("criteria")
    if qtype is QType.CHOICE:
        if not isinstance(criteria, dict) or len(criteria) < 2:
            raise ValueError(f"choice question {key!r} needs a criteria mapping with at least 2 options")
        if len(criteria) > MAX_CHOICE_OPTIONS:
            raise ValueError(f"choice question {key!r} has {len(criteria)} options; Jev allows {MAX_CHOICE_OPTIONS}")
        criteria = {str(k): str(v) for k, v in criteria.items()}
    elif qtype is QType.SCORE:
        if not isinstance(criteria, list) or len(criteria) < 2:
            raise ValueError(f"score question {key!r} needs an ordered list of at least 2 levels")
        criteria = tuple(str(c) for c in criteria)
    else:
        criteria = None
    return Question(key=key, type=qtype, instructions=" ".join(str(spec["instructions"]).split()), criteria=criteria)


def load_pack(path: str | Path) -> QuestionPack:
    data = yaml.safe_load(Path(path).read_text())
    qs = tuple(parse_question(k, v) for k, v in (data.get("questions") or {}).items())
    if not qs:
        raise ValueError(f"no questions in {path}")
    return QuestionPack(questions=qs, version=int(data.get("version", 1)))


def runbook_question(runbooks: dict[str, str]) -> Question:
    """Choice over runbooks or owning teams (use case 11). A `none` option is always added."""
    options = dict(runbooks)
    options.setdefault("none", "None of the listed runbooks applies.")
    if len(options) > MAX_CHOICE_OPTIONS:
        raise ValueError(f"{len(options)} runbooks exceeds Jev's {MAX_CHOICE_OPTIONS}-option limit")
    return Question(
        key="runbook",
        type=QType.CHOICE,
        instructions=(
            "This is a summary of one recurring log message pattern from a production service. "
            "Which runbook should an on-call engineer follow for it?"
        ),
        criteria=options,
    )


# L3 pairwise and hypothesis questions. State shapes are documented on each builder in correlate.py.
SAME_ISSUE = Question(
    key="same_issue",
    type=QType.NOUL,
    instructions=(
        "The state holds two log patterns, `a` and `b`, observed close together in time. "
        "Are they most likely caused by the same underlying problem?"
    ),
)

CHANGE_RELATED = Question(
    key="change_related",
    type=QType.NOUL,
    instructions=(
        "The state holds a `change` (a deploy or configuration change) and an error log pattern that "
        "started after it. Is the change a plausible cause of the error pattern?"
    ),
)

HYPOTHESIS_SUPPORTED = Question(
    key="hypothesis_supported",
    type=QType.NOUL,
    instructions=(
        "The state holds an incident `hypothesis` and an `evidence` bundle of log patterns and "
        "changes. Is the hypothesis consistent with and supported by the evidence?"
    ),
)

TIMELINE_EVENT = Question(
    key="event_type",
    type=QType.CHOICE,
    instructions="The state is one event from an incident. What kind of event is it?",
    criteria={
        "trigger": "A change or external event that could have started the incident.",
        "first_error": "The first sign that something is wrong.",
        "escalation": "The problem spreads to more services or gets worse.",
        "mitigation": "An action taken to reduce impact, such as a rollback or failover.",
        "recovery": "Signals that the system is returning to normal.",
        "other": "None of the above.",
    },
)

TIMELINE_MILESTONE = Question(
    key="milestone",
    type=QType.NOUL,
    instructions="The state is one event from an incident. Would it belong on a short incident timeline?",
)
