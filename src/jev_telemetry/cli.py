"""jev-telemetry command line."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import load_config
from .router import Decision


def _cfg(args) -> dict:
    cfg = load_config(args.config)
    if getattr(args, "backend", None):
        cfg["judge"]["backend"] = args.backend
    if getattr(args, "no_cache", False):
        cfg["cache"]["path"] = ":memory:"
    return cfg


def _print_triage(result, limit: int) -> None:
    s = result.summary
    st = result.reduce_stats
    print(f"\nrun {result.run_id}  model={result.model_version}")
    print(f"L0  {st.lines:,} lines -> {st.clusters:,} clusters ({st.reduction_ratio:.0f}x), "
          f"scrubbed {st.scrubbed_lines:,} lines {dict(st.scrub_hits)}")
    js = result.judge_stats
    print(f"L1  {js['requests']} requests, cache hit {js['cache_hit_rate']:.0%}, p50 {js['p50_ms']}ms, "
          f"p95 {js['p95_ms']}ms, wall {js['wall_ms']}ms, est ${js['est_cost_usd']}, "
          f"fallbacks {js['fallbacks']}, dead letters {len(js['dead_letters'])}")
    print(f"L2  {s.decisions}  suppressed {s.suppressed_lines:,} lines; "
          f"E[actionable clusters]={s.expected_actionable_clusters}, P(any severe)={s.p_any_severe}")
    print()
    print(f"{'decision':<12}{'service':<13}{'level':<7}{'count':>7} {'category':<11}{'sev':>5}{'P(act)':>8}  template")
    shown = 0
    for r in result.routed:
        if r.decision is Decision.SUPPRESS:
            continue
        sev = r.expected("severity")
        flags = f" [{','.join(r.flags)}]" if r.flags else ""
        print(f"{r.decision.value:<12}{r.cluster.service:<13}{r.cluster.level:<7}{r.cluster.count:>7} "
              f"{(r.choice('category') or '-'):<11}{(sev if sev is not None else 0):>5.1f}{r.p('actionable'):>8.2f}  "
              f"{r.cluster.template[:70]}{flags}")
        shown += 1
        if shown >= limit:
            break
    hidden = sum(1 for r in result.routed if r.decision is Decision.SUPPRESS)
    print(f"... {hidden} suppressed clusters hidden (counted, not dropped)")
    for inc in result.incidents[:5]:
        print(f"\nL3  {inc.incident_id}: {len(inc.members)} clusters across {inc.services}, {inc.lines:,} lines")
        for r, score in inc.root_causes[:3]:
            print(f"      rc-score   {score:.2f}  [{r.cluster.service}] {r.cluster.template[:70]}")
        for c in inc.changes[:2]:
            print(f"      change     {c.p_related:.2f}  [{c.change.service}] {c.change.summary}")
        for h in inc.hypotheses[:3]:
            print(f"      hypothesis {h.p_supported:.2f}  {h.text[:90]}")
    if result.timings_ms:
        print(f"\ntimings {result.timings_ms}")


async def _run(args) -> int:
    from .pipeline import Pipeline
    from .sinks import write_clickhouse, write_jsonl
    from .sources import read_changes, read_logs

    cfg = _cfg(args)
    pipe = Pipeline(cfg)
    try:
        changes = read_changes(args.changes) if args.changes else []
        result = await pipe.run(read_logs(args.input), changes=changes, correlate=not args.no_correlate)
    finally:
        await pipe.aclose()
    _print_triage(result, args.limit)
    sink = args.sink or cfg["sink"]["kind"]
    if sink in ("jsonl", "both"):
        d = write_jsonl(result, args.out or cfg["sink"]["out_dir"])
        print(f"\nwrote {d}")
    if sink in ("clickhouse", "both"):
        from .clickhouse import ClickHouse

        counts = write_clickhouse(result, ClickHouse(), cfg["sink"]["clickhouse_database"])
        print(f"wrote ClickHouse {counts}")
    return 0


async def _stream(args) -> int:
    """Micro-batch worker: judge each window of otel_logs and write enrichment tables back."""
    from .clickhouse import ClickHouse
    from .pipeline import Pipeline
    from .sinks import write_clickhouse
    from .sources import clickhouse_logs

    cfg = _cfg(args)
    ch = ClickHouse()
    pipe = Pipeline(cfg)
    lag = timedelta(seconds=args.lag)
    cursor = datetime.now(timezone.utc) - lag - timedelta(seconds=args.window)
    try:
        while True:
            end = cursor + timedelta(seconds=args.window)
            wait = (end + lag - datetime.now(timezone.utc)).total_seconds()
            if wait > 0:
                await asyncio.sleep(wait)
            result = await pipe.run(clickhouse_logs(ch, cursor, end, table=args.table))
            if result.clusters:
                counts = write_clickhouse(result, ch, cfg["sink"]["clickhouse_database"])
                print(f"{cursor:%H:%M:%S}-{end:%H:%M:%S} {result.summary.decisions} {counts}", flush=True)
            cursor = end
            if args.once:
                return 0
    finally:
        await pipe.aclose()
        ch.close()


def _estimate(args) -> int:
    from .estimate import estimate
    from .judge import AnswerCache
    from .questions import load_pack
    from .reduce import Reducer
    from .sources import read_logs

    cfg = _cfg(args)
    clusters, stats = Reducer(sim_threshold=cfg["reduce"]["sim_threshold"]).reduce(read_logs(args.input))
    cache = None if args.no_cache else AnswerCache(cfg["cache"]["path"])
    est = estimate(
        clusters, load_pack(cfg["pack"]), cache=cache, model_version=cfg["judge"]["model"],
        usd_per_m_input=cfg["pricing"]["usd_per_m_input"], billing=cfg["pricing"]["billing"],
        concurrency=cfg["judge"]["concurrency"], rpm=cfg["judge"]["rpm"], p50_latency_s=args.p50,
    )
    print(json.dumps({"lines": stats.lines, **est.to_dict()}, indent=2))
    return 0


async def _eval(args) -> int:
    from .eval import evaluate, format_report, load_golden
    from .pipeline import Pipeline

    cfg = _cfg(args)
    pipe = Pipeline(cfg)
    try:
        rows = load_golden(args.golden)
        rep = await evaluate(rows, pipe.pack, pipe.fanout, pipe.router, target_precision=args.target_precision,
                             price_per_m_input=cfg["pricing"]["usd_per_m_input"])
    finally:
        await pipe.aclose()
    print(format_report(rep))
    if args.json:
        Path(args.json).write_text(json.dumps(rep.to_dict(), indent=2))
        print(f"\nwrote {args.json}")
    fsr = rep.routing.get("false_suppression_rate")
    if args.max_false_suppression is not None and fsr is not None and fsr > args.max_false_suppression:
        print(f"FAIL: false-suppression rate {fsr} > {args.max_false_suppression}", file=sys.stderr)
        return 1
    return 0


def _export_golden(args) -> int:
    from .eval import export_for_labeling
    from .reduce import Reducer
    from .sources import read_logs

    clusters, _ = Reducer().reduce(read_logs(args.input))
    n = export_for_labeling(clusters, args.out)
    print(f"wrote {n} unlabeled clusters to {args.out}; fill in `labels` by hand")
    return 0


def _generate(args) -> int:
    from .sample_data import generate

    t = time.perf_counter()
    counts = generate(args.out, minutes=args.minutes, scale=args.scale)
    print(f"wrote {counts} to {args.out}/ in {time.perf_counter() - t:.1f}s")
    return 0


def _init_clickhouse(args) -> int:
    from .clickhouse import ClickHouse

    ch = ClickHouse()
    sql = "\n".join(line for line in Path(args.schema).read_text().splitlines() if not line.lstrip().startswith("--"))
    stmts = [s.strip() for s in sql.split(";") if s.strip()]
    for s in stmts:
        ch.command(s)
    print(f"applied {len(stmts)} statements from {args.schema}")
    return 0


def _replay(args) -> int:
    from .otlp import replay
    from .sources import read_logs

    n = replay(read_logs(args.input), endpoint=args.endpoint)
    print(f"sent {n:,} records to {args.endpoint}/v1/logs (timestamps rebased to end now)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="jev-telemetry", description=__doc__)
    default_cfg = "config/jev-telemetry.yaml"
    p.add_argument("--config", default=default_cfg if Path(default_cfg).exists() else None)
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate-sample", help="write synthetic logs, changes and a starter golden set")
    g.add_argument("--out", default="samples")
    g.add_argument("--minutes", type=int, default=120)
    g.add_argument("--scale", type=float, default=1.0)

    r = sub.add_parser("run", help="reduce, judge, route, correlate and narrate a log file")
    r.add_argument("--input", required=True)
    r.add_argument("--changes")
    r.add_argument("--backend", choices=["jev", "heuristic"])
    r.add_argument("--sink", choices=["jsonl", "clickhouse", "both", "none"])
    r.add_argument("--out")
    r.add_argument("--limit", type=int, default=25)
    r.add_argument("--no-correlate", action="store_true")
    r.add_argument("--no-cache", action="store_true")

    s = sub.add_parser("stream", help="micro-batch worker over ClickHouse otel_logs")
    s.add_argument("--window", type=int, default=60, help="window length in seconds")
    s.add_argument("--lag", type=int, default=15, help="seconds to wait for late logs")
    s.add_argument("--table", default="otel.otel_logs")
    s.add_argument("--backend", choices=["jev", "heuristic"])
    s.add_argument("--once", action="store_true")

    e = sub.add_parser("estimate", help="dry-run token, cost and time estimate")
    e.add_argument("--input", required=True)
    e.add_argument("--p50", type=float, default=0.5, help="assumed per-request p50 latency in seconds")
    e.add_argument("--no-cache", action="store_true")

    v = sub.add_parser("eval", help="offline eval on a golden set")
    v.add_argument("--golden", required=True)
    v.add_argument("--backend", choices=["jev", "heuristic"])
    v.add_argument("--json")
    v.add_argument("--target-precision", type=float, default=0.9)
    v.add_argument("--max-false-suppression", type=float, help="exit 1 if exceeded (for CI)")
    v.add_argument("--no-cache", action="store_true")

    x = sub.add_parser("export-golden", help="write clusters as unlabeled golden rows")
    x.add_argument("--input", required=True)
    x.add_argument("--out", default="golden_unlabeled.jsonl")

    rp = sub.add_parser("replay", help="send a JSONL log file to an OTLP/HTTP collector")
    rp.add_argument("--input", required=True)
    rp.add_argument("--endpoint", default="http://localhost:4318")

    i = sub.add_parser("init-clickhouse", help="create the enrichment tables")
    i.add_argument("--schema", default="deploy/clickhouse/init.sql")

    args = p.parse_args(argv)
    if args.cmd == "run":
        if args.sink == "none":
            args.sink = "off"
        return asyncio.run(_run(args))
    if args.cmd == "stream":
        return asyncio.run(_stream(args))
    if args.cmd == "eval":
        return asyncio.run(_eval(args))
    return {"generate-sample": _generate, "estimate": _estimate, "export-golden": _export_golden,
            "init-clickhouse": _init_clickhouse, "replay": _replay}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
