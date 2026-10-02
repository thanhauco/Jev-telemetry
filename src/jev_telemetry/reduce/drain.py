"""A compact Drain-style template miner (He et al., 2017).

Lines are masked (numbers, ids, hex, durations), routed by token count and leading token through a
fixed-depth tree, then matched to the most similar group by positional token overlap. Differing
positions become `<*>`. This keeps the repo dependency-free; drain3 is a drop-in alternative.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

WILDCARD = "<*>"

_MASKS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE), "<UUID>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "<IP>"),
    (re.compile(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{16,}\b", re.IGNORECASE), "<HEX>"),
    (re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|us|µs|ns|m|h|kb|mb|gb|b)\b", re.IGNORECASE), "<DUR>"),
    (re.compile(r"(?<![A-Za-z<])[-+]?\d+(?:\.\d+)?(?![A-Za-z>])"), "<NUM>"),
]
_SPLIT = re.compile(r"\s+")


def mask(line: str) -> str:
    for pattern, token in _MASKS:
        line = pattern.sub(token, line)
    return line


def template_hash(service: str, template: str) -> str:
    return hashlib.sha1(f"{service}\x00{template}".encode()).hexdigest()[:16]


@dataclass
class _Group:
    tokens: list[str]
    gid: int
    size: int = 0

    @property
    def template(self) -> str:
        return " ".join(self.tokens)


@dataclass
class DrainMiner:
    sim_threshold: float = 0.5
    depth: int = 2  # prefix tokens used for routing below the length layer
    max_children: int = 100
    _tree: dict = field(default_factory=dict)
    _groups: list[_Group] = field(default_factory=list)

    def add(self, line: str) -> tuple[int, str]:
        """Insert a line; return (group id, current template)."""
        tokens = [t for t in _SPLIT.split(mask(line.strip())) if t]
        if not tokens:
            tokens = ["<EMPTY>"]
        leaf = self._leaf(tokens)
        group = self._best_match(leaf, tokens)
        if group is None:
            group = _Group(tokens=list(tokens), gid=len(self._groups))
            self._groups.append(group)
            leaf.append(group)
        else:
            group.tokens = [a if a == b else WILDCARD for a, b in zip(group.tokens, tokens)]
        group.size += 1
        return group.gid, group.template

    def template_of(self, gid: int) -> str:
        return self._groups[gid].template

    @property
    def groups(self) -> list[_Group]:
        return self._groups

    def _leaf(self, tokens: list[str]) -> list[_Group]:
        node = self._tree.setdefault(len(tokens), {})
        for tok in tokens[: self.depth]:
            key = WILDCARD if any(c.isdigit() for c in tok) or tok.startswith("<") else tok
            if key not in node and len(node) >= self.max_children:
                key = WILDCARD
            node = node.setdefault(key, {})
        return node.setdefault("__groups__", [])

    def _best_match(self, groups: list[_Group], tokens: list[str]) -> _Group | None:
        best, best_sim = None, -1.0
        for g in groups:
            same = sum(1 for a, b in zip(g.tokens, tokens) if a == b and a != WILDCARD)
            sim = same / len(tokens)
            if sim > best_sim:
                best, best_sim = g, sim
        return best if best is not None and best_sim >= self.sim_threshold else None
