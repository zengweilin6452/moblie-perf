"""SQLite 存储（WAL）：聚合指标、事件与 run 归档索引（设计方案 §5/§10）。

默认 <repo>/data/mobile_perf.db，WAL + busy_timeout=5s；原始 CSV 永不入库，
只入聚合行（min/median/mean/max/p90）。baselines 表本票建表留空（后续票填）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_BUSY_TIMEOUT_MS = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    run_id          TEXT PRIMARY KEY,
    solo_session_id TEXT,
    serial          TEXT,
    device_profile  TEXT,
    target_pkg      TEXT,
    target_version  TEXT,
    items_json      TEXT,
    started_at      TEXT,
    stopped_at      TEXT,
    duration_s      REAL,
    sample_count    INTEGER,
    host_run_id     TEXT,
    clock_offset_ms INTEGER,
    status          TEXT,
    run_dir         TEXT
);

CREATE TABLE IF NOT EXISTS metrics (
    run_id       TEXT NOT NULL REFERENCES sessions(run_id) ON DELETE CASCADE,
    metric_key   TEXT NOT NULL,
    unit         TEXT,
    source       TEXT,
    min          REAL,
    median       REAL,
    mean         REAL,
    max          REAL,
    p90          REAL,
    sample_count INTEGER,
    PRIMARY KEY (run_id, metric_key)
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES sessions(run_id) ON DELETE CASCADE,
    ts          TEXT,
    kind        TEXT NOT NULL,
    detail_json TEXT
);

CREATE TABLE IF NOT EXISTS baselines (
    name             TEXT NOT NULL,
    target_pkg       TEXT,
    device_profile   TEXT,
    metric_key       TEXT NOT NULL,
    value            REAL,
    source_run_id    TEXT,
    created_at       TEXT,
    PRIMARY KEY (name, metric_key)
);

CREATE TABLE IF NOT EXISTS runs_artifacts (
    run_id  TEXT NOT NULL REFERENCES sessions(run_id) ON DELETE CASCADE,
    dir_uri TEXT NOT NULL,
    PRIMARY KEY (run_id, dir_uri)
);
"""

_METRIC_FIELDS = ("min", "median", "mean", "max", "p90")


class PerfStore:
    """组件的 SQLite 存储入口；同进程会话串行（xdist 到 M4 验证）。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        self._conn.execute("PRAGMA journal_mode = wal")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def record_session(self, session: dict[str, Any]) -> None:
        if not session.get("run_id"):
            raise ValueError("session 记录必须包含 run_id")
        self._conn.execute(
            """
            INSERT OR REPLACE INTO sessions (
                run_id, solo_session_id, serial, device_profile, target_pkg,
                target_version, items_json, started_at, stopped_at, duration_s,
                sample_count, host_run_id, clock_offset_ms, status, run_dir
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session["run_id"],
                session.get("solo_session_id"),
                session.get("serial"),
                session.get("device_profile"),
                session.get("target"),
                session.get("target_version"),
                json.dumps(session.get("items", []), ensure_ascii=False),
                session.get("started_at"),
                session.get("stopped_at"),
                session.get("duration_s"),
                session.get("sample_count"),
                session.get("host_run_id"),
                session.get("clock_offset_ms"),
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

    def record_metrics(self, run_id: str, aggregates: list[dict[str, Any]]) -> None:
        """写入一个 run 的聚合行；同 run 重复写入时按 metric_key 整体替换。"""
        if not aggregates:
            return
        with self._conn:
            self._conn.execute("DELETE FROM metrics WHERE run_id = ?", (run_id,))
            self._conn.executemany(
                """
                INSERT INTO metrics (
                    run_id, metric_key, unit, source, min, median, mean, max,
                    p90, sample_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        agg["metric_key"],
                        agg.get("unit"),
                        agg.get("source"),
                        agg.get("min"),
                        agg.get("median"),
                        agg.get("mean"),
                        agg.get("max"),
                        agg.get("p90"),
                        agg.get("sample_count"),
                    )
                    for agg in aggregates
                ],
            )

    def fetch_metrics(self, run_id: str) -> list[dict[str, Any]]:
        cursor = self._conn.execute(
            "SELECT * FROM metrics WHERE run_id = ? ORDER BY metric_key", (run_id,)
        )
        return [dict(zip([d[0] for d in cursor.description], row, strict=True)) for row in cursor]

    def record_event(
        self,
        run_id: str,
        *,
        kind: str,
        detail: dict[str, Any],
        ts: str | None = None,
    ) -> None:
        if ts is None:
            ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        self._conn.execute(
            "INSERT INTO events (run_id, ts, kind, detail_json) VALUES (?, ?, ?, ?)",
            (run_id, ts, kind, json.dumps(detail, ensure_ascii=False)),
        )
        self._conn.commit()

    def fetch_events(self, run_id: str) -> list[dict[str, Any]]:
        cursor = self._conn.execute(
            "SELECT run_id, ts, kind, detail_json FROM events WHERE run_id = ? ORDER BY id",
            (run_id,),
        )
        events = []
        for run, ts, kind, detail_json in cursor:
            events.append(
                {
                    "run_id": run,
                    "ts": ts,
                    "kind": kind,
                    "detail": json.loads(detail_json or "{}"),
                }
            )
        return events

    def record_baseline(
        self,
        *,
        name: str,
        metric_key: str,
        value: float,
        target_pkg: str | None = None,
        device_profile: str | None = None,
        source_run_id: str | None = None,
        created_at: str | None = None,
    ) -> None:
        """写入/替换一条基线行（主键 name+metric_key）；created_at 缺省取当前时间。"""
        if not name or not metric_key:
            raise ValueError("baseline 必须包含 name 与 metric_key")
        if created_at is None:
            created_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        self._conn.execute(
            """
            INSERT OR REPLACE INTO baselines (
                name, target_pkg, device_profile, metric_key, value,
                source_run_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (name, target_pkg, device_profile, metric_key, value, source_run_id, created_at),
        )
        self._conn.commit()

    def fetch_baseline(self, name: str) -> list[dict[str, Any]]:
        cursor = self._conn.execute(
            "SELECT * FROM baselines WHERE name = ? ORDER BY metric_key", (name,)
        )
        return [dict(zip([d[0] for d in cursor.description], row, strict=True)) for row in cursor]

    def record_run_artifacts(self, run_id: str, dir_uris: list[str]) -> None:
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO runs_artifacts (run_id, dir_uri) VALUES (?, ?)",
                [(run_id, uri) for uri in dir_uris],
            )

    def fetch_run_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        cursor = self._conn.execute(
            "SELECT run_id, dir_uri FROM runs_artifacts WHERE run_id = ? ORDER BY rowid",
            (run_id,),
        )
        return [{"run_id": run, "dir_uri": uri} for run, uri in cursor]

    def run_ids(self) -> list[str]:
        return [row[0] for row in self._conn.execute("SELECT run_id FROM sessions")]

    def delete_run(self, run_id: str) -> None:
        """删除一个 run 的全部行（sessions 及级联的 metrics/events/runs_artifacts）。"""
        with self._conn:
            self._conn.execute("DELETE FROM sessions WHERE run_id = ?", (run_id,))

    def close(self) -> None:
        self._conn.close()
