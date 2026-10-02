"""L0: deterministic reduction. Millions of lines in, thousands of clusters out."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..models import Cluster, LogRecord, level_rank, normalize_level
from .drain import DrainMiner, template_hash
from .scrub import Scrubber


@dataclass
class ReduceStats:
    lines: int = 0
    clusters: int = 0
    scrubbed_lines: int = 0
    scrub_hits: Counter = field(default_factory=Counter)

    @property
    def reduction_ratio(self) -> float:
        return self.lines / self.clusters if self.clusters else 0.0


class Reducer:
    """Scrub -> mine templates per service -> aggregate counts, levels, examples and trace ids.

    The miner is kept across calls so template ids stay stable between micro-batches of one process.
    """

    def __init__(
        self,
        scrubber: Scrubber | None = None,
        sim_threshold: float = 0.5,
        max_examples: int = 3,
        max_trace_ids: int = 5,
    ):
        self.scrubber = scrubber or Scrubber()
        self.sim_threshold = sim_threshold
        self.max_examples = max_examples
        self.max_trace_ids = max_trace_ids
        self._miners: dict[str, DrainMiner] = defaultdict(lambda: DrainMiner(sim_threshold=self.sim_threshold))

    def reduce(self, records: Iterable[LogRecord]) -> tuple[list[Cluster], ReduceStats]:
        stats = ReduceStats()
        by_group: dict[tuple[str, int], Cluster] = {}

        for rec in records:
            stats.lines += 1
            scrubbed = self.scrubber.scrub(rec.body)
            if scrubbed.dirty:
                stats.scrubbed_lines += 1
                stats.scrub_hits.update(scrubbed.hits)
            gid, _ = self._miners[rec.service].add(scrubbed.text)
            key = (rec.service, gid)
            level = normalize_level(rec.level)
            c = by_group.get(key)
            if c is None:
                c = by_group[key] = Cluster(template_id="", service=rec.service, template="", level=level)
            c.count += 1
            c.level_counts[level] = c.level_counts.get(level, 0) + 1
            if level_rank(level) > level_rank(c.level):
                c.level = level
            c.first_seen = rec.ts if c.first_seen is None or rec.ts < c.first_seen else c.first_seen
            c.last_seen = rec.ts if c.last_seen is None or rec.ts > c.last_seen else c.last_seen
            if len(c.examples) < self.max_examples and scrubbed.text not in c.examples:
                c.examples.append(scrubbed.text)
            if rec.trace_id and len(c.trace_ids) < self.max_trace_ids and rec.trace_id not in c.trace_ids:
                c.trace_ids.append(rec.trace_id)
            for name, n in scrubbed.hits.items():
                c.sensitive_hits[name] = c.sensitive_hits.get(name, 0) + n

        # Templates only settle once every line is in, so ids are assigned at the end. Groups that
        # converged to the same template are merged.
        merged: dict[str, Cluster] = {}
        for (service, gid), c in by_group.items():
            c.template = self._miners[service].template_of(gid)
            c.template_id = template_hash(service, c.template)
            prev = merged.get(c.template_id)
            merged[c.template_id] = _merge(prev, c) if prev else c

        clusters = sorted(merged.values(), key=lambda c: (-level_rank(c.level), -c.count))
        stats.clusters = len(clusters)
        return clusters, stats


def _merge(a: Cluster, b: Cluster) -> Cluster:
    a.count += b.count
    for k, v in b.level_counts.items():
        a.level_counts[k] = a.level_counts.get(k, 0) + v
    for k, v in b.sensitive_hits.items():
        a.sensitive_hits[k] = a.sensitive_hits.get(k, 0) + v
    if level_rank(b.level) > level_rank(a.level):
        a.level = b.level
    a.first_seen = min(x for x in (a.first_seen, b.first_seen) if x)
    a.last_seen = max(x for x in (a.last_seen, b.last_seen) if x)
    a.examples = (a.examples + [e for e in b.examples if e not in a.examples])[: max(len(a.examples), 3)]
    a.trace_ids = (a.trace_ids + [t for t in b.trace_ids if t not in a.trace_ids])[:5]
    return a


def windows(records: Iterable[LogRecord], seconds: int) -> Iterator[tuple[datetime, list[LogRecord]]]:
    """Group time-ordered records into tumbling windows of `seconds`."""
    bucket: list[LogRecord] = []
    start: datetime | None = None
    span = timedelta(seconds=seconds)
    for rec in records:
        if start is None:
            start = _floor(rec.ts, seconds)
        while rec.ts >= start + span:
            if bucket:
                yield start, bucket
                bucket = []
            start += span
        bucket.append(rec)
    if bucket and start is not None:
        yield start, bucket


def _floor(ts: datetime, seconds: int) -> datetime:
    epoch = ts.timestamp()
    return datetime.fromtimestamp(epoch - epoch % seconds, tz=ts.tzinfo)
