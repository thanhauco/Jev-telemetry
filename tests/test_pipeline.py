from pathlib import Path

from conftest import run

from jev_telemetry.config import load_config
from jev_telemetry.estimate import estimate
from jev_telemetry.pipeline import Pipeline
from jev_telemetry.questions import load_pack
from jev_telemetry.router import Decision
from jev_telemetry.sample_data import generate
from jev_telemetry.sinks import write_jsonl
from jev_telemetry.sources import read_changes, read_logs

ROOT = Path(__file__).resolve().parents[1]


def test_pack_loads():
    pack = load_pack(ROOT / "config/question_pack.yaml")
    assert pack.keys() == ["category", "component", "severity", "user_impact", "actionable", "benign",
                           "root_cause", "novel", "sensitive"]


def test_end_to_end_on_sample_incident(tmp_path):
    generate(tmp_path / "s", minutes=90, scale=1.0)
    cfg = load_config(ROOT / "config/jev-telemetry.yaml")
    cfg["cache"]["path"] = ":memory:"
    pipe = Pipeline(cfg)
    result = run(pipe.run(read_logs(tmp_path / "s/logs.jsonl"), changes=read_changes(tmp_path / "s/changes.jsonl")))
    run(pipe.aclose())

    st = result.reduce_stats
    assert st.lines > 1000 and st.clusters < 30
    by_template = {r.cluster.template: r for r in result.routed}
    pool = next(r for t, r in by_template.items() if "pool exhausted" in t)
    health = next(r for t, r in by_template.items() if "healthz" in t)
    assert pool.decision is Decision.ESCALATE
    assert health.decision is Decision.SUPPRESS
    # No ERROR/FATAL cluster is ever suppressed.
    assert not any(r.decision is Decision.SUPPRESS and r.cluster.level in ("ERROR", "FATAL") for r in result.routed)

    main = next(i for i in result.incidents if "payments" in i.services)
    assert {"payments", "checkout", "api-gateway"} <= set(main.services)
    assert "pool exhausted" in main.root_causes[0][0].cluster.template
    assert main.changes and "max_connections" in main.changes[0].change.summary
    assert result.narratives[main.incident_id].startswith("### inc-")

    out = write_jsonl(result, tmp_path / "out")
    assert {p.name for p in out.iterdir()} >= {"clusters.jsonl", "judgments.jsonl", "decisions.jsonl",
                                                 "incidents.jsonl", "summary.json"}


def test_estimate(tmp_path):
    from jev_telemetry.reduce import Reducer

    generate(tmp_path, minutes=10, scale=0.1)
    clusters, _ = Reducer().reduce(read_logs(tmp_path / "logs.jsonl"))
    pack = load_pack(ROOT / "config/question_pack.yaml")
    per_q = estimate(clusters, pack, billing="per_question")
    per_r = estimate(clusters, pack, billing="per_request")
    assert per_q.requests == len(clusters) and per_q.questions_asked == len(clusters) * len(pack)
    assert per_q.input_tokens > per_r.input_tokens > 0


def test_otlp_payload_groups_by_service(t0):
    from jev_telemetry.models import LogRecord
    from jev_telemetry.otlp import to_otlp

    recs = [LogRecord(t0, "a", "ERROR", "x", trace_id="ab" * 16), LogRecord(t0, "b", "INFO", "y")]
    body = to_otlp(recs)
    assert len(body["resourceLogs"]) == 2
    first = body["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    assert first["severityNumber"] == 17 and first["traceId"] == "ab" * 16
