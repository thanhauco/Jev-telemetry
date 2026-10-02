import json

import httpx
import pytest
from conftest import run

from jev_telemetry.judge import JevHTTPJudge, JudgeError
from jev_telemetry.models import QType, Question

QS = [
    Question("category", QType.CHOICE, "Which?", {"timeout": "t", "none": "n"}),
    Question("severity", QType.SCORE, "How bad?", ("low", "mid", "high")),
    Question("actionable", QType.NOUL, "Act?"),
]


def make(handler):
    return JevHTTPJudge(api_key="test", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_request_shape_and_parsing():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={
            "answers": {
                "category": {"choice": "timeout", "confidence": 0.6, "probabilities": {"timeout": 0.8, "none": 0.2}},
                "severity": {"score": 2, "confidence": 0.5, "probabilities": {"0": 0.1, "1": 0.2, "2": 0.7}},
                "actionable": {"noul": 0.83},
            },
            "usage": {"input_tokens": 321},
        })

    j = run(make(handler).ask({"template": "x"}, QS))
    assert seen["auth"] == "Bearer test"
    assert seen["body"]["model"] == "jev-1.13.0"
    assert seen["body"]["questions"]["severity"] == {"type": "score", "instructions": "How bad?",
                                                     "criteria": ["low", "mid", "high"]}
    assert "criteria" not in seen["body"]["questions"]["actionable"]
    assert j.answers["category"].value == "timeout"
    assert j.answers["severity"].value == 2 and abs(j.answers["severity"].expected_score() - 1.6) < 1e-9
    assert j.answers["actionable"].p_yes == 0.83
    assert j.usage.input_tokens == 321


@pytest.mark.parametrize("status,retryable", [(429, True), (503, True), (400, False), (401, False)])
def test_error_classification(status, retryable):
    judge = make(lambda req: httpx.Response(status, text="nope"))
    with pytest.raises(JudgeError) as e:
        run(judge.ask("x", QS))
    assert e.value.retryable is retryable
