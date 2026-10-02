from conftest import run

from jev_telemetry.judge import AnswerCache, FanOut, HeuristicJudge, JudgeError, JudgeTask, Judgment, Usage
from jev_telemetry.models import Answer, QType, Question

Q = [Question("actionable", QType.NOUL, "Act?"), Question("benign", QType.NOUL, "Noise?")]


class Flaky:
    name, model_version = "flaky", "v1"

    def __init__(self, fail_times=0, retryable=True):
        self.calls = 0
        self.fail_times = fail_times
        self.retryable = retryable

    async def ask(self, state, questions):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise JudgeError("boom", retryable=self.retryable)
        return Judgment({q.key: Answer(q.key, QType.NOUL, 0.7) for q in questions}, Usage(10, 1), latency_ms=5)

    async def aclose(self):
        pass


def tasks(n=3):
    return [JudgeTask(f"t{i}", {"template": f"t{i}"}, Q) for i in range(n)]


def test_cache_makes_repeats_free():
    judge, cache = Flaky(), AnswerCache()
    fo = FanOut(judge, cache=cache, rpm=None)
    run(fo.run(tasks()))
    assert judge.calls == 3
    res = run(fo.run(tasks()))
    assert judge.calls == 3
    assert all(a.source == "cache" for ans in res.answers.values() for a in ans.values())
    assert cache.stats.hit_rate == 0.5


def test_model_version_bump_invalidates_cache():
    cache = AnswerCache()
    run(FanOut(Flaky(), cache=cache, rpm=None).run(tasks(1)))
    j2 = Flaky()
    j2.model_version = "v2"
    run(FanOut(j2, cache=cache, rpm=None).run(tasks(1)))
    assert j2.calls == 1


def test_retries_then_succeeds():
    judge = Flaky(fail_times=2)
    res = run(FanOut(judge, max_retries=3, base_backoff_s=0.001, rpm=None).run(tasks(1)))
    assert judge.calls == 3 and not res.dead_letters
    assert res.answers["t0"]["actionable"].p_yes == 0.7


def test_dead_letter_and_fallback_not_cached():
    cache = AnswerCache()
    judge = Flaky(fail_times=99, retryable=False)
    fo = FanOut(judge, cache=cache, fallback=HeuristicJudge(), base_backoff_s=0.001, rpm=None)
    res = run(fo.run(tasks(2)))
    assert judge.calls == 2  # non-retryable: one attempt each
    assert len(res.dead_letters) == 2 and res.fallbacks == 2
    assert all(a.source == "fallback" for ans in res.answers.values() for a in ans.values())
    assert cache.get_many("t0", "flaky", Q) == {} or judge.model_version != "flaky"
    assert cache.get_many("t0", judge.model_version, Q) == {}


def test_concurrency_bound():
    import asyncio

    class Slow(Flaky):
        active = peak = 0

        async def ask(self, state, questions):
            Slow.active += 1
            Slow.peak = max(Slow.peak, Slow.active)
            await asyncio.sleep(0.01)
            Slow.active -= 1
            return await super().ask(state, questions)

    run(FanOut(Slow(), concurrency=4, rpm=None).run(tasks(20)))
    assert Slow.peak == 4
