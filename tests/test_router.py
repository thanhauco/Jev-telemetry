from conftest import cluster, noul, score

from jev_telemetry.router import Decision, Router, RouterConfig, noisy_or, summarize


def answers(act, ben, novel=0.1, sev=(0.7, 0.2, 0.1, 0, 0), sens=0.02, source="jev"):
    return {
        "actionable": noul("actionable", act, source), "benign": noul("benign", ben, source),
        "novel": noul("novel", novel, source), "sensitive": noul("sensitive", sens, source),
        "severity": score("severity", sev),
    }


def test_suppresses_only_with_rules_check():
    r = Router()
    assert r.route(cluster("GET /healthz 200", count=5), answers(0.02, 0.99)).decision is Decision.SUPPRESS
    # Rare and not on the allowlist: the rules check blocks suppression.
    out = r.route(cluster("odd but harmless", count=3), answers(0.02, 0.99))
    assert out.decision is not Decision.SUPPRESS
    assert any("rules check" in x for x in out.reasons)


def test_never_suppresses_errors_or_fallback_answers():
    r = Router()
    assert r.route(cluster("GET /healthz", level="ERROR"), answers(0.02, 0.99)).decision is not Decision.SUPPRESS
    out = r.route(cluster("GET /healthz"), answers(0.02, 0.99, source="fallback"))
    assert out.decision is not Decision.SUPPRESS and "fallback_answers" in out.flags


def test_escalates_severe_or_novel_actionable():
    r = Router()
    assert r.route(cluster(level="ERROR"), answers(0.9, 0.01, novel=0.8)).decision is Decision.ESCALATE
    assert r.route(cluster(level="ERROR"), answers(0.9, 0.01, sev=(0, 0, 0.1, 0.5, 0.4))).decision is Decision.ESCALATE
    assert r.route(cluster(level="ERROR"), answers(0.9, 0.01)).decision is Decision.TRIAGE


def test_uncertain_goes_to_review_and_missing_answers_too():
    r = Router()
    assert r.route(cluster(), answers(0.55, 0.3)).decision is Decision.REVIEW
    assert r.route(cluster(), {}).decision is Decision.REVIEW


def test_known_issue_and_leak_triage():
    c = cluster("db pool exhausted")
    r = Router(RouterConfig(known_issues={c.template_id: "INC-42"}))
    assert r.route(c, answers(0.9, 0.01)).decision is Decision.KNOWN_ISSUE
    leak = cluster("payment card=<CARD>", sensitive_hits={"CARD": 3})
    assert Router().route(leak, answers(0.05, 0.97)).decision is Decision.TRIAGE


def test_noisy_or_and_summary():
    assert abs(noisy_or([0.5, 0.5]) - 0.75) < 1e-9
    r = Router()
    routed = [r.route(cluster(f"t{i}", level="ERROR"), answers(0.9, 0.01, sev=(0, 0, 0, 0.5, 0.5))) for i in range(2)]
    s = summarize(routed)
    assert s.expected_actionable_clusters == 1.8
    assert abs(s.p_any_severe - 1.0) < 1e-9
