"""Golden set: hand-labeled clusters from past incidents (Phase 0 exit criterion: 300-1000 rows).

One JSON object per line:

    {"id": "g-001", "service": "payments", "level": "ERROR",
     "template": "db pool exhausted: <NUM> waiting",          # or "body" with a raw example line
     "examples": ["db pool exhausted: 37 waiting"], "count": 412,
     "labels": {"actionable": true, "benign": false, "category": "capacity", "severity": 3,
                "root_cause": true, "sensitive": false}}

Labels are optional per row; each metric uses only the rows that carry its label.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import Cluster, normalize_level
from ..reduce.drain import mask, template_hash
from ..reduce.scrub import Scrubber


@dataclass
class GoldenRow:
    id: str
    cluster: Cluster
    labels: dict[str, Any] = field(default_factory=dict)


def load_golden(path: str | Path, scrubber: Scrubber | None = None) -> list[GoldenRow]:
    scrubber = scrubber or Scrubber()
    rows = []
    for i, line in enumerate(Path(path).read_text().splitlines()):
        if not line.strip():
            continue
        d = json.loads(line)
        service = d.get("service", "unknown")
        template = d.get("template") or mask(scrubber.scrub(d["body"]).text)
        examples = [scrubber.scrub(e).text for e in d.get("examples") or ([d["body"]] if d.get("body") else [])]
        c = Cluster(
            template_id=template_hash(service, template),
            service=service,
            template=template,
            level=normalize_level(d.get("level", "INFO")),
            count=int(d.get("count", 1)),
            examples=examples[:3],
        )
        rows.append(GoldenRow(id=str(d.get("id", f"row-{i}")), cluster=c, labels=d.get("labels") or {}))
    return rows


def export_for_labeling(clusters: list[Cluster], path: str | Path) -> int:
    """Write clusters as unlabeled golden rows so a human can fill in `labels`."""
    blank = {"actionable": None, "benign": None, "category": None, "severity": None,
             "root_cause": None, "sensitive": None}
    with Path(path).open("w") as f:
        f.writelines(json.dumps({
                "id": c.template_id, "service": c.service, "level": c.level, "template": c.template,
                "examples": c.examples, "count": c.count, "labels": blank,
            }) + "\n" for c in clusters)
    return len(clusters)
