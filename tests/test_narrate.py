from types import SimpleNamespace

import pytest
from conftest import cluster, noul

from jev_telemetry.correlate import Incident, rank_root_causes
from jev_telemetry.narrate import TemplateNarrator, incident_bundle
from jev_telemetry.router import Router


def incident():
    r = Router()
    a = r.route(cluster("db pool exhausted", level="ERROR", service="payments", minute=0),
                {"actionable": noul("actionable", 0.9), "benign": noul("benign", 0.01),
                 "root_cause": noul("root_cause", 0.8)})
    b = r.route(cluster("checkout timeout", level="ERROR", service="checkout", minute=2,
                        examples=["secret stuff"]),
                {"actionable": noul("actionable", 0.9), "benign": noul("benign", 0.01),
                 "root_cause": noul("root_cause", 0.2), "sensitive": noul("sensitive", 0.9)})
    inc = Incident("inc-1", [a, b])
    inc.root_causes = rank_root_causes(inc.members)
    return inc


def test_template_narrator_and_bundle_redaction():
    inc = incident()
    text = TemplateNarrator().narrate(inc)
    assert "db pool exhausted" in text and "not explanations" in text
    pats = {p["template"]: p for p in incident_bundle(inc)["patterns"]}
    assert "examples" not in pats["checkout timeout"]  # sensitive cluster: examples withheld
    assert TemplateNarrator().propose_hypotheses(inc)


def test_claude_narrator_handles_refusal_and_success(monkeypatch):
    pytest.importorskip("anthropic")
    from jev_telemetry.narrate import ClaudeNarrator

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    n = ClaudeNarrator()
    calls = []

    def fake_create(**kw):
        calls.append(kw)
        if len(calls) == 1:
            return SimpleNamespace(stop_reason="refusal", content=[])
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="## Summary\nok")])

    monkeypatch.setattr(n.client.beta.messages, "create", fake_create)
    inc = incident()
    assert n.narrate(inc).startswith("### inc-1")  # refusal -> template fallback
    assert n.narrate(inc) == "## Summary\nok"
    assert calls[0]["model"] == "claude-opus-5" and calls[0]["fallbacks"] == "default"
