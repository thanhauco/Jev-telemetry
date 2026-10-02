"""Jev backend over the public REST endpoint.

Request shape (TypeSafe docs and community tutorials, Sept 2026):

    POST https://api.typesafe.ai/v1/systemone
    {"state": ..., "model": "jev-1.13.0",
     "questions": {"key": {"type": "choice|score|noul", "instructions": "...", "criteria": ...}}}

Answers come back keyed by question: `choice`/`score` with `probabilities` and `confidence`, or a
bare `noul` probability. The parser below is deliberately tolerant; re-check it against the current
API reference before production use. Pin `model` so cached answers and tuned thresholds stay valid.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Sequence
from typing import Any

import httpx

from ..models import Answer, QType, Question
from .base import JudgeError, Judgment, Usage

DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-1.13.0"


class JevHTTPJudge:
    name = "jev"

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout_s: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ):
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY")
        if not self.api_key and client is None:
            raise JudgeError("TYPESAFE_API_KEY is not set", retryable=False)
        self.model_version = model
        self.endpoint = endpoint
        self._client = client or httpx.AsyncClient(timeout=timeout_s)

    def build_payload(self, state: dict[str, Any] | str, questions: Sequence[Question]) -> dict[str, Any]:
        return {
            "state": state,
            "model": self.model_version,
            "questions": {q.key: q.to_payload() for q in questions},
        }

    async def ask(self, state: dict[str, Any] | str, questions: Sequence[Question]) -> Judgment:
        payload = self.build_payload(state, questions)
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        t0 = time.perf_counter()
        try:
            resp = await self._client.post(self.endpoint, content=json.dumps(payload), headers=headers)
        except httpx.TimeoutException as e:
            raise JudgeError(f"timeout: {e}", retryable=True) from e
        except httpx.TransportError as e:
            raise JudgeError(f"transport error: {e}", retryable=True) from e
        latency = (time.perf_counter() - t0) * 1000

        if resp.status_code == 429 or resp.status_code >= 500:
            raise JudgeError(f"HTTP {resp.status_code}: {resp.text[:200]}", retryable=True, status=resp.status_code)
        if resp.status_code >= 400:
            raise JudgeError(f"HTTP {resp.status_code}: {resp.text[:200]}", retryable=False, status=resp.status_code)

        data = resp.json()
        answers = parse_answers(data, questions)
        usage = Usage(input_tokens=_input_tokens(data.get("usage")), requests=1)
        return Judgment(answers=answers, usage=usage, latency_ms=latency)

    async def aclose(self) -> None:
        await self._client.aclose()


def parse_answers(data: dict[str, Any], questions: Sequence[Question]) -> dict[str, Answer]:
    raw = data.get("answers", data)
    out: dict[str, Answer] = {}
    for q in questions:
        a = raw.get(q.key)
        if a is None:
            raise JudgeError(f"response is missing answer for {q.key!r}", retryable=False)
        if q.type is QType.NOUL:
            p = a if isinstance(a, (int, float)) else a.get("noul", a.get("probability"))
            out[q.key] = Answer(q.key, q.type, float(p), {"yes": float(p), "no": 1 - float(p)})
            continue
        probs = {str(k): float(v) for k, v in (a.get("probabilities") or {}).items()}
        if q.type is QType.CHOICE:
            value: str | int = str(a.get("choice") or max(probs, key=probs.get))
        else:
            value = int(a["score"]) if a.get("score") is not None else int(max(probs, key=probs.get))
        out[q.key] = Answer(q.key, q.type, value, probs, a.get("confidence"))
    return out


def _input_tokens(usage: Any) -> int:
    if not isinstance(usage, dict):
        return 0
    for k in ("input_tokens", "prompt_tokens", "total_tokens"):
        if k in usage:
            return int(usage[k])
    return 0
