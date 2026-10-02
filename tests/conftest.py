import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from jev_telemetry.models import Answer, Cluster, QType

T0 = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def noul(key, p, source="jev"):
    return Answer(key, QType.NOUL, p, {"yes": p, "no": 1 - p}, source=source)


def score(key, probs):
    probs = {str(i): p for i, p in enumerate(probs)}
    return Answer(key, QType.SCORE, int(max(probs, key=probs.get)), probs, 0.5)


def cluster(template="something happened", level="INFO", count=100, service="svc", minute=0, **kw):
    return Cluster(
        template_id=f"{service}:{template}"[:16], service=service, template=template, level=level, count=count,
        first_seen=T0 + timedelta(minutes=minute), last_seen=T0 + timedelta(minutes=minute + 5), **kw,
    )


@pytest.fixture
def t0():
    return T0
