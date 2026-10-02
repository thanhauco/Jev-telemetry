from .base import Judge, JudgeError, Judgment, Usage
from .cache import AnswerCache, CacheStats
from .fanout import DeadLetter, FanOut, FanOutResult, JudgeTask, RateLimiter
from .heuristic import HeuristicJudge
from .jev_http import DEFAULT_ENDPOINT, DEFAULT_MODEL, JevHTTPJudge


def make_judge(backend: str, **kwargs) -> Judge:
    if backend == "heuristic":
        return HeuristicJudge()
    if backend == "jev":
        return JevHTTPJudge(**kwargs)
    raise ValueError(f"unknown judge backend {backend!r} (expected 'jev' or 'heuristic')")


__all__ = [
    "DEFAULT_ENDPOINT",
    "DEFAULT_MODEL",
    "AnswerCache",
    "CacheStats",
    "DeadLetter",
    "FanOut",
    "FanOutResult",
    "HeuristicJudge",
    "JevHTTPJudge",
    "Judge",
    "JudgeError",
    "JudgeTask",
    "Judgment",
    "RateLimiter",
    "Usage",
    "make_judge",
]
