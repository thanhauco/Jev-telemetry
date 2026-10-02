"""Minimal ClickHouse HTTP client (JSONEachRow in and out). No driver dependency."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from typing import Any

import httpx


class ClickHouse:
    def __init__(
        self,
        url: str | None = None,
        user: str | None = None,
        password: str | None = None,
        database: str = "default",
        timeout_s: float = 30.0,
    ):
        self.url = (url or os.environ.get("CLICKHOUSE_URL", "http://localhost:8123")).rstrip("/")
        self.auth = (
            user or os.environ.get("CLICKHOUSE_USER", "default"),
            password or os.environ.get("CLICKHOUSE_PASSWORD", ""),
        )
        self.database = database
        self._http = httpx.Client(timeout=timeout_s)

    def _post(self, query: str, body: bytes | None = None, **settings: Any) -> httpx.Response:
        resp = self._http.post(
            self.url,
            params={"query": query, "database": self.database, **settings},
            content=body,
            auth=self.auth,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"ClickHouse error {resp.status_code}: {resp.text[:500]}")
        return resp

    def command(self, sql: str) -> None:
        self._post(sql)

    def query(self, sql: str) -> list[dict[str, Any]]:
        text = self._post(f"{sql.rstrip().rstrip(';')} FORMAT JSONEachRow").text
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def insert(self, table: str, rows: Iterable[dict[str, Any]]) -> int:
        payload = [json.dumps(r, default=str) for r in rows]
        if not payload:
            return 0
        self._post(
            f"INSERT INTO {table} FORMAT JSONEachRow",
            "\n".join(payload).encode(),
            date_time_input_format="best_effort",
        )
        return len(payload)

    def close(self) -> None:
        self._http.close()
