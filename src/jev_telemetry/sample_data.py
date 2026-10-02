"""Synthetic logs for a small e-commerce system, with one injected incident.

Scenario: a payments deploy shrinks the DB pool. Pool exhaustion in payments (root cause) causes
query timeouts, checkout timeouts and gateway 504s (symptoms). Around it: routine noise, PII and a
leaked key that the scrubber must catch, plus an unrelated novel crash in inventory.
Every event kind carries ground-truth labels, which become a starter golden set. The `sensitive`
label means "still sensitive after the regex scrubber", which is what the `sensitive` Noul asks.
"""

from __future__ import annotations

import json
import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

R = random.Random(7)


def _ms() -> int:
    return R.randint(3, 900)


def _user() -> str:
    return f"user{R.randint(1000, 9999)}@example.com"


@dataclass
class Kind:
    name: str
    service: str
    level: str
    body: Callable[[], str]
    rate: float  # lines per minute in steady state
    labels: dict
    incident: bool = False  # only emitted during the incident window


def _card() -> str:
    return R.choice(["4111 1111 1111 1111", "5500 0000 0000 0004", "4012 8888 8888 1881"])


KINDS: list[Kind] = [
    Kind("gw_ok", "api-gateway", "INFO",
         lambda: f"GET /api/v1/{R.choice(['cart', 'products', 'orders'])} status=200 duration={_ms()}ms",
         600, dict(actionable=False, benign=True, category="none", severity=0, root_cause=False, sensitive=False)),
    Kind("gw_health", "api-gateway", "DEBUG", lambda: "GET /healthz status=200 duration=1ms",
         120, dict(actionable=False, benign=True, category="none", severity=0, root_cause=False, sensitive=False)),
    Kind("checkout_ok", "checkout", "INFO",
         lambda: f"order {uuid.UUID(int=R.getrandbits(128))} request completed in {_ms()}ms",
         80, dict(actionable=False, benign=True, category="none", severity=0, root_cause=False, sensitive=False)),
    Kind("inv_cache", "inventory", "DEBUG", lambda: f"cache hit for sku-{R.randint(1, 50000)}",
         200, dict(actionable=False, benign=True, category="none", severity=0, root_cause=False, sensitive=False)),
    Kind("pay_heartbeat", "payments", "INFO", lambda: "heartbeat ok, pool active=3 idle=7",
         6, dict(actionable=False, benign=True, category="none", severity=0, root_cause=False, sensitive=False)),
    Kind("auth_login", "auth", "INFO", lambda: f"user logged in {_user()} via password",
         40, dict(actionable=False, benign=True, category="none", severity=0, root_cause=False, sensitive=False)),
    Kind("auth_badtoken", "auth", "WARN", lambda: f"invalid token signature for {_user()}, rejecting request",
         3, dict(actionable=False, benign=True, category="auth", severity=1, root_cause=False, sensitive=False)),
    Kind("checkout_retry", "checkout", "WARN",
         lambda: f"retrying inventory reservation attempt {R.randint(1, 3)}, retry succeeded",
         4, dict(actionable=False, benign=True, category="dependency", severity=1, root_cause=False, sensitive=False)),
    Kind("pay_card_leak", "checkout", "INFO",
         lambda: f"payment attempt card={_card()} email={_user()} amount={R.randint(5, 400)}.00",
         1, dict(actionable=True, benign=False, category="data", severity=2, root_cause=True, sensitive=False)),
    Kind("auth_key_leak", "auth", "WARN",
         lambda: "identity provider config reloaded api_key=sk-test" + "".join(R.choices("abcdef0123456789", k=24)),
         0.05, dict(actionable=True, benign=False, category="config", severity=2, root_cause=True, sensitive=False)),
    # Free-text PII that no regex catches. This is what the `sensitive` Noul is for.
    Kind("addr_leak", "checkout", "INFO",
         lambda: f"printing shipping label for {R.choice(['Maria Garcia', 'Wei Chen', 'Sam Okafor'])}, "
                 f"{R.randint(10, 999)} {R.choice(['Evergreen Terrace', 'Elm Street', 'Harbor Road'])}",
         0.5, dict(actionable=True, benign=False, category="data", severity=2, root_cause=True, sensitive=True)),
    # Incident window.
    Kind("pay_pool", "payments", "ERROR",
         lambda: f"db pool exhausted: {R.randint(10, 60)} requests waiting (max_connections=5)",
         40, dict(actionable=True, benign=False, category="capacity", severity=3, root_cause=True, sensitive=False),
         incident=True),
    Kind("pay_query_timeout", "payments", "ERROR",
         lambda: f"query timeout after 5000ms on orders_tx (attempt {R.randint(1, 3)})",
         25, dict(actionable=True, benign=False, category="timeout", severity=3, root_cause=False, sensitive=False),
         incident=True),
    Kind("checkout_timeout", "checkout", "ERROR",
         lambda: "timeout calling payments: deadline exceeded after 3000ms",
         60, dict(actionable=True, benign=False, category="timeout", severity=3, root_cause=False, sensitive=False),
         incident=True),
    Kind("gw_504", "api-gateway", "ERROR",
         lambda: f"upstream checkout returned 504 for POST /api/v1/checkout duration={R.randint(3000, 3100)}ms",
         70, dict(actionable=True, benign=False, category="dependency", severity=3, root_cause=False, sensitive=False),
         incident=True),
    Kind("inv_panic", "inventory", "FATAL",
         lambda: "panic: unexpected nil pointer dereference in reservation handler",
         0.3, dict(actionable=True, benign=False, category="code_bug", severity=3, root_cause=True, sensitive=False),
         incident=True),
]


def generate(
    out_dir: str | Path,
    minutes: int = 120,
    scale: float = 1.0,
    start: datetime | None = None,
    incident_at_min: int = 60,
    incident_len_min: int = 25,
) -> dict[str, int]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    t0 = start or datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
    inc_start = t0 + timedelta(minutes=incident_at_min + 2)
    inc_end = inc_start + timedelta(minutes=incident_len_min)
    records = []
    for minute in range(minutes):
        base = t0 + timedelta(minutes=minute)
        in_incident = inc_start <= base < inc_end
        incident_traces = [uuid.UUID(int=R.getrandbits(128)).hex for _ in range(8)]
        for k in KINDS:
            if k.incident and not in_incident:
                continue
            lam = k.rate * scale
            n = int(lam) + (1 if R.random() < lam - int(lam) else 0)
            for _ in range(n):
                ts = base + timedelta(seconds=R.random() * 60)
                trace = R.choice(incident_traces) if k.incident else uuid.UUID(int=R.getrandbits(128)).hex
                records.append({"ts": ts.isoformat(), "service": k.service, "level": k.level,
                                "body": k.body(), "trace_id": trace})
    records.sort(key=lambda r: r["ts"])
    with (out / "logs.jsonl").open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

    changes = [
        {"id": "chg-101", "ts": (t0 + timedelta(minutes=15)).isoformat(), "service": "auth",
         "summary": "auth v3.2.0: update login page copy"},
        {"id": "chg-102", "ts": (t0 + timedelta(minutes=incident_at_min)).isoformat(), "service": "payments",
         "summary": "payments v2.14.0: reduce db pool max_connections from 50 to 5"},
        {"id": "chg-103", "ts": (t0 + timedelta(minutes=incident_at_min + 1)).isoformat(), "service": "inventory",
         "summary": "inventory v1.9.3: new reservation handler"},
    ]
    with (out / "changes.jsonl").open("w") as f:
        for c in changes:
            f.write(json.dumps(c) + "\n")

    with (out / "golden.jsonl").open("w") as f:
        for k in KINDS:
            for j in range(3):
                body = k.body()
                f.write(json.dumps({
                    "id": f"{k.name}-{j}", "service": k.service, "level": k.level, "body": body,
                    "count": max(1, int(k.rate * 10)), "labels": k.labels,
                }) + "\n")
    return {"logs": len(records), "changes": len(changes), "golden": len(KINDS) * 3}
