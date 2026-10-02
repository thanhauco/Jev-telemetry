"""Quality and calibration metrics. Calibration holds over groups of predictions, so measure it here."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass
class BinaryMetrics:
    n: int
    positives: int
    threshold: float
    tp: int
    fp: int
    fn: int
    tn: int

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    def to_dict(self) -> dict:
        return {
            "n": self.n, "positives": self.positives, "threshold": self.threshold,
            "tp": self.tp, "fp": self.fp, "fn": self.fn, "tn": self.tn,
            "precision": round(self.precision, 4), "recall": round(self.recall, 4), "f1": round(self.f1, 4),
        }


def binary_metrics(y: Sequence[bool], p: Sequence[float], threshold: float = 0.5) -> BinaryMetrics:
    tp = fp = fn = tn = 0
    for yi, pi in zip(y, p):
        pred = pi >= threshold
        if pred and yi:
            tp += 1
        elif pred:
            fp += 1
        elif yi:
            fn += 1
        else:
            tn += 1
    return BinaryMetrics(len(y), sum(1 for v in y if v), threshold, tp, fp, fn, tn)


def reliability(y: Sequence[bool], p: Sequence[float], bins: int = 10) -> list[dict]:
    """Reliability curve: for each probability bin, mean predicted probability vs observed frequency."""
    buckets: list[list[tuple[bool, float]]] = [[] for _ in range(bins)]
    for yi, pi in zip(y, p):
        buckets[min(int(pi * bins), bins - 1)].append((bool(yi), pi))
    out = []
    for i, b in enumerate(buckets):
        if not b:
            continue
        out.append({
            "bin": f"{i / bins:.1f}-{(i + 1) / bins:.1f}",
            "n": len(b),
            "mean_p": round(sum(pi for _, pi in b) / len(b), 4),
            "observed": round(sum(1 for yi, _ in b if yi) / len(b), 4),
        })
    return out


def ece(y: Sequence[bool], p: Sequence[float], bins: int = 10) -> float:
    """Expected calibration error: bin-weighted |observed - predicted|."""
    n = len(y)
    if not n:
        return 0.0
    return round(sum(r["n"] / n * abs(r["observed"] - r["mean_p"]) for r in reliability(y, p, bins)), 4)


def brier(y: Sequence[bool], p: Sequence[float]) -> float:
    return round(sum((pi - float(yi)) ** 2 for yi, pi in zip(y, p)) / len(y), 4) if y else 0.0


def best_threshold_for_precision(y: Sequence[bool], p: Sequence[float], target_precision: float) -> float | None:
    """Lowest threshold that reaches the target precision, which maximizes recall at that precision."""
    for t in sorted(set(round(x, 3) for x in p)):
        m = binary_metrics(y, p, t)
        if m.tp and m.precision >= target_precision:
            return t
    return None
