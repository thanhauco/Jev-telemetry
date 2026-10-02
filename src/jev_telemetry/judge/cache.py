"""Answer cache keyed by (subject id, model version, question fingerprint).

Logs repeat heavily, so a template is judged once and every repeat is free. The model version is
part of the key: bumping it invalidates answers automatically, and you should re-run the golden set.
Fallback answers are never cached, so a Jev outage does not poison later runs.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..models import Answer, Question


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


class AnswerCache:
    def __init__(self, path: str | Path = ":memory:", ttl_s: float | None = None):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        self.ttl_s = ttl_s
        self.stats = CacheStats()
        with self._lock:
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS answers (
                       subject_id TEXT NOT NULL,
                       model_version TEXT NOT NULL,
                       question_fp TEXT NOT NULL,
                       answer TEXT NOT NULL,
                       created_at REAL NOT NULL,
                       PRIMARY KEY (subject_id, model_version, question_fp))"""
            )
            self._db.commit()

    def get_many(self, subject_id: str, model_version: str, questions: list[Question]) -> dict[str, Answer]:
        if not questions:
            return {}
        fps = {q.fingerprint(): q for q in questions}
        marks = ",".join("?" * len(fps))
        with self._lock:
            rows = self._db.execute(
                f"SELECT question_fp, answer, created_at FROM answers "
                f"WHERE subject_id=? AND model_version=? AND question_fp IN ({marks})",
                (subject_id, model_version, *fps),
            ).fetchall()
        now = time.time()
        found: dict[str, Answer] = {}
        for fp, blob, created in rows:
            if self.ttl_s is not None and now - created > self.ttl_s:
                continue
            a = Answer.from_dict(json.loads(blob))
            a.source = "cache"
            found[fps[fp].key] = a
        self.stats.hits += len(found)
        self.stats.misses += len(questions) - len(found)
        return found

    def put_many(
        self, subject_id: str, model_version: str, questions: list[Question], answers: dict[str, Answer]
    ) -> None:
        now = time.time()
        rows = [
            (subject_id, model_version, q.fingerprint(), json.dumps(answers[q.key].to_dict()), now)
            for q in questions
            if q.key in answers and answers[q.key].source not in ("fallback", "cache")
        ]
        if not rows:
            return
        with self._lock:
            self._db.executemany("INSERT OR REPLACE INTO answers VALUES (?,?,?,?,?)", rows)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()
