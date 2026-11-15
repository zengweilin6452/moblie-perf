"""store 骨架的单元测试：WAL、busy_timeout、会话记录往返。"""

import sqlite3
from pathlib import Path

import pytest

from mobile_perf import store as store_module
from mobile_perf.store import PerfStore


@pytest.fixture()
def store(tmp_path):
    s = PerfStore(tmp_path / "data" / "mobile_perf.db")
    yield s
    s.close()


def test_open_creates_db_with_wal_and_busy_timeout(tmp_path):
    db_path = tmp_path / "nested" / "mobile_perf.db"
    store = PerfStore(db_path)

    assert db_path.is_file()
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    store.close()


def test_record_and_fetch_session_round_trip(store):
    store.record_session(
        {
            "run_id": "run-20261025-100000-ab12",
            "solo_session_id": "sid-1",
            "serial": "749622c9",
            "target": "tv.danmaku.bili",
            "items": ["CPU", "Memory"],
            "started_at": "2026-10-25T10:00:00",
            "stopped_at": "2026-10-25T10:01:00",
            "duration_s": 60.0,
            "status": "stopped",
            "run_dir": "runs/run-20261025-100000-ab12",
        }
    )

    rows = store.fetch_sessions(target="tv.danmaku.bili")

    assert len(rows) == 1
    assert rows[0]["run_id"] == "run-20261025-100000-ab12"
    assert rows[0]["items"] == ["CPU", "Memory"]
    assert rows[0]["duration_s"] == 60.0


def test_fetch_sessions_filters_by_target(store):
    store.record_session({"run_id": "r1", "target": "tv.danmaku.bili"})
    store.record_session({"run_id": "r2", "target": "com.other"})

    assert [r["run_id"] for r in store.fetch_sessions(target="com.other")] == ["r2"]


def test_record_session_requires_run_id(store):
    with pytest.raises(ValueError, match="run_id"):
        store.record_session({"target": "tv.danmaku.bili"})


def _record_aggregates(store, run_id="run-agg-1"):
    store.record_session({"run_id": run_id, "target": "tv.danmaku.bili"})
    store.record_metrics(
        run_id,
        [
            {
                "metric_key": "mem_pss_mb",
                "unit": "MB",
                "source": "solo_pi",
                "min": 100.0,
                "median": 150.0,
                "mean": 151.5,
                "max": 220.0,
                "p90": 200.0,
                "sample_count": 81,
            },
            {
                "metric_key": "cpu_process_pct",
                "unit": "%",
                "source": "solo_pi",
                "min": 0.0,
                "median": 4.0,
                "mean": 5.0,
                "max": 30.0,
                "p90": 12.0,
                "sample_count": 341,
            },
        ],
    )


def test_record_and_fetch_metrics_round_trip(store):
    _record_aggregates(store)

    rows = store.fetch_metrics("run-agg-1")
    by_key = {row["metric_key"]: row for row in rows}

    assert set(by_key) == {"mem_pss_mb", "cpu_process_pct"}
    assert by_key["mem_pss_mb"]["median"] == 150.0
    assert by_key["mem_pss_mb"]["unit"] == "MB"
    assert by_key["mem_pss_mb"]["sample_count"] == 81
    assert by_key["cpu_process_pct"]["p90"] == 12.0


def test_record_metrics_replaces_rows_for_same_run(store):
    _record_aggregates(store)
    store.record_metrics(
        "run-agg-1",
        [
            {
                "metric_key": "mem_pss_mb",
                "unit": "MB",
                "source": "solo_pi",
                "min": 1.0,
                "median": 2.0,
                "mean": 2.0,
                "max": 3.0,
                "p90": 2.9,
                "sample_count": 9,
            }
        ],
    )

    rows = store.fetch_metrics("run-agg-1")

    assert len(rows) == 1
    assert rows[0]["median"] == 2.0


def test_record_and_fetch_events_round_trip(store):
    _record_aggregates(store)
    store.record_event(
        "run-agg-1",
        kind="integrity_warning",
        detail={"reason": "row_count_mismatch", "file": "PSS-main-1.csv"},
        ts="2026-10-25T10:01:00+08:00",
    )
    store.record_event("run-agg-1", kind="integrity_warning", detail={"reason": "device_rm_failed"})

    events = store.fetch_events("run-agg-1")

    assert [e["kind"] for e in events] == ["integrity_warning", "integrity_warning"]
    assert events[0]["detail"]["reason"] == "row_count_mismatch"
    assert events[0]["ts"] == "2026-10-25T10:01:00+08:00"
    assert events[1]["ts"]  # 未传 ts 时自动补当前时间


def test_record_run_artifacts_round_trip(store):
    _record_aggregates(store)
    store.record_run_artifacts("run-agg-1", ["runs/run-agg-1/artifacts", "runs/run-agg-1"])

    rows = store.fetch_run_artifacts("run-agg-1")

    assert rows == [
        {"run_id": "run-agg-1", "dir_uri": "runs/run-agg-1/artifacts"},
        {"run_id": "run-agg-1", "dir_uri": "runs/run-agg-1"},
    ]


def test_schema_creates_all_design_v05_tables(store):
    tables = {
        row[0]
        for row in store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }

    # §5 全部数据表（thresholds 不入库）
    assert {"sessions", "metrics", "events", "baselines", "runs_artifacts"} <= tables


def test_delete_run_removes_all_rows_of_the_run(store):
    _record_aggregates(store)
    store.record_event("run-agg-1", kind="integrity_warning", detail={})
    store.record_run_artifacts("run-agg-1", ["runs/run-agg-1/artifacts"])

    store.delete_run("run-agg-1")

    assert store.fetch_sessions() == []
    assert store.fetch_metrics("run-agg-1") == []
    assert store.fetch_events("run-agg-1") == []
    assert store.fetch_run_artifacts("run-agg-1") == []


def test_delete_run_keeps_other_runs_rows(store):
    store.record_session({"run_id": "run-keep", "target": "tv.danmaku.bili"})
    store.record_metrics(
        "run-keep",
        [
            {
                "metric_key": "mem_pss_mb",
                "unit": "MB",
                "source": "solo_pi",
                "min": 1.0,
                "median": 2.0,
                "mean": 2.0,
                "max": 3.0,
                "p90": 2.9,
                "sample_count": 9,
            }
        ],
    )
    store.record_session({"run_id": "run-drop", "target": "com.other"})

    store.delete_run("run-drop")

    assert [r["run_id"] for r in store.fetch_sessions()] == ["run-keep"]
    assert [r["metric_key"] for r in store.fetch_metrics("run-keep")] == ["mem_pss_mb"]


def test_baseline_record_and_fetch_round_trip(store):
    store.record_baseline(
        name="bili-home-v3",
        target_pkg="tv.danmaku.bili",
        device_profile="PICO4-Android12",
        metric_key="mem_pss_mb",
        value=180.5,
        source_run_id="run-20261025-100000-ab12",
        created_at="2026-10-25T10:00:00+08:00",
    )
    store.record_baseline(
        name="bili-home-v3",
        target_pkg="tv.danmaku.bili",
        device_profile="PICO4-Android12",
        metric_key="cpu_app_pct",
        value=12.0,
        source_run_id="run-20261025-100000-ab12",
        created_at="2026-10-25T10:00:00+08:00",
    )

    rows = store.fetch_baseline("bili-home-v3")

    assert {row["metric_key"] for row in rows} == {"mem_pss_mb", "cpu_app_pct"}
    mem = next(row for row in rows if row["metric_key"] == "mem_pss_mb")
    assert mem["name"] == "bili-home-v3"
    assert mem["target_pkg"] == "tv.danmaku.bili"
    assert mem["device_profile"] == "PICO4-Android12"
    assert mem["value"] == 180.5
    assert mem["source_run_id"] == "run-20261025-100000-ab12"
    assert mem["created_at"] == "2026-10-25T10:00:00+08:00"


def test_baseline_fetch_unknown_name_returns_empty(store):
    assert store.fetch_baseline("no-such-baseline") == []


def test_baseline_upsert_same_name_metric_replaces_value(store):
    store.record_baseline(
        name="bili-home-v3",
        metric_key="mem_pss_mb",
        value=180.0,
        source_run_id="run-old",
    )
    store.record_baseline(
        name="bili-home-v3",
        metric_key="mem_pss_mb",
        value=200.0,
        source_run_id="run-new",
    )

    rows = store.fetch_baseline("bili-home-v3")

    assert len(rows) == 1
    assert rows[0]["value"] == 200.0
    assert rows[0]["source_run_id"] == "run-new"


def test_baseline_created_at_defaults_to_local_iso_now(store):
    store.record_baseline(name="bili-home-v3", metric_key="mem_pss_mb", value=1.0)

    (row,) = store.fetch_baseline("bili-home-v3")

    assert row["created_at"]  # 写入时自动补时间戳


def test_concurrent_store_construction_on_fresh_db_is_safe(tmp_path):
    """xdist 场景：多进程/线程同时首连同一新库，journal_mode=wal 切换竞争不炸。"""
    import threading

    db_path = tmp_path / "shared" / "mobile_perf.db"
    barrier = threading.Barrier(8)
    errors: list[Exception] = []
    stores: list[PerfStore] = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            s = PerfStore(db_path)
            s.close()  # 连接归创建线程所有，本线程内关闭
            with lock:
                stores.append(s)
        except Exception as exc:  # noqa: BLE001 - 收集后统一断言
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(stores) == 8
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


# ---- devices 表（设计方案 §5）：设备状态如实入库 ----

def test_device_upsert_and_fetch_round_trip(store):
    store.upsert_device(
        "749622c9",
        status="online",
        model="PICO 4",
        os_version="12",
        device_profile="PICO4-Android12",
        health={"ok": True, "degradedItems": []},
    )

    (row,) = store.fetch_devices()

    assert row["serial"] == "749622c9"
    assert row["status"] == "online"
    assert row["model"] == "PICO 4"
    assert row["os_version"] == "12"
    assert row["device_profile"] == "PICO4-Android12"
    assert row["health"] == {"ok": True, "degradedItems": []}
    assert row["updated_at"]


def test_device_upsert_merges_partial_fields_and_updates_status(store):
    """未提供的列保留旧值，status 以最新巡检结论为准（needs_attention 如实入库）。"""
    store.upsert_device("749622c9", status="online", model="PICO 4")

    store.upsert_device("749622c9", status="needs_attention", health={"bound": False})

    (row,) = store.fetch_devices()
    assert row["status"] == "needs_attention"
    assert row["model"] == "PICO 4"
    assert row["health"] == {"bound": False}


def test_device_upsert_requires_serial(store):
    with pytest.raises(ValueError, match="serial"):
        store.upsert_device("", status="online")


def test_ensure_ready_upserts_device_status(tmp_path):
    """编排层巡检后设备状态入库：健康 → online；needs_attention 如实入库。"""
    from tests.unit.test_kit import FakeHarnessBackend, make_kit

    kit = make_kit(tmp_path, FakeHarnessBackend())
    kit.ensure_ready()
    (row,) = kit.store.fetch_devices()
    assert row["serial"] == "749622c9"
    assert row["status"] == "online"

    # 无障碍可解析但未绑定我们的服务 → needs_attention 如实入库
    needs_attention_call = make_needs_attention_device_call()
    kit2 = make_kit(tmp_path, FakeHarnessBackend(), device_call=needs_attention_call)
    report = kit2.ensure_ready()
    assert report.needs_attention is True
    (row2,) = kit2.store.fetch_devices()
    assert row2["status"] == "needs_attention"
    assert row2["health"]["needsAttention"] is True


def make_needs_attention_device_call():
    """巡检替身：无障碍 dump 可解析但 Bound 段为空（假开启形态）。"""
    from types import SimpleNamespace

    accessibility_dump = (
        "ACCESSIBILITY MANAGER SERVICES (dumpsys accessibility)\n"
        "  Enabled services:\n"
        "    Service[com.other.app/com.other.app.OtherService]\n"
        "  Bound services:\n"
    )

    def call(args, timeout_s=15.0):
        if args == ["devices"]:
            stdout = "List of devices attached\n749622c9\tdevice\n"
        elif "accessibility" in args:
            stdout = accessibility_dump
        elif "battery" in args:
            stdout = "AC powered: false\nUSB powered: false\nstatus: 4\n"
        elif "activity" in args:
            stdout = "  mResumedActivity: ActivityRecord{1 u0 tv.danmaku.bili/.MainActivity t42}\n"
        elif "date" in args:
            stdout = "1792895595"
        else:
            stdout = ""
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    return call


def test_fetch_metric_series_joins_sessions_in_ascending_time(store):
    """泄漏趋势查询：同包同档案的 run 聚合随 startedAt 升序返回（ticket 13）。"""
    for run_id, started_at, profile, median in [
        ("r2", "2026-10-22T10:00:00+08:00", "Xiaomi-2206123SC-Android14", 120.0),
        ("r0", "2026-10-20T10:00:00+08:00", "Xiaomi-2206123SC-Android14", 100.0),
        ("r1", "2026-10-21T10:00:00+08:00", "Xiaomi-2206123SC-Android14", 110.0),
    ]:
        store.record_session(
            {
                "run_id": run_id,
                "target": "com.sinosoft.trainapp",
                "device_profile": profile,
                "started_at": started_at,
                "duration_s": 47.0,
            }
        )
        store.record_metrics(
            run_id,
            [{"metric_key": "mem_pss_mb", "unit": "MB", "median": median, "sample_count": 46}],
        )

    series = store.fetch_metric_series("com.sinosoft.trainapp", "mem_pss_mb")

    assert [row["run_id"] for row in series] == ["r0", "r1", "r2"]
    assert [row["median"] for row in series] == [100.0, 110.0, 120.0]
    assert series[0]["device_profile"] == "Xiaomi-2206123SC-Android14"
    assert series[0]["duration_s"] == 47.0


def test_fetch_metric_series_filters_by_profile(store):
    for run_id, profile in [("r0", "A"), ("r1", "B")]:
        store.record_session(
            {
                "run_id": run_id,
                "target": "com.sinosoft.trainapp",
                "device_profile": profile,
                "started_at": f"2026-10-2{run_id[-1]}T10:00:00+08:00",
            }
        )
        store.record_metrics(
            run_id, [{"metric_key": "mem_pss_mb", "unit": "MB", "median": 100.0}]
        )

    assert [row["run_id"] for row in store.fetch_metric_series(
        "com.sinosoft.trainapp", "mem_pss_mb", profile="B"
    )] == ["r1"]


def test_fetch_metric_series_skips_runs_without_that_metric(store):
    store.record_session({"run_id": "r0", "target": "com.sinosoft.trainapp"})
    store.record_metrics(
        "r0", [{"metric_key": "cpu_process_pct", "unit": "%", "median": 5.0}]
    )

    assert store.fetch_metric_series("com.sinosoft.trainapp", "mem_pss_mb") == []


# ---- ticket 20：sessions.net_profile 列与旧库迁移 ----


def test_session_net_profile_round_trip(store):
    """档位名+参数整体入 JSON、读出还原为 dict；没跑弱网的是 None 不是空 dict。"""
    store.record_session(
        {
            "run_id": "r-weak",
            "target": "tv.danmaku.bili",
            "net_profile": {"name": "weak", "latencyMs": 400.0, "downKbps": 400.0},
        }
    )
    store.record_session({"run_id": "r-plain", "target": "tv.danmaku.bili"})

    rows = {row["run_id"]: row for row in store.fetch_sessions(target="tv.danmaku.bili")}

    assert rows["r-weak"]["net_profile"] == {
        "name": "weak",
        "latencyMs": 400.0,
        "downKbps": 400.0,
    }
    assert rows["r-plain"]["net_profile"] is None


def test_fetch_session_by_run_id(store):
    store.record_session({"run_id": "r-x", "target": "tv.danmaku.bili"})

    assert store.fetch_session("r-x")["target_pkg"] == "tv.danmaku.bili"
    assert store.fetch_session("r-missing") is None


def test_old_db_without_net_profile_column_is_migrated(tmp_path):
    """建库早于工单 20 的库没有 net_profile 列：打开时自动补列，不要求重建。"""
    db_path = tmp_path / "data" / "mobile_perf.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE sessions (
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
            )
            """
        )
        conn.execute(
            "INSERT INTO sessions (run_id, target_pkg) VALUES ('r-old', 'tv.danmaku.bili')"
        )

    store = PerfStore(db_path)
    try:
        # 旧记录照常可读（net_profile 为 NULL），新写入可带档位
        assert store.fetch_session("r-old")["net_profile"] is None
        store.record_session(
            {
                "run_id": "r-new",
                "target": "tv.danmaku.bili",
                "net_profile": {"name": "weak"},
            }
        )
        assert store.fetch_session("r-new")["net_profile"] == {"name": "weak"}
    finally:
        store.close()


# ---- P1-05：REPLACE 级联、备份恢复、迁移版本与异常退出自检 ----

def test_rerecord_session_keeps_metrics_and_events(store):
    """同 run_id 重写 session 不得级联清空 metrics/events。

    修复前 INSERT OR REPLACE 先删冲突行，ON DELETE CASCADE 静默清空子表
    （实测 1 metrics + 1 events → 0 + 0）；UPSERT 只更新列，子表保留。
    """
    store.record_session({"run_id": "r1", "target": "tv.danmaku.bili", "status": "running"})
    store.record_metrics("r1", [{"metric_key": "cpu_process_pct", "median": 12.5}])
    store.record_event("r1", kind="mark", detail={"label": "x"})

    store.record_session({"run_id": "r1", "target": "tv.danmaku.bili", "status": "stopped"})

    assert len(store.fetch_metrics("r1")) == 1
    assert len(store.fetch_events("r1")) == 1
    assert store.fetch_session("r1")["status"] == "stopped"


def test_backup_to_restores_every_table_independently(tmp_path, store):
    """Backup API 快照含 WAL 中已提交数据，四张表逐一对得上（P1-05）。"""
    store.record_session({"run_id": "r1", "target": "tv.danmaku.bili"})
    store.record_metrics("r1", [{"metric_key": "fps_frame", "median": 55.0}])
    store.record_event("r1", kind="mark", detail={"label": "mid"})
    store.record_baseline(name="b1", metric_key="fps_frame", value=55.0)
    store.record_run_artifacts("r1", ["runs/r1/artifacts"])
    # 关闭 autocheckpoint：已提交数据只留在 -wal，主 DB 文件保持陈旧
    store._conn.execute("PRAGMA wal_autocheckpoint = 0")

    backup = store.backup_to(tmp_path / "backup.db")

    fresh = PerfStore(backup)
    assert fresh.fetch_session("r1")["run_id"] == "r1"
    assert [m["metric_key"] for m in fresh.fetch_metrics("r1")] == ["fps_frame"]
    assert [e["kind"] for e in fresh.fetch_events("r1")] == ["mark"]
    assert fresh.fetch_baseline("b1")[0]["value"] == 55.0
    assert fresh.fetch_run_artifacts("r1")[0]["dir_uri"] == "runs/r1/artifacts"
    assert fresh.integrity_check() == "ok"

    # 对照：只复制主 DB 文件在未 checkpoint 时拿不到数据——这就是必须用
    # Backup API 的原因（不做强断言，checkpoint 时机随实现而定）
    stale_copy = tmp_path / "file-copy.db"
    stale_copy.write_bytes(store.db_path.read_bytes())
    with sqlite3.connect(stale_copy) as raw:
        tables = {
            row[0]
            for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "sessions" in tables:
            assert raw.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_backup_overwrites_existing_target(tmp_path, store):
    """对已存在的目标文件重复备份：结果为最新快照，不残留旧数据。"""
    store.record_session({"run_id": "r-old", "target": "tv.danmaku.bili"})
    target = store.backup_to(tmp_path / "backup.db")
    store.delete_run("r-old")
    store.record_session({"run_id": "r-new", "target": "tv.danmaku.bili"})

    store.backup_to(target)

    fresh = PerfStore(target)
    assert fresh.run_ids() == ["r-new"]


_LEGACY_SCHEMA = """
CREATE TABLE sessions (
    run_id TEXT PRIMARY KEY, solo_session_id TEXT, serial TEXT,
    device_profile TEXT, target_pkg TEXT, target_version TEXT,
    items_json TEXT, started_at TEXT, stopped_at TEXT, duration_s REAL,
    sample_count INTEGER, host_run_id TEXT, clock_offset_ms INTEGER,
    status TEXT, run_dir TEXT
);
CREATE TABLE metrics (
    run_id TEXT NOT NULL REFERENCES sessions(run_id) ON DELETE CASCADE,
    metric_key TEXT NOT NULL, unit TEXT, source TEXT, min REAL, median REAL,
    mean REAL, max REAL, p90 REAL, sample_count INTEGER,
    PRIMARY KEY (run_id, metric_key)
);
"""


def test_legacy_db_migrates_to_current_version_and_keeps_data(tmp_path):
    """v0 旧库（缺 net_profile）重开即迁移：数据保留、版本推进（P1-05）。"""
    db = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(_LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO sessions (run_id, target_pkg) VALUES ('legacy-1', 'tv.danmaku.bili')"
    )
    conn.execute(
        "INSERT INTO metrics (run_id, metric_key, median) VALUES ('legacy-1', 'fps_frame', 55.0)"
    )
    conn.commit()
    conn.close()
    assert conn is not None

    store = PerfStore(db)  # 首次重开：执行 v0 → v1 迁移
    try:
        assert store.integrity_check() == "ok"
        assert int(
            store._conn.execute("PRAGMA user_version").fetchone()[0]
        ) == store_module._SCHEMA_USER_VERSION
        # 旧数据原样保留
        assert store.fetch_session("legacy-1")["target_pkg"] == "tv.danmaku.bili"
        assert store.fetch_metrics("legacy-1")[0]["metric_key"] == "fps_frame"
        # 新列可用：迁移后能写入 net_profile 并读回
        store.record_session({"run_id": "legacy-1", "target": "tv.danmaku.bili",
                              "net_profile": {"name": "3g"}})
        assert store.fetch_session("legacy-1")["net_profile"] == {"name": "3g"}
    finally:
        store.close()

    again = PerfStore(db)  # 重复打开：迁移幂等，不重复加列不报错
    try:
        assert int(
            again._conn.execute("PRAGMA user_version").fetchone()[0]
        ) == store_module._SCHEMA_USER_VERSION
        assert again.fetch_session("legacy-1")["net_profile"] == {"name": "3g"}
    finally:
        again.close()


def test_integrity_check_after_abnormal_exit(tmp_path):
    """子进程提交后 os._exit 硬退出：重开自检 ok、已提交数据完好（P1-05）。"""
    import subprocess
    import sys as _sys

    from mobile_perf import store as store_module

    src_root = str(Path(store_module.__file__).resolve().parent.parent)
    db = tmp_path / "crash.sqlite"
    script = (
        "import os, sys;"
        f"sys.path.insert(0, {src_root!r});"
        "from mobile_perf.store import PerfStore;"
        f"s = PerfStore({str(db)!r});"
        "s.record_session({'run_id': 'crash-1', 'target': 'app'});"
        "s.record_event('crash-1', kind='mark', detail={'label': 'kept'});"
        "os._exit(1)"  # 模拟强杀：不 close、不 checkpoint，WAL 原样留存
    )
    proc = subprocess.run([_sys.executable, "-c", script], timeout=60)
    assert proc.returncode == 1

    store = PerfStore(db)
    try:
        assert store.integrity_check() == "ok"
        assert store.fetch_session("crash-1")["run_id"] == "crash-1"
        assert [e["kind"] for e in store.fetch_events("crash-1")] == ["mark"]
    finally:
        store.close()
