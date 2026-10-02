"""Replay a JSONL log file into an OTLP/HTTP endpoint, with timestamps rebased to end now."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timezone

import httpx

from .models import LogRecord

SEVERITY_NUMBER = {"TRACE": 1, "DEBUG": 5, "INFO": 9, "WARN": 13, "ERROR": 17, "FATAL": 21}


def to_otlp(records: list[LogRecord], shift_s: float = 0.0) -> dict:
    by_service: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        rec = {
            "timeUnixNano": str(int((r.ts.timestamp() + shift_s) * 1e9)),
            "severityText": r.level,
            "severityNumber": SEVERITY_NUMBER.get(r.level, 9),
            "body": {"stringValue": r.body},
        }
        if r.trace_id and len(r.trace_id) == 32:
            rec["traceId"] = r.trace_id
        by_service[r.service].append(rec)
    return {
        "resourceLogs": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": svc}}]},
                "scopeLogs": [{"scope": {"name": "jev-telemetry.replay"}, "logRecords": recs}],
            }
            for svc, recs in by_service.items()
        ]
    }


def replay(records: Iterable[LogRecord], endpoint: str = "http://localhost:4318", batch: int = 2000) -> int:
    recs = list(records)
    if not recs:
        return 0
    shift = datetime.now(timezone.utc).timestamp() - max(r.ts for r in recs).timestamp()
    sent = 0
    with httpx.Client(timeout=30) as client:
        for i in range(0, len(recs), batch):
            chunk = recs[i : i + batch]
            resp = client.post(f"{endpoint.rstrip('/')}/v1/logs", json=to_otlp(chunk, shift))
            resp.raise_for_status()
            sent += len(chunk)
    return sent
