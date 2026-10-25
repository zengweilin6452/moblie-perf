"""SQLite 存储（WAL）骨架：聚合指标与 run 归档索引。

设计方案 §5/§10：默认 <repo>/data/mobile_perf.db，WAL + busy_timeout=5s；
原始 CSV 永不入库。本票仅落会话索引，metrics/events/baselines 等表后续票建。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

_BUSY_TIMEOUT_MS = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    run_id          TEXT PRIMARY KEY,
    solo_session_id TEXT,
    serial          TEXT,
    target_pkg      TEXT,
    items_json      TEXT,
    started_at      TEXT,
    stopped_at      TEXT,
    duration_s      REAL,
    host_run_id     TEXT,
    status          TEXT,
    run_dir         TEXT
);
"""


class PerfStore:
    """组件的 SQLite 存储入口；同进程会话串行（xdist 到 M4 验证）。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        self._conn.execute("PRAGMA journal_mode = wal")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def record_session(self, session: dict[str, Any]) -> None:
        if not session.get("run_id"):
            raise ValueError("session 记录必须包含 run_id")
        self._conn.execute(
            """
            INSERT OR REPLACE INTO sessions (
                run_id, solo_session_id, serial, target_pkg, items_json,
                started_at, stopped_at, duration_s, host_run_id, status, run_dir
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session["run_id"],
                session.get("solo_session_id"),
                session.get("serial"),
                session.get("target"),
                json.dumps(session.get("items", []), ensure_ascii=False),
                session.get("started_at"),
                session.get("stopped_at"),
                session.get("duration_s"),
                session.get("host_run_id"),
                session.get("status"),
                session.get("run_dir"),
            ),
        )
        self._conn.commit()

    def fetch_sessions(self, target: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if target is not None:
            cursor = self._conn.execute(
                "SELECT * FROM sessions WHERE target_pkg = ? ORDER BY started_at DESC LIMIT ?",
                (target, limit),
            )
        else:
            cursor = self._conn.execute(
                "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?", (limit,)
            )
        columns = [desc[0] for desc in cursor.description]
        for row in cursor.fetchall():
            record = dict(zip(columns, row, strict=True))
            record["items"] = json.loads(record.pop("items_json") or "[]")
            rows.append(record)
        return rows

    def close(self) -> None:
        self._conn.close()
