"""PerfKit.run_collect 编排的单元测试：假后端注入，不碰 adb 与真机。"""

import json
from pathlib import Path

import pytest

from mobile_perf.kit import CollectError, PerfKit
from mobile_perf.store import PerfStore


class FakeHarnessBackend:
    """模拟 Harness 后端：start 返回固定 sessionId，stop 造一份产物 CSV。"""

    def __init__(self):
        self.started = []
        self.stopped = []

    def start(self, target, items):
        self.started.append((target, items))
        return "sid-fake-1"

    def stop(self, session_id, output_dir):
        self.stopped.append((session_id, output_dir))
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        csv_path = Path(output_dir) / "performance.csv"
        csv_path.write_text("时间,CPU\n0,1.0\n", encoding="utf-8")
        return {
            "success": True,
            "state": "stopped",
            "sessionId": session_id,
            "artifacts": {"performance": {"localDir": str(output_dir)}},
        }


def make_kit(tmp_path, backend, **kwargs):
    clocks = iter([0.0, 30.0])
    adb_path = fake_adb(tmp_path)
    kwargs.setdefault("adb_path", adb_path)
    kwargs.setdefault("serial", "749622c9")
    kwargs.setdefault("runs_dir", tmp_path / "runs")
    kwargs.setdefault("backend", backend)
    kwargs.setdefault("clock", lambda: next(clocks))
    kwargs.setdefault("sleep", lambda seconds: None)
    kwargs.setdefault("store", PerfStore(tmp_path / "data" / "mobile_perf.db"))
    return PerfKit(**kwargs)


def fake_adb(tmp_path):
    adb = tmp_path / "adb.exe"
    adb.write_bytes(b"x")
    return str(adb)


def test_run_collect_end_to_end_lands_artifacts_meta_report(tmp_path):
    backend = FakeHarnessBackend()
    kit = make_kit(tmp_path, backend)

    report = kit.run_collect(
        target="tv.danmaku.bili",
        items=["CPU", "Memory"],
        duration_s=30.0,
        host_run_id="run-manual-1",
        runs_dir=tmp_path / "runs",
    )

    run_dir = tmp_path / "runs" / "run-manual-1"
    assert run_dir.is_dir()
    assert backend.started == [("tv.danmaku.bili", ["CPU", "Memory"])]
    assert backend.stopped == [("sid-fake-1", str(run_dir / "artifacts"))]
    assert (run_dir / "artifacts" / "performance.csv").stat().st_size > 0

    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["runId"] == "run-manual-1"
    assert meta["target"] == "tv.danmaku.bili"
    assert meta["durationS"] == 30.0
    assert meta["soloSessionId"] == "sid-fake-1"
    assert meta["status"] == "stopped"

    assert report["runId"] == "run-manual-1"
    assert report["artifacts"] == [str(run_dir / "artifacts" / "performance.csv")]
    assert json.loads((run_dir / "report.json").read_text(encoding="utf-8")) == report


def test_run_collect_generates_run_id_when_host_run_id_missing(tmp_path):
    backend = FakeHarnessBackend()
    kit = make_kit(tmp_path, backend)

    report = kit.run_collect(
        target="tv.danmaku.bili",
        items=["CPU"],
        duration_s=30.0,
        runs_dir=tmp_path / "runs",
    )

    assert report["runId"].startswith("run-")
    assert (tmp_path / "runs" / report["runId"]).is_dir()


def test_run_collect_records_session_into_store(tmp_path):
    backend = FakeHarnessBackend()
    store = PerfStore(tmp_path / "data" / "mobile_perf.db")
    kit = make_kit(tmp_path, backend, store=store)

    kit.run_collect(
        target="tv.danmaku.bili",
        items=["CPU"],
        duration_s=30.0,
        host_run_id="run-store-1",
        runs_dir=tmp_path / "runs",
    )

    rows = store.fetch_sessions(target="tv.danmaku.bili")
    assert [r["run_id"] for r in rows] == ["run-store-1"]
    assert rows[0]["solo_session_id"] == "sid-fake-1"
    assert rows[0]["duration_s"] == 30.0


def test_run_collect_fails_fast_when_start_fails(tmp_path):
    class FailingBackend(FakeHarnessBackend):
        def start(self, target, items):
            raise CollectError("perf-start 失败: A performance recording is already active")

    kit = make_kit(tmp_path, FailingBackend())

    with pytest.raises(CollectError, match="already active"):
        kit.run_collect(
            target="tv.danmaku.bili",
            items=["CPU"],
            duration_s=30.0,
            runs_dir=tmp_path / "runs",
        )


def test_run_collect_fails_when_pull_artifacts_missing(tmp_path):
    class EmptyBackend(FakeHarnessBackend):
        def stop(self, session_id, output_dir):
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            return {"success": True, "state": "stopped", "sessionId": session_id}

    kit = make_kit(tmp_path, EmptyBackend())

    with pytest.raises(Exception, match="CSV"):
        kit.run_collect(
            target="tv.danmaku.bili",
            items=["CPU"],
            duration_s=30.0,
            host_run_id="run-empty",
            runs_dir=tmp_path / "runs",
        )


def test_kit_requires_serial_for_collect(tmp_path):
    with pytest.raises(ValueError, match="serial"):
        PerfKit(adb_path=fake_adb(tmp_path), serial=None)


def test_kit_resolves_adb_by_three_level_rule(tmp_path):
    kit = PerfKit(adb_path=fake_adb(tmp_path), serial="749622c9")

    assert kit.adb_path == Path(fake_adb(tmp_path))
