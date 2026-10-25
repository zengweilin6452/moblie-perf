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
