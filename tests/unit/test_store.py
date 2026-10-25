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
