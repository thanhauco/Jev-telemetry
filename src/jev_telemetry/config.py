"""Configuration loading: YAML file merged over defaults, then environment overrides."""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "judge": {
        "backend": "heuristic",
        "model": "jev-1.13.0",
        "endpoint": "https://api.typesafe.ai/v1/systemone",
        "fallback": "heuristic",
        "concurrency": 32,
        "max_retries": 3,
        "rpm": 1200,
        "timeout_s": 30,
    },
    "cache": {"path": ".jev-cache/answers.sqlite", "ttl_days": 30},
    "pack": "config/question_pack.yaml",
    "reduce": {"sim_threshold": 0.5, "redact_ips": False},
    "router": {},
    "known_issues": {},
    "runbooks": {},
    "correlate": {
        "enabled": True,
        "same_issue_threshold": 0.6,
        "max_gap_s": 600,
        "change_lookback_s": 3600,
        "change_threshold": 0.5,
        "topology": None,
    },
    "narrate": {"kind": "template", "top_k": 3, "model": "claude-opus-5", "effort": "medium"},
    "pricing": {"usd_per_m_input": 0.042, "billing": "per_question"},
    "sink": {"kind": "jsonl", "out_dir": "out", "clickhouse_database": "jev"},
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    cfg = copy.deepcopy(DEFAULTS)
    base_dir = Path.cwd()
    if path:
        p = Path(path)
        cfg = _merge(cfg, yaml.safe_load(p.read_text()) or {})
        base_dir = p.resolve().parent.parent if p.resolve().parent.name == "config" else p.resolve().parent
    if os.environ.get("JEV_BACKEND"):
        cfg["judge"]["backend"] = os.environ["JEV_BACKEND"]
    if os.environ.get("JEV_MODEL"):
        cfg["judge"]["model"] = os.environ["JEV_MODEL"]
    pack = Path(cfg["pack"])
    if not pack.is_absolute() and not pack.exists():
        cfg["pack"] = str(base_dir / pack)
    return cfg
