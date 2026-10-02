"""L1 fan-out: bounded concurrency, rate limiting, retries with jitter, dead-letter queue, fallback.

One request per subject (cluster, pair, hypothesis) carries all of that subject's questions, since
Jev runs them in parallel and in isolation server-side. Subjects run concurrently client-side.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..models import Answer, Question
from .base import Judge, JudgeError, Usage
from .cache import AnswerCache


@dataclass
class JudgeTask:
    subject_id: str
    state: dict[str, Any] | str
    questions: Sequence[Question]


@dataclass
class DeadLetter:
    subject_id: str
    error: str
    attempts: int


@dataclass
class FanOutResult:
    answers: dict[str, dict[str, Answer]] = field(default_factory=dict)
    usage: Usage = field(default_factory=Usage)
    latencies_ms: list[float] = field(default_factory=list)
    dead_letters: list[DeadLetter] = field(default_factory=list)
    fallbacks: int = 0
    wall_ms: float = 0.0

    def percentile(self, p: float) -> float:
        if not self.latencies_ms:
            return 0.0
        xs = sorted(self.latencies_ms)
        return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


class RateLimiter:
    """Token bucket over requests per minute."""

    def __init__(self, rpm: float):
        self.rate = rpm / 60.0
        self.capacity = max(1.0, self.rate)
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


class FanOut:
    def __init__(
        self,
        judge: Judge,
        cache: AnswerCache | None = None,
        fallback: Judge | None = None,
        concurrency: int = 32,
        max_retries: int = 3,
        base_backoff_s: float = 0.25,
        rpm: float | None = 1200,
    ):
        self.judge = judge
        self.cache = cache
        self.fallback = fallback
        self.concurrency = concurrency
        self.max_retries = max_retries
        self.base_backoff_s = base_backoff_s
        self.limiter = RateLimiter(rpm) if rpm else None

    async def run(self, tasks: Sequence[JudgeTask]) -> FanOutResult:
        result = FanOutResult()
        sem = asyncio.Semaphore(self.concurrency)
        t0 = time.perf_counter()

        async def worker(task: JudgeTask) -> None:
            async with sem:
                result.answers[task.subject_id] = await self._one(task, result)

        await asyncio.gather(*(worker(t) for t in tasks))
        result.wall_ms = (time.perf_counter() - t0) * 1000
        return result

    async def _one(self, task: JudgeTask, result: FanOutResult) -> dict[str, Answer]:
        version = self.judge.model_version
        questions = list(task.questions)
        cached = self.cache.get_many(task.subject_id, version, questions) if self.cache else {}
        missing = [q for q in questions if q.key not in cached]
        if not missing:
            return cached

        attempt, last_err = 0, ""
        while attempt <= self.max_retries:
            attempt += 1
            try:
                if self.limiter:
                    await self.limiter.acquire()
                judgment = await self.judge.ask(task.state, missing)
                result.usage.add(judgment.usage)
                result.latencies_ms.append(judgment.latency_ms)
                if self.cache:
                    self.cache.put_many(task.subject_id, version, missing, judgment.answers)
                return {**cached, **judgment.answers}
            except JudgeError as e:
                last_err = str(e)
                if not e.retryable:
                    break
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
            if attempt <= self.max_retries:
                # Full-jitter exponential backoff.
                await asyncio.sleep(random.uniform(0, self.base_backoff_s * 2 ** (attempt - 1)))

        result.dead_letters.append(DeadLetter(task.subject_id, last_err, attempt))
        if self.fallback is None:
            return cached
        fb = await self.fallback.ask(task.state, missing)
        for a in fb.answers.values():
            a.source = "fallback"
        result.fallbacks += 1
        return {**cached, **fb.answers}
