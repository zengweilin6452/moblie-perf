"""store 骨架的单元测试：WAL、busy_timeout、会话记录往返。"""

import sqlite3

import pytest

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
