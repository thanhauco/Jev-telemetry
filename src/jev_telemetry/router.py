"""L2: deterministic router. Probabilities in, decisions out.

Guardrails encoded here:
- Never suppress on a single probability: suppression needs a high `benign`, a low `actionable`,
  and a deterministic rules check, and it is never applied to ERROR/FATAL or to fallback answers.
- Probabilities near a threshold go to REVIEW, the cascade's next tier (LLM or human).
- Suppressed clusters are counted, never silently dropped.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .models import Answer, Cluster, QType

# Scrubber hit kinds that mean the logging call itself is a bug: credentials or payment data.
HIGH_RISK_SCRUB_HITS = frozenset({"CARD", "SSN", "API_KEY", "AWS_KEY", "JWT", "PRIVATE_KEY", "SECRET", "BEARER"})


class Decision(str, Enum):
    ESCALATE = "escalate"        # novel or high-impact: goes to L3 correlation and L4 narrative
    TRIAGE = "triage"            # actionable but not urgent: queue for owners
    KNOWN_ISSUE = "known_issue"  # matches a runbook or open ticket
    REVIEW = "review"            # uncertain: send to the next tier of the cascade
    MONITOR = "monitor"          # visible in the triage view, no action
    SUPPRESS = "suppress"        # benign noise: hidden, counted


@dataclass
class RouterConfig:
    suppress_benign_min: float = 0.95
    suppress_actionable_max: float = 0.10
    never_suppress_levels: tuple[str, ...] = ("ERROR", "FATAL")
    # Rules check for suppression: the template matches the allowlist, or it is repetitive chatter.
    suppress_min_count: int = 20
    noise_allowlist: tuple[str, ...] = (r"health", r"heartbeat", r"readyz|livez", r"cache hit", r"metrics")
    escalate_actionable_min: float = 0.6
    escalate_novel_min: float = 0.5
    escalate_severity_min: float = 2.5  # expected severity level, E[level]
    known_issue_runbook_min: float = 0.7
    sensitive_flag_min: float = 0.5
    uncertainty_band: float = 0.1
    known_issues: dict[str, str] = field(default_factory=dict)  # template_id -> ticket or runbook URL

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> RouterConfig:
        d = dict(d or {})
        for k in ("never_suppress_levels", "noise_allowlist"):
            if k in d:
                d[k] = tuple(d[k])
        return cls(**d)


@dataclass
class Routed:
    cluster: Cluster
    answers: dict[str, Answer]
    decision: Decision
    reasons: list[str]
    flags: list[str]
    priority: float

    def p(self, key: str, default: float = 0.0) -> float:
        a = self.answers.get(key)
        return a.p_yes if a is not None and a.type is QType.NOUL else default

    def choice(self, key: str) -> str | None:
        a = self.answers.get(key)
        return str(a.value) if a is not None else None

    def expected(self, key: str) -> float | None:
        a = self.answers.get(key)
        return a.expected_score() if a is not None and a.type is QType.SCORE else None


class Router:
    def __init__(self, config: RouterConfig | None = None):
        self.cfg = config or RouterConfig()
        self._allow = [re.compile(p, re.IGNORECASE) for p in self.cfg.noise_allowlist]

    def rules_allow_suppress(self, c: Cluster) -> bool:
        if c.level in self.cfg.never_suppress_levels:
            return False
        if any(p.search(c.template) for p in self._allow):
            return True
        return c.count >= self.cfg.suppress_min_count

    def route(self, cluster: Cluster, answers: dict[str, Answer]) -> Routed:
        cfg = self.cfg
        reasons: list[str] = []
        flags: list[str] = []

        def p(key: str) -> float | None:
            a = answers.get(key)
            return a.p_yes if a is not None and a.type is QType.NOUL else None

        actionable, benign, novel, sensitive = p("actionable"), p("benign"), p("novel"), p("sensitive")
        sev_a = answers.get("severity")
        severity = sev_a.expected_score() if sev_a is not None else None
        any_fallback = any(a.source == "fallback" for a in answers.values())
        if any_fallback:
            flags.append("fallback_answers")
        if sensitive is not None and sensitive >= cfg.sensitive_flag_min:
            flags.append("sensitive_data")
            reasons.append(f"P(sensitive)={sensitive:.2f}: keep examples out of L4 and fix the scrubber")
        if cluster.sensitive_hits:
            flags.append("scrubbed")

        priority = _priority(cluster, actionable, severity, novel)
        mk = lambda d: Routed(cluster, answers, d, reasons, flags, priority)

        if actionable is None or benign is None:
            reasons.append("missing core answers (actionable/benign)")
            return mk(Decision.REVIEW)

        ticket = cfg.known_issues.get(cluster.template_id)
        if ticket:
            reasons.append(f"known issue: {ticket}")
            return mk(Decision.KNOWN_ISSUE)
        rb = answers.get("runbook")
        rb_p = rb.probabilities.get(str(rb.value), 0) if rb is not None else 0
        if rb is not None and rb.value != "none" and rb_p >= cfg.known_issue_runbook_min:
            reasons.append(f"runbook {rb.value} (p={rb.probabilities[str(rb.value)]:.2f})")
            return mk(Decision.KNOWN_ISSUE)

        leaked = sorted(HIGH_RISK_SCRUB_HITS & set(cluster.sensitive_hits))
        escalate_worthy = actionable >= cfg.escalate_actionable_min and (
            (novel is not None and novel >= cfg.escalate_novel_min)
            or (severity is not None and severity >= cfg.escalate_severity_min)
        )
        if leaked and not escalate_worthy:
            reasons.append(f"scrubber caught {', '.join(leaked)}: fix the logging call at the source")
            return mk(Decision.TRIAGE)

        if benign >= cfg.suppress_benign_min and actionable <= cfg.suppress_actionable_max:
            if any_fallback:
                reasons.append("would suppress, but answers came from the fallback judge")
            elif "sensitive_data" in flags:
                reasons.append("would suppress, but the cluster may contain sensitive data")
            elif not self.rules_allow_suppress(cluster):
                reasons.append("would suppress, but the rules check did not pass")
            else:
                reasons.append(f"P(benign)={benign:.2f}, P(actionable)={actionable:.2f}, rules check passed")
                return mk(Decision.SUPPRESS)

        if actionable >= cfg.escalate_actionable_min:
            hot = []
            if novel is not None and novel >= cfg.escalate_novel_min:
                hot.append(f"P(novel)={novel:.2f}")
            if severity is not None and severity >= cfg.escalate_severity_min:
                hot.append(f"E[severity]={severity:.1f}")
            if hot:
                reasons.append(f"P(actionable)={actionable:.2f}, " + ", ".join(hot))
                return mk(Decision.ESCALATE)
            reasons.append(f"P(actionable)={actionable:.2f}")
            return mk(Decision.TRIAGE)

        band = cfg.uncertainty_band
        if abs(actionable - cfg.escalate_actionable_min) <= band or abs(benign - cfg.suppress_benign_min) <= band:
            reasons.append(f"near threshold: P(actionable)={actionable:.2f}, P(benign)={benign:.2f}")
            return mk(Decision.REVIEW)

        reasons.append(f"P(actionable)={actionable:.2f}, P(benign)={benign:.2f}")
        return mk(Decision.MONITOR)


def _priority(c: Cluster, actionable: float | None, severity: float | None, novel: float | None) -> float:
    return round((actionable or 0) * (1 + (severity or 0)) * (1 + (novel or 0)) * math.log1p(c.count), 4)


def noisy_or(ps: list[float]) -> float:
    """P(at least one event is true), treating events as independent."""
    q = 1.0
    for p in ps:
        q *= 1 - min(max(p, 0.0), 1.0)
    return 1 - q


@dataclass
class WindowSummary:
    clusters: int
    lines: int
    decisions: dict[str, int]
    suppressed_lines: int
    expected_actionable_clusters: float
    p_any_severe: float
    categories: dict[str, int]

    @property
    def alert_reduction_ratio(self) -> float:
        surfaced = self.decisions.get("escalate", 0) + self.decisions.get("triage", 0)
        return self.clusters / surfaced if surfaced else float(self.clusters)

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "alert_reduction_ratio": round(self.alert_reduction_ratio, 2)}


def summarize(routed: list[Routed], severe_level: int = 3) -> WindowSummary:
    """Deterministic reduce step: expected counts and noisy-OR over the window."""
    decisions = Counter(r.decision.value for r in routed)
    categories = Counter(r.choice("category") or "unknown" for r in routed)
    p_severe = []
    for r in routed:
        sev = r.answers.get("severity")
        if sev is not None and sev.probabilities:
            p_severe.append(sum(p for k, p in sev.probabilities.items() if int(k) >= severe_level))
    return WindowSummary(
        clusters=len(routed),
        lines=sum(r.cluster.count for r in routed),
        decisions=dict(decisions),
        suppressed_lines=sum(r.cluster.count for r in routed if r.decision is Decision.SUPPRESS),
        expected_actionable_clusters=round(sum(r.p("actionable") for r in routed), 3),
        p_any_severe=round(noisy_or(p_severe), 4),
        categories=dict(categories),
    )
