"""Log and change sources: JSONL/plain-text files and the OTel ClickHouse `otel_logs` table."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from .clickhouse import ClickHouse
from .models import Change, LogRecord, normalize_level

_TS_KEYS = ("ts", "timestamp", "Timestamp", "time", "@timestamp")
_SVC_KEYS = ("service", "ServiceName", "service.name", "app")
_LVL_KEYS = ("level", "SeverityText", "severity", "lvl")
_BODY_KEYS = ("body", "Body", "message", "msg")
_TRACE_KEYS = ("trace_id", "TraceId", "traceId")


def parse_ts(v) -> datetime:
    if isinstance(v, (int, float)):
        # Accept seconds, milliseconds or nanoseconds since epoch.
        while v > 1e11:
            v /= 1000
        return datetime.fromtimestamp(v, tz=timezone.utc)
    s = str(v).replace("Z", "+00:00")
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    dt = datetime.fromisoformat(s[:32] if "." in s else s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _first(d: dict, keys: tuple[str, ...], default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _guess_level(line: str) -> str:
    up = line.upper()
    for lvl in ("FATAL", "ERROR", "WARN", "DEBUG"):
        if lvl in up:
            return lvl
    return "INFO"


def read_logs(path: str | Path) -> Iterator[LogRecord]:
    """JSON lines (any of the common key spellings) or plain text, one record per line."""
    now = datetime.now(timezone.utc)
    with Path(path).open() as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                yield LogRecord(ts=now, service="unknown", level=_guess_level(line), body=line)
                continue
            body = _first(d, _BODY_KEYS, "")
            ts = _first(d, _TS_KEYS)
            yield LogRecord(
                ts=parse_ts(ts) if ts is not None else now,
                service=str(_first(d, _SVC_KEYS, "unknown")),
                level=normalize_level(str(_first(d, _LVL_KEYS, "INFO"))),
                body=body if isinstance(body, str) else json.dumps(body),
                trace_id=_first(d, _TRACE_KEYS),
                attrs=d.get("attrs") or d.get("LogAttributes") or {},
            )


def read_changes(path: str | Path) -> list[Change]:
    out = []
    for line in Path(path).read_text().splitlines():
        if line.strip():
            d = json.loads(line)
            out.append(Change(ts=parse_ts(d["ts"]), service=d["service"], summary=d["summary"],
                              change_id=str(d.get("id", ""))))
    return sorted(out, key=lambda c: c.ts)


def clickhouse_logs(
    ch: ClickHouse, start: datetime, end: datetime, table: str = "otel.otel_logs", limit: int = 2_000_000
) -> Iterator[LogRecord]:
    """Pull a time window from the table the OTel ClickHouse exporter writes."""
    sql = f"""
        SELECT Timestamp, ServiceName, SeverityText, Body, TraceId
        FROM {table}
        WHERE Timestamp >= parseDateTime64BestEffort('{start.isoformat()}')
          AND Timestamp <  parseDateTime64BestEffort('{end.isoformat()}')
        ORDER BY Timestamp
        LIMIT {int(limit)}"""
    for r in ch.query(sql):
        yield LogRecord(
            ts=parse_ts(r["Timestamp"]),
            service=r.get("ServiceName") or "unknown",
            level=normalize_level(r.get("SeverityText") or "INFO"),
            body=r.get("Body") or "",
            trace_id=r.get("TraceId") or None,
        )
