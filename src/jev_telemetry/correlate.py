"""L3: correlation and hypothesis ranking on the pruned candidate set.

Pairwise questions are O(n^2), so candidates are prefiltered by time proximity and service topology
before any judge call. Only clusters the router surfaced (escalate/triage/review) take part.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from .judge import FanOut, JudgeTask
from .models import Change
from .questions import CHANGE_RELATED, HYPOTHESIS_SUPPORTED, SAME_ISSUE, TIMELINE_EVENT, TIMELINE_MILESTONE
from .router import Decision, Routed

SURFACED = (Decision.ESCALATE, Decision.TRIAGE, Decision.REVIEW)


@dataclass
class ChangeLink:
    change: Change
    template_id: str
    p_related: float


@dataclass
class Hypothesis:
    text: str
    p_supported: float


@dataclass
class TimelineEvent:
    ts: datetime
    label: str
    event_type: str
    p_milestone: float


@dataclass
class Incident:
    incident_id: str
    members: list[Routed]
    root_causes: list[tuple[Routed, float]] = field(default_factory=list)
    changes: list[ChangeLink] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    timeline: list[TimelineEvent] = field(default_factory=list)

    @property
    def start(self) -> datetime:
        return min(m.cluster.first_seen for m in self.members if m.cluster.first_seen)

    @property
    def end(self) -> datetime:
        return max(m.cluster.last_seen for m in self.members if m.cluster.last_seen)

    @property
    def services(self) -> list[str]:
        return sorted({m.cluster.service for m in self.members})

    @property
    def lines(self) -> int:
        return sum(m.cluster.count for m in self.members)

    @property
    def priority(self) -> float:
        return sum(m.priority for m in self.members)


def _adjacent(a: str, b: str, topology: dict[str, Sequence[str]] | None) -> bool:
    if a == b or topology is None:
        return True
    return b in topology.get(a, ()) or a in topology.get(b, ())


def candidate_pairs(
    routed: Sequence[Routed],
    max_gap_s: float = 600,
    topology: dict[str, Sequence[str]] | None = None,
    max_pairs: int = 2000,
) -> list[tuple[Routed, Routed, float]]:
    """Time- and topology-prefiltered pairs. Sorted by first_seen, so the inner loop breaks early."""
    items = sorted((r for r in routed if r.cluster.first_seen), key=lambda r: r.cluster.first_seen)
    pairs: list[tuple[Routed, Routed, float]] = []
    for i, a in enumerate(items):
        for b in items[i + 1 :]:
            gap = (b.cluster.first_seen - a.cluster.first_seen).total_seconds()
            if gap > max_gap_s:
                break
            if _adjacent(a.cluster.service, b.cluster.service, topology):
                pairs.append((a, b, gap))
    pairs.sort(key=lambda t: -(t[0].priority + t[1].priority))
    return pairs[:max_pairs]


def _pair_state(a: Routed, b: Routed, gap: float) -> dict:
    def side(r: Routed) -> dict:
        s = r.cluster.to_state(max_examples=2)
        s["trace_ids"] = r.cluster.trace_ids
        return s

    return {"a": side(a), "b": side(b), "seconds_apart": round(gap, 1)}


async def collapse_alerts(
    fanout: FanOut,
    routed: Sequence[Routed],
    threshold: float = 0.6,
    max_gap_s: float = 600,
    topology: dict[str, Sequence[str]] | None = None,
) -> list[Incident]:
    """Use case 3: union clusters whose `same_issue` probability clears the threshold."""
    surfaced = [r for r in routed if r.decision in SURFACED]
    pairs = candidate_pairs(surfaced, max_gap_s=max_gap_s, topology=topology)
    tasks = [
        JudgeTask(f"pair:{a.cluster.template_id}:{b.cluster.template_id}", _pair_state(a, b, gap), [SAME_ISSUE])
        for a, b, gap in pairs
    ]
    res = await fanout.run(tasks)

    parent = {r.cluster.template_id: r.cluster.template_id for r in surfaced}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for (a, b, _), task in zip(pairs, tasks):
        ans = res.answers.get(task.subject_id, {}).get("same_issue")
        if ans is not None and ans.p_yes >= threshold:
            parent[find(a.cluster.template_id)] = find(b.cluster.template_id)

    groups: dict[str, list[Routed]] = {}
    for r in surfaced:
        groups.setdefault(find(r.cluster.template_id), []).append(r)

    incidents = []
    for members in groups.values():
        ids = "".join(sorted(m.cluster.template_id for m in members))
        inc = Incident(incident_id="inc-" + hashlib.sha1(ids.encode()).hexdigest()[:10], members=members)
        inc.root_causes = rank_root_causes(members)
        incidents.append(inc)
    incidents.sort(key=lambda i: -i.priority)
    return incidents


def inc_floor(members: Sequence[Routed]) -> datetime:
    return min(m.cluster.first_seen for m in members if m.cluster.first_seen)


def rank_root_causes(members: Sequence[Routed], earliest_bonus: float = 0.5) -> list[tuple[Routed, float]]:
    """Use case 4: P(root cause) combined with first-occurrence order. Earlier clusters get a boost."""
    ordered = sorted(members, key=lambda r: (r.cluster.first_seen is None, r.cluster.first_seen or inc_floor(members)))
    scored = []
    for rank, r in enumerate(ordered):
        bonus = earliest_bonus / (1 + rank)
        scored.append((r, round(r.p("root_cause", 0.5) * (1 + bonus), 4)))
    return sorted(scored, key=lambda t: -t[1])


async def correlate_changes(
    fanout: FanOut,
    incidents: Sequence[Incident],
    changes: Sequence[Change],
    lookback_s: float = 3600,
    threshold: float = 0.5,
) -> None:
    """Use case 6: ask whether each (change, error cluster) pair is plausibly related."""
    plan: list[tuple[Incident, Change, Routed, JudgeTask]] = []
    for inc in incidents:
        for r in inc.members:
            fs = r.cluster.first_seen
            for ch in changes:
                delta = (fs - ch.ts).total_seconds()
                if 0 <= delta <= lookback_s:
                    state = {
                        "change": {"service": ch.service, "summary": ch.summary, "at": ch.ts.isoformat()},
                        "error": {**r.cluster.to_state(max_examples=2), "minutes_after_change": round(delta / 60, 1)},
                    }
                    sid = f"change:{ch.change_id or ch.ts.isoformat()}:{r.cluster.template_id}"
                    plan.append((inc, ch, r, JudgeTask(sid, state, [CHANGE_RELATED])))
    res = await fanout.run([t for *_, t in plan])
    for inc, ch, r, task in plan:
        ans = res.answers.get(task.subject_id, {}).get("change_related")
        if ans is not None and ans.p_yes >= threshold:
            inc.changes.append(ChangeLink(ch, r.cluster.template_id, ans.p_yes))
    for inc in incidents:
        # One link per change: keep the cluster it relates to most strongly.
        best: dict[str, ChangeLink] = {}
        for link in inc.changes:
            k = link.change.change_id or link.change.summary
            if k not in best or link.p_related > best[k].p_related:
                best[k] = link
        inc.changes = sorted(best.values(), key=lambda c: -c.p_related)


def evidence_bundle(inc: Incident, max_clusters: int = 8) -> dict:
    top = sorted(inc.members, key=lambda r: -r.priority)[:max_clusters]
    return {
        "services": inc.services,
        "window": [inc.start.isoformat(), inc.end.isoformat()],
        "patterns": [
            {"service": r.cluster.service, "level": r.cluster.level, "template": r.cluster.template,
             "occurrences": r.cluster.count, "first_seen": r.cluster.first_seen.isoformat()}
            for r in top
        ],
        "changes": [{"service": c.change.service, "summary": c.change.summary} for c in inc.changes[:5]],
    }


async def score_hypotheses(fanout: FanOut, inc: Incident, hypotheses: Sequence[str]) -> list[Hypothesis]:
    """Use case 7: score each proposed hypothesis against the evidence in parallel."""
    ev = evidence_bundle(inc)
    tasks = [
        JudgeTask(f"hyp:{inc.incident_id}:{hashlib.sha1(h.encode()).hexdigest()[:10]}",
                  {"hypothesis": h, "evidence": ev},
                  [HYPOTHESIS_SUPPORTED])
        for h in hypotheses
    ]
    res = await fanout.run(tasks)
    out = [
        Hypothesis(h, res.answers[t.subject_id]["hypothesis_supported"].p_yes)
        for h, t in zip(hypotheses, tasks)
        if "hypothesis_supported" in res.answers.get(t.subject_id, {})
    ]
    inc.hypotheses = sorted(out, key=lambda h: -h.p_supported)
    return inc.hypotheses


async def build_timeline(fanout: FanOut, inc: Incident, min_milestone: float = 0.5) -> list[TimelineEvent]:
    """Use case 12: classify each event and keep milestones, with no generative model involved."""
    events: list[tuple[datetime, str, str]] = []
    for c in inc.changes:
        events.append((c.change.ts, f"[{c.change.service}] change: {c.change.summary}", "change"))
    for r in inc.members:
        events.append((r.cluster.first_seen, f"[{r.cluster.service}] {r.cluster.level}: {r.cluster.template}", "log"))
    events.sort(key=lambda e: e[0])
    tasks = [
        JudgeTask(f"tl:{hashlib.sha1(f'{ts.isoformat()}|{label}'.encode()).hexdigest()[:16]}",
                  {"kind": kind, "at": ts.isoformat(), "event": label}, [TIMELINE_EVENT, TIMELINE_MILESTONE])
        for ts, label, kind in events
    ]
    res = await fanout.run(tasks)
    timeline = []
    for (ts, label, _), t in zip(events, tasks):
        a = res.answers.get(t.subject_id, {})
        if "milestone" in a and a["milestone"].p_yes >= min_milestone:
            timeline.append(TimelineEvent(ts, label, str(a["event_type"].value), a["milestone"].p_yes))
    inc.timeline = timeline
    return timeline
