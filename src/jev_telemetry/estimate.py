"""Dry-run estimation: tokens, cost and wall time for a backfill, without calling the judge."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass

from .judge import AnswerCache
from .models import Cluster
from .questions import QuestionPack

CHARS_PER_TOKEN = 4.0  # rough English/JSON average; replace with a measured ratio once you have usage data


@dataclass
class Estimate:
    clusters: int
    cached_clusters: int
    requests: int
    questions_asked: int
    input_tokens: int
    est_cost_usd: float
    est_wall_s: float
    max_request_tokens: int

    def to_dict(self) -> dict:
        return self.__dict__


def tokens(obj) -> int:
    text = obj if isinstance(obj, str) else json.dumps(obj, separators=(",", ":"))
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def estimate(
    clusters: Sequence[Cluster],
    pack: QuestionPack,
    cache: AnswerCache | None = None,
    model_version: str = "jev-1.13.0",
    usd_per_m_input: float = 0.042,
    billing: str = "per_question",
    concurrency: int = 32,
    rpm: float = 1200,
    p50_latency_s: float = 0.5,
) -> Estimate:
    q_tokens = {q.key: tokens(q.to_payload()) for q in pack}
    total = requests = asked = cached_clusters = max_req = 0
    for c in clusters:
        missing = list(pack)
        if cache is not None:
            have = cache.get_many(c.template_id, model_version, missing)
            missing = [q for q in missing if q.key not in have]
        if not missing:
            cached_clusters += 1
            continue
        st = tokens(c.to_state())
        req = st * len(missing) + sum(q_tokens[q.key] for q in missing) if billing == "per_question" \
            else st + sum(q_tokens[q.key] for q in missing)
        total += req
        requests += 1
        asked += len(missing)
        max_req = max(max_req, st + max(q_tokens[q.key] for q in missing))
    throughput = min(concurrency / max(p50_latency_s, 1e-3), rpm / 60.0)  # requests per second
    return Estimate(
        clusters=len(clusters),
        cached_clusters=cached_clusters,
        requests=requests,
        questions_asked=asked,
        input_tokens=total,
        est_cost_usd=round(total / 1e6 * usd_per_m_input, 6),
        est_wall_s=round(requests / throughput, 1) if requests else 0.0,
        max_request_tokens=max_req,
    )
