"""Phase 1 offline eval: run the question pack on the golden set and report quality, calibration,
latency and cost. Re-run on every model-version bump and on a schedule to catch drift."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..judge import FanOut, JudgeTask
from ..models import QType
from ..questions import QuestionPack
from ..router import Decision, Router
from .golden import GoldenRow
from .metrics import best_threshold_for_precision, binary_metrics, brier, ece, reliability

SURFACED = {Decision.ESCALATE, Decision.TRIAGE, Decision.REVIEW}


@dataclass
class EvalReport:
    model_version: str
    rows: int
    noul: dict[str, dict] = field(default_factory=dict)
    choice: dict[str, dict] = field(default_factory=dict)
    score: dict[str, dict] = field(default_factory=dict)
    routing: dict[str, Any] = field(default_factory=dict)
    performance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return self.__dict__


async def evaluate(
    rows: list[GoldenRow],
    pack: QuestionPack,
    fanout: FanOut,
    router: Router,
    target_precision: float = 0.9,
    price_per_m_input: float = 0.042,
) -> EvalReport:
    tasks = [JudgeTask(r.cluster.template_id, r.cluster.to_state(), list(pack)) for r in rows]
    res = await fanout.run(tasks)
    report = EvalReport(model_version=fanout.judge.model_version, rows=len(rows))

    for q in pack:
        labeled = [(r, res.answers.get(r.cluster.template_id, {}).get(q.key)) for r in rows
                   if r.labels.get(q.key) is not None]
        labeled = [(r, a) for r, a in labeled if a is not None]
        if not labeled:
            continue
        if q.type is QType.NOUL:
            y = [bool(r.labels[q.key]) for r, _ in labeled]
            p = [a.p_yes for _, a in labeled]
            report.noul[q.key] = {
                **binary_metrics(y, p, 0.5).to_dict(),
                "ece": ece(y, p),
                "brier": brier(y, p),
                f"threshold_for_precision_{target_precision}": best_threshold_for_precision(y, p, target_precision),
                "reliability": reliability(y, p),
            }
        elif q.type is QType.CHOICE:
            y = [str(r.labels[q.key]) for r, _ in labeled]
            pred = [str(a.value) for _, a in labeled]
            conf = [a.probabilities.get(str(a.value), 0.0) for _, a in labeled]
            correct = [yi == pi for yi, pi in zip(y, pred)]
            report.choice[q.key] = {
                "n": len(y),
                "accuracy": round(sum(correct) / len(y), 4),
                "top1_ece": ece(correct, conf),
                "confusions": _confusions(y, pred),
            }
        else:
            y = [int(r.labels[q.key]) for r, _ in labeled]
            exp = [a.expected_score() for _, a in labeled]
            arg = [int(a.value) for _, a in labeled]
            report.score[q.key] = {
                "n": len(y),
                "exact": round(sum(1 for a, b in zip(y, arg) if a == b) / len(y), 4),
                "within_1": round(sum(1 for a, b in zip(y, arg) if abs(a - b) <= 1) / len(y), 4),
                "mae_expected": round(sum(abs(a - b) for a, b in zip(y, exp)) / len(y), 4),
            }

    # Routing: the false-suppression rate is the metric that matters most.
    routed = [(r, router.route(r.cluster, res.answers.get(r.cluster.template_id, {}))) for r in rows]
    act = [(r, x) for r, x in routed if r.labels.get("actionable") is not None]
    truly_actionable = [x for r, x in act if r.labels["actionable"]]
    suppressed = [(r, x) for r, x in act if x.decision is Decision.SUPPRESS]
    surfaced = [(r, x) for r, x in act if x.decision in SURFACED]
    report.routing = {
        "decisions": _count(x.decision.value for _, x in routed),
        "false_suppression_rate": round(
            sum(1 for x in truly_actionable if x.decision is Decision.SUPPRESS) / len(truly_actionable), 4
        ) if truly_actionable else None,
        "suppression_precision": round(
            sum(1 for r, _ in suppressed if not r.labels["actionable"]) / len(suppressed), 4
        ) if suppressed else None,
        "surfaced_precision": round(sum(1 for r, _ in surfaced if r.labels["actionable"]) / len(surfaced), 4)
        if surfaced else None,
        "surfaced_recall": round(
            sum(1 for x in truly_actionable if x.decision in SURFACED) / len(truly_actionable), 4
        ) if truly_actionable else None,
        "false_suppressions": [r.id for r, x in act if r.labels["actionable"] and x.decision is Decision.SUPPRESS],
    }
    report.performance = {
        "requests": res.usage.requests,
        "input_tokens": res.usage.input_tokens,
        "est_cost_usd": round(res.usage.input_tokens / 1e6 * price_per_m_input, 6),
        "p50_ms": round(res.percentile(50), 1),
        "p95_ms": round(res.percentile(95), 1),
        "wall_ms": round(res.wall_ms, 1),
        "fallbacks": res.fallbacks,
        "dead_letters": len(res.dead_letters),
    }
    return report


def _count(items) -> dict[str, int]:
    out: dict[str, int] = {}
    for i in items:
        out[i] = out.get(i, 0) + 1
    return out


def _confusions(y: list[str], pred: list[str], top: int = 5) -> list[dict]:
    pairs = _count(f"{a}->{b}" for a, b in zip(y, pred) if a != b)
    return [{"label->pred": k, "n": v} for k, v in sorted(pairs.items(), key=lambda kv: -kv[1])[:top]]


def format_report(rep: EvalReport) -> str:
    lines = [f"Eval: {rep.rows} golden rows, model {rep.model_version}", ""]
    if rep.noul:
        lines.append(f"{'noul':<14}{'n':>5}{'prec':>8}{'recall':>8}{'f1':>8}{'ECE':>8}{'brier':>8}")
        for k, m in rep.noul.items():
            lines.append(f"{k:<14}{m['n']:>5}{m['precision']:>8.3f}{m['recall']:>8.3f}{m['f1']:>8.3f}"
                         f"{m['ece']:>8.3f}{m['brier']:>8.3f}")
        lines.append("")
    for k, m in rep.choice.items():
        lines.append(f"choice {k}: accuracy {m['accuracy']:.3f} (n={m['n']}), top-1 ECE {m['top1_ece']:.3f}")
    for k, m in rep.score.items():
        lines.append(f"score  {k}: exact {m['exact']:.3f}, within-1 {m['within_1']:.3f}, "
                     f"MAE(E[level]) {m['mae_expected']:.3f}")
    r = rep.routing
    lines += [
        "",
        f"routing: {r['decisions']}",
        f"  false-suppression rate: {r['false_suppression_rate']}  (suppressed real issues: "
        f"{r['false_suppressions'] or 'none'})",
        f"  suppression precision: {r['suppression_precision']}",
        f"  surfaced precision / recall: {r['surfaced_precision']} / {r['surfaced_recall']}",
        "",
        f"perf: {rep.performance}",
    ]
    return "\n".join(lines)
