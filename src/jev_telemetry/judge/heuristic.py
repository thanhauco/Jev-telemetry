"""Deterministic offline judge.

Two jobs:
1. The fallback every Jev-backed action needs when the API is down, rate-limited or over budget.
2. A free stand-in for running the whole pipeline, tests and demos without an API key.

It is keyword and level based. Its probabilities are rough and not calibrated, so never tune
production thresholds against it; tune against Jev on your golden set.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from typing import Any

from ..models import Answer, QType, Question, level_rank
from ..reduce.scrub import Scrubber
from .base import Judgment, Usage

_WORD = re.compile(r"[a-z][a-z0-9_]+")

CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "timeout": ("timeout", "timed out", "deadline", "took too long", "504"),
    "auth": ("unauthorized", "forbidden", "401", "403", "invalid token", "expired token", "denied", "login failed"),
    "capacity": ("exhausted", "out of memory", "oom", "quota", "rate limit", "429", "queue full", "disk full",
                 "too many connections", "pool"),
    "dependency": ("refused", "unreachable", "upstream", "503", "502", "bad gateway", "unavailable", "dependency",
                   "connection reset"),
    "config": ("config", "missing env", "feature flag", "misconfigured", "invalid setting", "not set"),
    "data": ("validation", "malformed", "parse error", "invalid json", "constraint", "duplicate key", "schema"),
    "code_bug": ("nullpointer", "null pointer", "typeerror", "keyerror", "undefined", "panic", "assert",
                 "traceback", "unhandled exception", "stack trace", "attributeerror"),
    "infra": ("dns", "node", "pod", "container", "evicted", "crashloop", "kernel", "network unreachable"),
}

COMPONENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "edge": ("gateway", "ingress", "nginx", "load balancer", "cdn", "envoy"),
    "database": ("db", "database", "postgres", "mysql", "sql", "pool", "query", "deadlock"),
    "cache": ("cache", "redis", "memcached"),
    "queue": ("queue", "kafka", "consumer", "producer", "job", "worker", "sqs"),
    "external_api": ("upstream", "api call", "http client", "calling", "stripe", "provider", "external"),
    "platform": ("pod", "node", "container", "dns", "kubelet", "oom", "disk"),
}

NOISE_KEYWORDS = (
    "health", "healthz", "readyz", "heartbeat", "ping", "cache hit", "request completed", "metrics scraped",
    "connection established", "started", "listening on", "retry succeeded", "user logged in", "gc pause",
    "scheduled job finished", "status=200", "200 ok",
)
ERROR_KEYWORDS = ("error", "exception", "fail", "failed", "refused", "timeout", "exhausted", "panic", "denied",
                  "unavailable", "crash", "fatal")
ROOT_CAUSE_KEYWORDS = ("exhausted", "out of memory", "oom", "disk full", "config", "nullpointer", "panic",
                       "deadlock", "invalid", "missing", "certificate expired", "migration")
SYMPTOM_KEYWORDS = ("upstream", "timeout calling", "502", "503", "504", "bad gateway", "dependency",
                    "retrying", "circuit breaker open", "downstream")
USER_FACING = ("checkout", "login", "payment", "order", "signup", "cart", "5xx", "500", "502", "503", "504")


def _softmax(scores: dict[str, float], temp: float = 1.0) -> dict[str, float]:
    m = max(scores.values())
    exps = {k: math.exp((v - m) / temp) for k, v in scores.items()}
    z = sum(exps.values())
    return {k: round(v / z, 6) for k, v in exps.items()}


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def _confidence(probs: dict[str, float]) -> float:
    n = len(probs)
    return (n * max(probs.values()) - 1) / (n - 1) if n > 1 else 1.0


def _text(state: dict[str, Any] | str) -> str:
    return (state if isinstance(state, str) else json.dumps(state, default=str)).lower()


def _hits(text: str, words: Sequence[str]) -> int:
    return sum(1 for w in words if w in text)


def _tokens(s: str) -> set[str]:
    return set(_WORD.findall(s.lower()))


class HeuristicJudge:
    name = "heuristic"
    model_version = "heuristic-1"

    def __init__(self):
        self._scrubber = Scrubber()

    async def ask(self, state: dict[str, Any] | str, questions: Sequence[Question]) -> Judgment:
        return Judgment(answers={q.key: self.answer(state, q) for q in questions}, usage=Usage(0, 1))

    async def aclose(self) -> None:
        return None

    # -- dispatch ---------------------------------------------------------------------------------

    def answer(self, state: dict[str, Any] | str, q: Question) -> Answer:
        fn = getattr(self, f"_q_{q.key}", None)
        if fn is not None:
            return fn(state, q)
        return self._generic(state, q)

    def _noul(self, q: Question, p: float) -> Answer:
        p = min(max(p, 0.01), 0.99)
        return Answer(q.key, QType.NOUL, round(p, 4), {"yes": p, "no": 1 - p}, source="heuristic")

    def _dist(self, q: Question, probs: dict[str, float]) -> Answer:
        best = max(probs, key=probs.get)
        value: str | int = best if q.type is QType.CHOICE else int(best)
        return Answer(q.key, q.type, value, probs, round(_confidence(probs), 4), source="heuristic")

    def _generic(self, state: dict[str, Any] | str, q: Question) -> Answer:
        if q.type is QType.NOUL:
            return self._noul(q, 0.5)
        text = _text(state)
        if q.type is QType.CHOICE:
            # Token overlap between the state and each option's description.
            st = _tokens(text)
            scores = {k: len(st & _tokens(f"{k} {v}")) for k, v in (q.criteria or {}).items()}
            return self._dist(q, _softmax({k: float(v) for k, v in scores.items()}, temp=0.7))
        n = len(q.criteria or ())
        return self._dist(q, {str(i): 1 / n for i in range(n)})

    # -- cluster features -------------------------------------------------------------------------

    @staticmethod
    def _features(state: dict[str, Any] | str) -> dict[str, Any]:
        text = _text(state)
        level = state.get("level", "INFO") if isinstance(state, dict) else "INFO"
        count = int(state.get("occurrences", 1)) if isinstance(state, dict) else 1
        return {
            "text": text,
            "rank": level_rank(level),
            "count": count,
            "noise": _hits(text, NOISE_KEYWORDS),
            "err": _hits(text, ERROR_KEYWORDS),
            "rc": _hits(text, ROOT_CAUSE_KEYWORDS),
            "sym": _hits(text, SYMPTOM_KEYWORDS),
            "user": _hits(text, USER_FACING),
        }

    def _actionable_logit(self, f: dict[str, Any]) -> float:
        return -2.6 + 1.1 * max(f["rank"] - 2, 0) + 0.7 * min(f["err"], 3) - 1.6 * min(f["noise"], 2) + 0.3 * min(
            f["rc"], 2
        )

    def _q_category(self, state, q):
        f = self._features(state)
        scores = {k: 2.0 * _hits(f["text"], CATEGORY_KEYWORDS.get(k, ())) for k in (q.criteria or {})}
        if "none" in scores:
            scores["none"] = 1.5 + 2.0 * min(f["noise"], 2) - 1.0 * max(f["rank"] - 2, 0) - 0.5 * f["err"]
        return self._dist(q, _softmax(scores, temp=1.0))

    def _q_component(self, state, q):
        f = self._features(state)
        scores = {k: 1.5 * _hits(f["text"], COMPONENT_KEYWORDS.get(k, ())) for k in (q.criteria or {})}
        if "application" in scores:
            scores["application"] = max(scores["application"], 1.0)
        return self._dist(q, _softmax(scores, temp=1.0))

    def _q_severity(self, state, q):
        f = self._features(state)
        n = len(q.criteria or ())
        base = {0: 0, 1: 0, 2: 0, 3: 1, 4: 2, 5: 3.4}[f["rank"]]
        center = base + 0.4 * min(f["rc"], 2) - 0.8 * min(f["noise"], 2) + (0.4 if f["count"] > 500 else 0)
        center = min(max(center, 0), n - 1)
        return self._dist(q, _softmax({str(i): -abs(i - center) * 1.6 for i in range(n)}))

    def _q_user_impact(self, state, q):
        f = self._features(state)
        n = len(q.criteria or ())
        center = max(f["rank"] - 2, 0) * 0.8 + 0.6 * min(f["user"], 2) - 0.8 * min(f["noise"], 2)
        center = min(max(center, 0), n - 1)
        return self._dist(q, _softmax({str(i): -abs(i - center) * 1.6 for i in range(n)}))

    def _q_actionable(self, state, q):
        return self._noul(q, _sigmoid(self._actionable_logit(self._features(state))))

    def _q_benign(self, state, q):
        f = self._features(state)
        return self._noul(q, _sigmoid(-self._actionable_logit(f) - 0.6 + 0.9 * min(f["noise"], 2)))

    def _q_root_cause(self, state, q):
        f = self._features(state)
        if f["rank"] < 3 and not f["err"]:
            return self._noul(q, 0.05)
        return self._noul(q, _sigmoid(-0.4 + 1.2 * min(f["rc"], 2) - 1.3 * min(f["sym"], 2)))

    def _q_novel(self, state, q):
        f = self._features(state)
        weird = _hits(f["text"], ("panic", "unexpected", "assert", "corrupt", "unknown", "nullpointer", "segfault"))
        return self._noul(q, _sigmoid(-2.4 + 1.3 * weird + 0.5 * max(f["rank"] - 3, 0) - 0.8 * min(f["noise"], 2)))

    def _q_sensitive(self, state, q):
        raw = state if isinstance(state, str) else json.dumps(state, default=str)
        return self._noul(q, 0.95 if self._scrubber.scrub(raw).dirty else 0.03)

    # -- L3 questions -----------------------------------------------------------------------------

    def _q_same_issue(self, state, q):
        a, b = state.get("a", {}), state.get("b", {})
        ta, tb = _tokens(a.get("template", "")), _tokens(b.get("template", ""))
        jac = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
        shared_traces = bool(set(a.get("trace_ids") or []) & set(b.get("trace_ids") or []))
        gap = abs(float(state.get("seconds_apart", 600)))
        p = 0.08 + 0.55 * jac + (0.35 if shared_traces else 0) + (0.15 if gap < 120 else 0)
        p += 0.1 if a.get("service") == b.get("service") else 0
        return self._noul(q, p)

    def _q_change_related(self, state, q):
        ch, err = state.get("change", {}), state.get("error", {})
        p = 0.08
        p += 0.45 if ch.get("service") == err.get("service") else 0.0
        minutes = float(err.get("minutes_after_change", 999))
        p += 0.25 if 0 <= minutes <= 30 else (0.1 if minutes <= 120 else -0.05)
        overlap = len(_tokens(ch.get("summary", "")) & _tokens(err.get("template", "")))
        p += min(0.2, 0.07 * overlap)
        return self._noul(q, p)

    def _q_hypothesis_supported(self, state, q):
        hyp = _tokens(str(state.get("hypothesis", "")))
        ev = _tokens(json.dumps(state.get("evidence", ""), default=str))
        overlap = len(hyp & ev) / max(len(hyp), 1)
        return self._noul(q, _sigmoid(-2.2 + 6.0 * overlap))

    def _q_event_type(self, state, q):
        t = _text(state)
        scores = {k: 0.0 for k in (q.criteria or {})}
        rules = {
            "trigger": ("deploy", "config change", "rollout", "release", "migration"),
            "first_error": ("first_error", "error", "exception", "failed"),
            "escalation": ("timeout", "upstream", "503", "504", "spreading"),
            "mitigation": ("rollback", "failover", "scaled", "restart"),
            "recovery": ("recovered", "healthy", "resolved", "back to normal"),
        }
        for k, words in rules.items():
            if k in scores:
                scores[k] = 1.5 * _hits(t, words)
        if "other" in scores:
            scores["other"] = 0.5
        return self._dist(q, _softmax(scores))

    def _q_milestone(self, state, q):
        t = _text(state)
        return self._noul(q, _sigmoid(-1.0 + 0.8 * _hits(t, ("deploy", "rollback", "first", "error", "recovered"))))
