"""Write run results as enrichment tables: JSONL files and/or ClickHouse (deploy/clickhouse/init.sql)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .clickhouse import ClickHouse
from .models import QType
from .pipeline import RunResult


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def cluster_rows(r: RunResult) -> list[dict[str, Any]]:
    start, end = r.window
    return [
        {
            "run_id": r.run_id, "window_start": _iso(start), "window_end": _iso(end),
            "template_id": c.template_id, "service": c.service, "template": c.template, "level": c.level,
            "count": c.count, "first_seen": _iso(c.first_seen), "last_seen": _iso(c.last_seen),
            "examples": c.examples, "trace_ids": c.trace_ids, "level_counts": c.level_counts,
            "sensitive_hits": c.sensitive_hits,
        }
        for c in r.clusters
    ]


def judgment_rows(r: RunResult) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for x in r.routed:
        for a in x.answers.values():
            rows.append({
                "run_id": r.run_id, "judged_at": now, "template_id": x.cluster.template_id,
                "model_version": r.model_version, "question_key": a.key, "question_type": a.type.value,
                "value": str(a.value), "p_yes": a.p_yes if a.type is QType.NOUL else None,
                "probabilities": a.probabilities, "confidence": a.confidence, "source": a.source,
            })
    return rows


def decision_rows(r: RunResult) -> list[dict[str, Any]]:
    start, _ = r.window
    return [
        {
            "run_id": r.run_id, "window_start": _iso(start), "template_id": x.cluster.template_id,
            "service": x.cluster.service, "level": x.cluster.level, "count": x.cluster.count,
            "decision": x.decision.value, "reasons": x.reasons, "flags": x.flags, "priority": x.priority,
            "category": x.choice("category"), "component": x.choice("component"),
            "expected_severity": x.expected("severity"), "expected_user_impact": x.expected("user_impact"),
            "p_actionable": x.p("actionable"), "p_benign": x.p("benign"), "p_root_cause": x.p("root_cause"),
            "p_novel": x.p("novel"), "p_sensitive": x.p("sensitive"),
        }
        for x in r.routed
    ]


def incident_rows(r: RunResult) -> list[dict[str, Any]]:
    rows = []
    for inc in r.incidents:
        rc = inc.root_causes[0][0].cluster.template_id if inc.root_causes else ""
        rows.append({
            "run_id": r.run_id, "incident_id": inc.incident_id, "start": _iso(inc.start), "end": _iso(inc.end),
            "services": inc.services, "template_ids": [m.cluster.template_id for m in inc.members],
            "lines": inc.lines, "priority": round(inc.priority, 4), "root_cause_template_id": rc,
            "linked_changes": [c.change.summary for c in inc.changes],
            "top_hypothesis": inc.hypotheses[0].text if inc.hypotheses else "",
            "narrative": r.narratives.get(inc.incident_id, ""),
        })
    return rows


def write_jsonl(r: RunResult, out_dir: str | Path) -> Path:
    d = Path(out_dir) / r.run_id
    d.mkdir(parents=True, exist_ok=True)
    for name, rows in (
        ("clusters", cluster_rows(r)),
        ("judgments", judgment_rows(r)),
        ("decisions", decision_rows(r)),
        ("incidents", incident_rows(r)),
    ):
        with (d / f"{name}.jsonl").open("w") as f:
            for row in rows:
                f.write(json.dumps(row, default=str) + "\n")
    (d / "summary.json").write_text(json.dumps({
        "run_id": r.run_id, "summary": r.summary.to_dict(), "reduce": {
            "lines": r.reduce_stats.lines, "clusters": r.reduce_stats.clusters,
            "reduction_ratio": round(r.reduce_stats.reduction_ratio, 1),
            "scrubbed_lines": r.reduce_stats.scrubbed_lines, "scrub_hits": dict(r.reduce_stats.scrub_hits),
        }, "judge": r.judge_stats, "timings_ms": r.timings_ms,
    }, indent=2, default=str))
    if r.narratives:
        (d / "incidents.md").write_text("\n\n---\n\n".join(r.narratives.values()) + "\n")
    return d


def write_clickhouse(r: RunResult, ch: ClickHouse, database: str = "jev") -> dict[str, int]:
    return {
        "log_clusters": ch.insert(f"{database}.log_clusters", cluster_rows(r)),
        "cluster_judgments": ch.insert(f"{database}.cluster_judgments", judgment_rows(r)),
        "routing_decisions": ch.insert(f"{database}.routing_decisions", decision_rows(r)),
        "incidents": ch.insert(f"{database}.incidents", incident_rows(r)),
    }
