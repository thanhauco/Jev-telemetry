"""Orchestrates L0 -> L4 for one batch or window of records."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .correlate import Incident, build_timeline, collapse_alerts, correlate_changes, score_hypotheses
from .judge import AnswerCache, FanOut, FanOutResult, HeuristicJudge, Judge, JudgeTask, make_judge
from .models import Change, Cluster, LogRecord
from .narrate import Narrator, make_narrator
from .questions import QuestionPack, load_pack, runbook_question
from .reduce import Reducer, ReduceStats, Scrubber
from .router import Decision, Routed, Router, RouterConfig, WindowSummary, summarize


@dataclass
class RunResult:
    run_id: str
    model_version: str
    clusters: list[Cluster]
    reduce_stats: ReduceStats
    routed: list[Routed]
    summary: WindowSummary
    incidents: list[Incident] = field(default_factory=list)
    narratives: dict[str, str] = field(default_factory=dict)
    judge_stats: dict[str, Any] = field(default_factory=dict)
    timings_ms: dict[str, float] = field(default_factory=dict)

    @property
    def window(self) -> tuple[datetime | None, datetime | None]:
        firsts = [c.first_seen for c in self.clusters if c.first_seen]
        lasts = [c.last_seen for c in self.clusters if c.last_seen]
        return (min(firsts) if firsts else None, max(lasts) if lasts else None)


class Pipeline:
    def __init__(
        self,
        cfg: dict[str, Any],
        judge: Judge | None = None,
        narrator: Narrator | None = None,
        cache: AnswerCache | None = None,
    ):
        self.cfg = cfg
        jc = cfg["judge"]
        self.judge = judge or make_judge(
            jc["backend"],
            **({"model": jc["model"], "endpoint": jc["endpoint"], "timeout_s": jc["timeout_s"]}
               if jc["backend"] == "jev" else {}),
        )
        fallback = HeuristicJudge() if jc.get("fallback") == "heuristic" and self.judge.name != "heuristic" else None
        ttl = cfg["cache"].get("ttl_days")
        self.cache = cache if cache is not None else AnswerCache(
            cfg["cache"]["path"], ttl_s=ttl * 86400 if ttl else None
        )
        self.fanout = FanOut(
            self.judge,
            cache=self.cache,
            fallback=fallback,
            concurrency=jc["concurrency"],
            max_retries=jc["max_retries"],
            rpm=jc.get("rpm") if self.judge.name != "heuristic" else None,
        )
        pack = load_pack(cfg["pack"])
        if cfg.get("runbooks"):
            pack = QuestionPack(questions=(*pack.questions, runbook_question(cfg["runbooks"])), version=pack.version)
        self.pack = pack
        self.reducer = Reducer(
            scrubber=Scrubber(redact_ips=cfg["reduce"].get("redact_ips", False)),
            sim_threshold=cfg["reduce"]["sim_threshold"],
        )
        router_cfg = {**cfg.get("router", {}), "known_issues": cfg.get("known_issues") or {}}
        self.router = Router(RouterConfig.from_dict(router_cfg))
        nc = cfg["narrate"]
        self.narrator = narrator or make_narrator(
            nc["kind"], **({"model": nc["model"], "effort": nc["effort"]} if nc["kind"] == "claude" else {})
        )

    async def judge_clusters(self, clusters: Sequence[Cluster]) -> FanOutResult:
        tasks = [JudgeTask(c.template_id, c.to_state(), list(self.pack)) for c in clusters]
        return await self.fanout.run(tasks)

    async def run(
        self,
        records: Iterable[LogRecord],
        changes: Sequence[Change] = (),
        correlate: bool | None = None,
        narrate: bool = True,
        run_id: str | None = None,
    ) -> RunResult:
        timings: dict[str, float] = {}
        t = time.perf_counter()

        # L0
        clusters, rstats = self.reducer.reduce(records)
        timings["L0_reduce"] = _ms(t)

        # L1
        t = time.perf_counter()
        hits0, misses0 = self.cache.stats.hits, self.cache.stats.misses
        res = await self.judge_clusters(clusters)
        timings["L1_judge"] = _ms(t)

        # L2
        t = time.perf_counter()
        routed = [self.router.route(c, res.answers.get(c.template_id, {})) for c in clusters]
        routed.sort(key=lambda r: -r.priority)
        summary = summarize(routed)
        timings["L2_route"] = _ms(t)

        result = RunResult(
            run_id=run_id or uuid.uuid4().hex[:12],
            model_version=self.judge.model_version,
            clusters=clusters,
            reduce_stats=rstats,
            routed=routed,
            summary=summary,
        )

        # L3
        cc = self.cfg["correlate"]
        if correlate if correlate is not None else cc.get("enabled", True):
            t = time.perf_counter()
            incidents = await collapse_alerts(
                self.fanout, routed, threshold=cc["same_issue_threshold"], max_gap_s=cc["max_gap_s"],
                topology=cc.get("topology"),
            )
            # Only incidents with at least one escalated member go further.
            incidents = [i for i in incidents if any(m.decision is Decision.ESCALATE for m in i.members)]
            if changes:
                await correlate_changes(self.fanout, incidents, changes, lookback_s=cc["change_lookback_s"],
                                        threshold=cc["change_threshold"])
            top = incidents[: self.cfg["narrate"]["top_k"]]
            for inc in top:
                await score_hypotheses(self.fanout, inc, self.narrator.propose_hypotheses(inc))
                await build_timeline(self.fanout, inc)
            result.incidents = incidents
            timings["L3_correlate"] = _ms(t)

            # L4
            if narrate:
                t = time.perf_counter()
                result.narratives = {inc.incident_id: self.narrator.narrate(inc) for inc in top}
                timings["L4_narrate"] = _ms(t)

        hits, misses = self.cache.stats.hits - hits0, self.cache.stats.misses - misses0
        result.judge_stats = {
            "backend": self.judge.name,
            "model_version": self.judge.model_version,
            "requests": res.usage.requests,
            "input_tokens": res.usage.input_tokens,
            "est_cost_usd": round(res.usage.input_tokens / 1e6 * self.cfg["pricing"]["usd_per_m_input"], 6),
            "p50_ms": round(res.percentile(50), 1),
            "p95_ms": round(res.percentile(95), 1),
            "wall_ms": round(res.wall_ms, 1),
            "cache_hit_rate": round(hits / (hits + misses), 4) if hits + misses else 0.0,
            "fallbacks": res.fallbacks,
            "dead_letters": [d.__dict__ for d in res.dead_letters],
        }
        result.timings_ms = timings
        return result

    async def aclose(self) -> None:
        await self.judge.aclose()
        self.cache.close()


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)
