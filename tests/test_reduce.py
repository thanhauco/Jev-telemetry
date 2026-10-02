from datetime import timedelta

from jev_telemetry.models import LogRecord
from jev_telemetry.reduce import DrainMiner, Reducer, mask, windows


def test_mask_numbers_ids_durations():
    assert mask("took 350ms for id 42 from 10.1.2.3:8080") == "took <DUR> for id <NUM> from <IP>"


def test_drain_merges_variants_into_one_template():
    m = DrainMiner()
    g1, _ = m.add("connection to db-7 failed after 3 retries")
    g2, t = m.add("connection to db-9 failed after 5 retries")
    assert g1 == g2
    assert t == "connection to db-<NUM> failed after <NUM> retries"


def test_drain_separates_different_messages():
    m = DrainMiner()
    a, _ = m.add("user logged in")
    b, _ = m.add("payment declined by issuer")
    assert a != b


def test_reducer_counts_levels_and_scrubs(t0):
    recs = [
        LogRecord(t0 + timedelta(seconds=i), "auth", "WARN" if i % 2 else "INFO", f"login failed for u{i}@x.com")
        for i in range(10)
    ] + [LogRecord(t0, "auth", "ERROR", "token service unreachable")]
    clusters, stats = Reducer().reduce(recs)
    assert stats.lines == 11 and stats.clusters == 2
    login = next(c for c in clusters if "login" in c.template)
    assert login.count == 10 and login.level == "WARN"
    assert login.level_counts == {"INFO": 5, "WARN": 5}
    assert all("@" not in e for e in login.examples)
    assert login.sensitive_hits["EMAIL"] == 10
    assert clusters[0].level == "ERROR"  # highest level sorts first


def test_template_ids_are_stable_across_runs(t0):
    recs = [LogRecord(t0, "svc", "INFO", f"job {i} finished in {i}ms") for i in range(5)]
    a, _ = Reducer().reduce(recs)
    b, _ = Reducer().reduce(recs)
    assert [c.template_id for c in a] == [c.template_id for c in b]


def test_windows(t0):
    recs = [LogRecord(t0 + timedelta(seconds=s), "svc", "INFO", "x") for s in (0, 10, 61, 62, 200)]
    out = [(start, len(rs)) for start, rs in windows(recs, 60)]
    assert [n for _, n in out] == [2, 2, 1]
