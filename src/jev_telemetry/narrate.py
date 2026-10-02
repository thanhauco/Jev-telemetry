"""L4: narrative for the top-K incidents only.

Jev returns probabilities, not explanations, so prose comes from here: a deterministic template
narrator by default, or Claude when `anthropic` is installed and credentials are configured.
Clusters flagged `sensitive_data` never have their example lines sent to the narrator.
"""

from __future__ import annotations

import json
from typing import Protocol

from .correlate import Incident

NARRATOR_SYSTEM = """You are an SRE incident assistant. You receive an evidence bundle produced by a \
log triage pipeline: log patterns with occurrence counts and calibrated probabilities from a \
classifier, linked deploys or config changes, ranked root-cause candidates, and scored hypotheses.

The probabilities are classifier outputs, not reasoning. Treat them as signals to weigh, and say so \
when the evidence is thin. Do not invent facts that are not in the bundle.

Respond in Markdown with these sections: Summary (two or three sentences), Likely cause, Evidence \
(bullets that cite the patterns and changes), Next steps (concrete checks or mitigations)."""

HYPOTHESIS_SYSTEM = """You are an SRE incident assistant. Given an evidence bundle from a log triage \
pipeline, propose distinct, testable root-cause hypotheses. Return only a JSON array of strings, one \
sentence each, most plausible first."""


class Narrator(Protocol):
    def propose_hypotheses(self, inc: Incident, n: int = 6) -> list[str]: ...

    def narrate(self, inc: Incident) -> str: ...


def incident_bundle(inc: Incident, include_examples: bool = True) -> dict:
    members = sorted(inc.members, key=lambda r: -r.priority)[:10]
    return {
        "incident_id": inc.incident_id,
        "window": [inc.start.isoformat(), inc.end.isoformat()],
        "services": inc.services,
        "total_lines": inc.lines,
        "patterns": [
            {
                "service": r.cluster.service,
                "level": r.cluster.level,
                "template": r.cluster.template,
                "occurrences": r.cluster.count,
                "first_seen": r.cluster.first_seen.isoformat() if r.cluster.first_seen else None,
                "category": r.choice("category"),
                "p_actionable": round(r.p("actionable"), 3),
                "p_root_cause": round(r.p("root_cause"), 3),
                "expected_severity": r.expected("severity"),
                **(
                    {"examples": r.cluster.examples[:2]}
                    if include_examples and "sensitive_data" not in r.flags
                    else {}
                ),
            }
            for r in members
        ],
        "root_cause_ranking": [
            {"service": r.cluster.service, "template": r.cluster.template, "score": s} for r, s in inc.root_causes[:5]
        ],
        "linked_changes": [
            {"service": c.change.service, "summary": c.change.summary, "at": c.change.ts.isoformat(),
             "p_related": round(c.p_related, 3)}
            for c in inc.changes[:5]
        ],
        "hypotheses": [{"text": h.text, "p_supported": round(h.p_supported, 3)} for h in inc.hypotheses],
    }


class TemplateNarrator:
    """Deterministic narrator and hypothesis generator. No network, no model."""

    def propose_hypotheses(self, inc: Incident, n: int = 6) -> list[str]:
        out: list[str] = []
        for c in inc.changes[:3]:
            out.append(f"The change to {c.change.service} ({c.change.summary}) introduced the failure.")
        for r, _ in inc.root_causes[:3]:
            cat = (r.choice("category") or "unknown").replace("_", " ")
            out.append(f"A {cat} problem in {r.cluster.service} is the origin: {r.cluster.template}")
        out.append("A shared downstream dependency degraded and the errors are symptoms of it.")
        out.append("A traffic spike exhausted capacity across the affected services.")
        seen, uniq = set(), []
        for h in out:
            if h not in seen:
                seen.add(h)
                uniq.append(h)
        return uniq[:n]

    def narrate(self, inc: Incident) -> str:
        top_rc = inc.root_causes[0][0] if inc.root_causes else None
        lines = [
            f"### {inc.incident_id}: {', '.join(inc.services)}",
            "",
            f"{len(inc.members)} log patterns, {inc.lines} lines, "
            f"{inc.start:%Y-%m-%d %H:%M:%S} to {inc.end:%H:%M:%S} UTC.",
            "",
        ]
        if top_rc:
            lines += [
                f"**Most likely origin:** `{top_rc.cluster.service}`: `{top_rc.cluster.template}` "
                f"(P(root cause)={top_rc.p('root_cause'):.2f}, first seen {top_rc.cluster.first_seen:%H:%M:%S}).",
                "",
            ]
        if inc.changes:
            c = inc.changes[0]
            lines += [f"**Linked change:** {c.change.service}: {c.change.summary} (P(related)={c.p_related:.2f}).", ""]
        if inc.hypotheses:
            lines.append("**Hypotheses, by P(supported):**")
            lines += [f"- {h.p_supported:.2f}: {h.text}" for h in inc.hypotheses[:5]]
            lines.append("")
        if inc.timeline:
            lines.append("**Timeline:**")
            lines += [f"- {e.ts:%H:%M:%S} [{e.event_type}] {e.label}" for e in inc.timeline[:12]]
            lines.append("")
        lines.append("_Probabilities are classifier outputs, not explanations. Verify before acting._")
        return "\n".join(lines)


class ClaudeNarrator:
    """Claude-backed narrator. Requires `pip install jev-telemetry[narrate]` and Anthropic credentials."""

    def __init__(self, model: str = "claude-opus-5", effort: str = "medium", max_tokens: int = 16000):
        import anthropic  # optional dependency

        self._anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self._fallback = TemplateNarrator()

    def _call(self, system: str, user: str) -> str | None:
        # Server-side refusal fallbacks in "default" mode route a declined request to the model
        # Anthropic recommends for that refusal category.
        resp = self.client.beta.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": self.effort},
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        if resp.stop_reason == "refusal":
            return None
        return "".join(b.text for b in resp.content if b.type == "text").strip() or None

    def propose_hypotheses(self, inc: Incident, n: int = 6) -> list[str]:
        bundle = incident_bundle(inc, include_examples=False)
        try:
            text = self._call(HYPOTHESIS_SYSTEM, f"Propose up to {n} hypotheses.\n\n{json.dumps(bundle, indent=1)}")
            items = json.loads(text[text.index("[") : text.rindex("]") + 1]) if text else []
            hyps = [str(h) for h in items if str(h).strip()][:n]
        except (self._anthropic.APIError, ValueError):
            hyps = []
        return hyps or self._fallback.propose_hypotheses(inc, n)

    def narrate(self, inc: Incident) -> str:
        bundle = incident_bundle(inc)
        try:
            text = self._call(NARRATOR_SYSTEM, f"Evidence bundle:\n\n{json.dumps(bundle, indent=1)}")
        except self._anthropic.APIError:
            text = None
        return text or self._fallback.narrate(inc)


def make_narrator(kind: str, **kwargs) -> Narrator:
    if kind == "template":
        return TemplateNarrator()
    if kind == "claude":
        return ClaudeNarrator(**kwargs)
    raise ValueError(f"unknown narrator {kind!r} (expected 'template' or 'claude')")
