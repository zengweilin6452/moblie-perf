"""SQLite 存储（WAL）：设备状态、聚合指标、事件、基线与 run 归档索引（设计方案 §5/§10）。

默认 <repo>/data/mobile_perf.db，WAL + busy_timeout=5s（并发首连切换重试见
_enable_wal）；原始 CSV 永不入库，只入聚合行（min/median/mean/max/p90）。
metrics.source 语义（指标字典为准）：solo_pi=端侧 CSV 采集、
pc_supplement=PC 侧 dumpsys 补采、pc_orchestration=PC 编排计得（ticket 12
启动时间，单样本聚合）。
表结构：devices（设备状态，巡检后如实 upsert）、sessions、metrics、events、
baselines（宿主经 kit.save_baseline 写入）、runs_artifacts。
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_BUSY_TIMEOUT_MS = 5000
# journal_mode 切换需要短暂排他锁且不吃 busy_timeout（xdist 多进程同启实测）；
# 超时前轮询重试，超时后确认模式已是 wal 亦可接受
_WAL_SWITCH_TIMEOUT_S = 5.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    run_id          TEXT PRIMARY KEY,
    solo_session_id TEXT,
    serial          TEXT,
    device_profile  TEXT,
    net_profile     TEXT,
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

CREATE TABLE IF NOT EXISTS devices (
    serial         TEXT PRIMARY KEY,
    model          TEXT,
    os_version     TEXT,
    device_profile TEXT,
    status         TEXT,
    health_json    TEXT,
    updated_at     TEXT
);
"""

_METRIC_FIELDS = ("min", "median", "mean", "max", "p90")


class PerfStore:
    """组件的 SQLite 存储入口；WAL + busy_timeout，xdist 并发已在工单 11 验证。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        self._enable_wal()
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._migrate_schema()
        self._conn.commit()

    def _migrate_schema(self) -> None:
        """旧库加列（工单 20）：sessions.net_profile 在首批库上不存在，补上。

        CREATE TABLE IF NOT EXISTS 不会改已有表，只能按 PRAGMA 逐列补。
        """
        columns = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "net_profile" not in columns:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN net_profile TEXT")

    def _enable_wal(self) -> None:
        """切 WAL：并发首连时该 PRAGMA 需要短暂排他锁且不吃 busy_timeout，
        轮询重试至超时；超时后若模式已是 wal（他人切换完成）同样视为成功。"""
        deadline = time.monotonic() + _WAL_SWITCH_TIMEOUT_S
        while True:
            try:
                self._conn.execute("PRAGMA journal_mode = wal")
                return
            except sqlite3.OperationalError:
                if time.monotonic() < deadline:
                    time.sleep(0.05)
                    continue
                mode = self._conn.execute("PRAGMA journal_mode").fetchone()[0]
                if str(mode).lower() == "wal":
                    return
                raise

    def record_session(self, session: dict[str, Any]) -> None:
        if not session.get("run_id"):
            raise ValueError("session 记录必须包含 run_id")
        self._conn.execute(
            """
            INSERT OR REPLACE INTO sessions (
                run_id, solo_session_id, serial, device_profile, net_profile,
                target_pkg, target_version, items_json, started_at, stopped_at,
                duration_s, sample_count, host_run_id, clock_offset_ms, status,
                run_dir
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session["run_id"],
                session.get("solo_session_id"),
                session.get("serial"),
                session.get("device_profile"),
                # 网络档位（工单 20）：档位名+整形参数整体入 JSON，与报告一致
                json.dumps(session.get("net_profile"), ensure_ascii=False)
                if session.get("net_profile") is not None
                else None,
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
            # 网络档位以 JSON 存储（档位名+参数），读出即还原为 dict
            record["net_profile"] = (
                json.loads(record["net_profile"])
                if record.get("net_profile")
                else None
            )
            rows.append(record)
        return rows

    def fetch_session(self, run_id: str) -> dict[str, Any] | None:
        """按 run_id 取单条 session（无则 None）；列语义与 fetch_sessions 一致。"""
        cursor = self._conn.execute("SELECT * FROM sessions WHERE run_id = ?", (run_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        record = dict(zip([d[0] for d in cursor.description], row, strict=True))
        record["items"] = json.loads(record.pop("items_json") or "[]")
        record["net_profile"] = (
            json.loads(record["net_profile"]) if record.get("net_profile") else None
        )
        return record

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

    def fetch_metric_series(
        self,
        target: str,
        metric_key: str,
        profile: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """同包（可选同档案）某指标的跨 run 序列，按 startedAt 升序（ticket 13）。

        每条为 sessions 与 metrics 的连接行：run_id/started_at/duration_s/
        device_profile/serial + 聚合值。无该指标聚合的 run 不出现。
        """
        sql = (
            "SELECT s.run_id, s.started_at, s.duration_s, s.device_profile, s.serial, "
            "m.min, m.median, m.mean, m.max, m.p90, m.sample_count "
            "FROM sessions s JOIN metrics m ON m.run_id = s.run_id "
            "WHERE s.target_pkg = ? AND m.metric_key = ?"
        )
        params: list[Any] = [target, metric_key]
        if profile is not None:
            sql += " AND s.device_profile = ?"
            params.append(profile)
        sql += " ORDER BY s.started_at ASC LIMIT ?"
        params.append(limit)
        cursor = self._conn.execute(sql, params)
        return [
            dict(zip([d[0] for d in cursor.description], row, strict=True))
            for row in cursor
        ]

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

    def upsert_device(
        self,
        serial: str,
        *,
        status: str,
        model: str | None = None,
        os_version: str | None = None,
        device_profile: str | None = None,
        health: dict[str, Any] | None = None,
        updated_at: str | None = None,
    ) -> None:
        """upsert 设备状态行（§5 devices 表）：未提供的列保留旧值，status 以最新为准。"""
        if not serial:
            raise ValueError("device 记录必须包含 serial")
        if updated_at is None:
            updated_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        cursor = self._conn.execute(
            "SELECT model, os_version, device_profile FROM devices WHERE serial = ?",
            (serial,),
        )
        row = cursor.fetchone()
        self._conn.execute(
            """
            INSERT OR REPLACE INTO devices (
                serial, model, os_version, device_profile, status, health_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                serial,
                model if model is not None else (row[0] if row else None),
                os_version if os_version is not None else (row[1] if row else None),
                device_profile if device_profile is not None else (row[2] if row else None),
                status,
                json.dumps(health or {}, ensure_ascii=False),
                updated_at,
            ),
        )
        self._conn.commit()

    def fetch_devices(self) -> list[dict[str, Any]]:
        cursor = self._conn.execute("SELECT * FROM devices ORDER BY serial")
        devices = []
        for row in cursor:
            record = dict(zip([d[0] for d in cursor.description], row, strict=True))
            record["health"] = json.loads(record.pop("health_json") or "{}")
            devices.append(record)
        return devices

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
