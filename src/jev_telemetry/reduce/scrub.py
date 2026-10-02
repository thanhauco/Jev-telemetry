"""Regex PII and secret scrubber.

This is the first privacy layer and runs before anything is stored in a cluster or sent to a judge.
The `sensitive` Noul in the question pack is the second layer, not a replacement.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

# Order matters: more specific patterns run first so a JWT is not half-eaten by the generic token rule.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("AWS_KEY", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("API_KEY", re.compile(r"\b(?:sk|pk|rk|ghp|gho|xox[abp])[-_][A-Za-z0-9_-]{16,}\b")),
    ("BEARER", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}")),
    (
        "SECRET",
        re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)\b(\s*[=:]\s*)(?!<[A-Z_]+>)(\"[^\"]*\"|'[^']*'|[^\s,;\"]+)"),
    ),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("CARD", re.compile(r"(?<![\w-])\d(?:[ -]?\d){12,18}(?![\w-])")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("PHONE", re.compile(r"(?<![\w.])\+?\d{1,2}[ .-]?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b")),
    ("IPV4", re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")),
]


def _luhn_ok(digits: str) -> bool:
    nums = [int(c) for c in digits if c.isdigit()]
    if not 13 <= len(nums) <= 19:
        return False
    total = 0
    for i, n in enumerate(reversed(nums)):
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


@dataclass
class ScrubResult:
    text: str
    hits: Counter

    @property
    def dirty(self) -> bool:
        return bool(self.hits)


class Scrubber:
    def __init__(self, redact_ips: bool = False, extra: dict[str, str] | None = None):
        self.patterns = [(n, p) for n, p in _PATTERNS if redact_ips or n != "IPV4"]
        for name, rx in (extra or {}).items():
            self.patterns.append((name, re.compile(rx)))

    def scrub(self, text: str) -> ScrubResult:
        hits: Counter = Counter()
        for name, pattern in self.patterns:

            def repl(m: re.Match[str], name: str = name) -> str:
                if name == "CARD" and not _luhn_ok(m.group(0)):
                    return m.group(0)
                hits[name] += 1
                if name == "SECRET":
                    return f"{m.group(1)}{m.group(2)}<SECRET>"
                return f"<{name}>"

            text = pattern.sub(repl, text)
        return ScrubResult(text, hits)
