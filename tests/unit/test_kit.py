"""PerfKit.run_collect 编排的单元测试：假后端注入，不碰 adb 与真机。"""

import json
from pathlib import Path

import pytest

from mobile_perf import kit as kit_module
from mobile_perf.gate import FileLockGate, GateBusyError
from mobile_perf.kit import CollectError, PerfKit, TrendError
from mobile_perf.store import PerfStore


class FakeHarnessBackend:
    """模拟 Harness 后端：start 返回固定 sessionId，stop 造一份 SoloPi 格式 CSV。"""

    def __init__(self):
        self.started = []
        self.stopped = []

    def start(self, target, items):
        self.started.append((target, items))
        return "sid-fake-1"

    def stop(self, session_id, output_dir):
        self.stopped.append((session_id, output_dir))
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        rows = ["RecordTime,PSS-main-18390(MB),extra,SimpleTime"]
        rows += [f"{1792895595000 + i},{42.0 + i},,{i}" for i in range(3)]
        Path(output_dir).joinpath("performance.csv").write_text(
            "\n".join(rows) + "\n", encoding="gb18030"
        )
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
    kwargs.setdefault("gate", FileLockGate(tmp_path / "gate-leases"))
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



class SoloPiCsvBackend(FakeHarnessBackend):
    """stop 产出真实格式的 GBK CSV，并声称设备端目录 outputPath。"""

    REMOTE = "/sdcard/Android/data/tv.danmaku.bili/files/records/performance-abc123"

    def stop(self, session_id, output_dir):
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        rows = ["RecordTime,PSS-main-18390(MB),extra,SimpleTime"]
        rows += [f"{1792895595000 + i},{value},,{i}" for i, value in enumerate([10, 20, 30])]
        (Path(output_dir) / "PSS-main-18390_Memory_x.csv").write_text(
            "\n".join(rows) + "\n", encoding="gb18030"
        )
        return {
            "success": True,
            "state": "stopped",
            "sessionId": session_id,
            "outputPath": self.REMOTE,
        }


class FakeDeviceAdb:
    """模拟 adb：按文件名返回行数（默认与本地一致），记录 rm 命令。"""

    def __init__(self, overrides=None):
        self.overrides = overrides or {}
        self.removed = []

    def __call__(self, cmd, timeout_s):
        class Result:
            def __init__(self, stdout, returncode):
                self.stdout = stdout
                self.returncode = returncode

        if "ls" in cmd:
            return Result("PSS-main-18390_Memory_x.csv\n", 0)
        if "wc" in cmd:
            lines = 4 + self.overrides.get("delta", 0)  # 表头 1 行 + 数据 3 行
            remote_base = SoloPiCsvBackend.REMOTE
            return Result(f"{lines} {remote_base}/PSS-main-18390_Memory_x.csv\n", 0)
        if "rm" in cmd:
            self.removed.append(cmd[-1])
            return Result("", 0)
        return Result("", 1)


def make_solopi_kit(tmp_path, backend, adb_runner=None, **kwargs):
    clocks = iter([0.0, 30.0])
    adb_path = fake_adb(tmp_path)
    kwargs.setdefault("adb_path", adb_path)
    kwargs.setdefault("serial", "749622c9")
    kwargs.setdefault("runs_dir", tmp_path / "runs")
    kwargs.setdefault("backend", backend)
    kwargs.setdefault("clock", lambda: next(clocks))
    kwargs.setdefault("sleep", lambda seconds: None)
    kwargs.setdefault("store", PerfStore(tmp_path / "data" / "mobile_perf.db"))
    if adb_runner is not None:
        kwargs.setdefault("adb_runner", adb_runner)
    return PerfKit(**kwargs)


def test_run_collect_aggregates_metrics_into_store_and_report(tmp_path):
    backend = SoloPiCsvBackend()
    adb = FakeDeviceAdb()
    store = PerfStore(tmp_path / "data" / "mobile_perf.db")
    kit = make_solopi_kit(tmp_path, backend, adb_runner=adb, store=store)

    report = kit.run_collect(
        target="tv.danmaku.bili",
        items=["Memory"],
        duration_s=30.0,
        host_run_id="run-20261025-100000-abcd",
        runs_dir=tmp_path / "runs",
        supplement=False,  # 本测试只关注 integrity 事件，补采单独覆盖
    )

    rows = store.fetch_metrics("run-20261025-100000-abcd")
    assert len(rows) == 1
    assert rows[0]["metric_key"] == "mem_pss_mb"
    assert rows[0]["min"] == 10.0
    assert rows[0]["median"] == 20.0
    assert rows[0]["max"] == 30.0
    assert rows[0]["sample_count"] == 3

    assert report["metricsSummary"]["mem_pss_mb"]["median"] == 20.0
    assert report["metricsSummary"]["mem_pss_mb"]["sampleCount"] == 3
    assert report["integrity"]["deviceRowCheck"] == "passed"
    assert report["integrity"]["deviceDirRemoved"] is True

    assert adb.removed == [backend.REMOTE]  # 校验通过才删设备端
    artifacts = store.fetch_run_artifacts("run-20261025-100000-abcd")
    assert artifacts  # 产物目录入 runs_artifacts


def test_run_collect_keeps_device_dir_and_alarms_on_row_mismatch(tmp_path):
    backend = SoloPiCsvBackend()
    adb = FakeDeviceAdb(overrides={"delta": 5})  # 设备端比本地多 5 行
    store = PerfStore(tmp_path / "data" / "mobile_perf.db")
    kit = make_solopi_kit(tmp_path, backend, adb_runner=adb, store=store)

    report = kit.run_collect(
        target="tv.danmaku.bili",
        items=["Memory"],
        duration_s=30.0,
        host_run_id="run-20261025-100000-abcd",
        runs_dir=tmp_path / "runs",
        supplement=False,  # 本测试只关注 integrity 事件，补采单独覆盖
    )

    assert adb.removed == []  # 行数不一致保留设备端
    assert report["integrity"]["deviceRowCheck"] == "failed"
    assert report["integrity"]["deviceDirRemoved"] is False

    events = store.fetch_events("run-20261025-100000-abcd")
    assert [e["kind"] for e in events] == ["integrity_warning"]
    assert events[0]["detail"]["reason"] == "row_count_mismatch"
    assert events[0]["detail"]["deviceDirKept"] is True

    # 本地已拉到的数据仍聚合入库
    assert store.fetch_metrics("run-20261025-100000-abcd")


def test_run_collect_skips_device_probe_without_remote_path(tmp_path):
    backend = FakeHarnessBackend()  # 旧载荷：无 outputPath
    store = PerfStore(tmp_path / "data" / "mobile_perf.db")
    kit = make_solopi_kit(tmp_path, backend, store=store)

    report = kit.run_collect(
        target="tv.danmaku.bili",
        items=["CPU"],
        duration_s=30.0,
        host_run_id="run-no-remote",
        runs_dir=tmp_path / "runs",
        supplement=False,  # 本测试只关注 integrity 事件，补采单独覆盖
    )

    assert report["integrity"]["deviceRowCheck"] == "skipped"
    assert store.fetch_events("run-no-remote") == []


def test_run_collect_enforces_local_retention(tmp_path):
    backend = FakeHarnessBackend()
    store = PerfStore(tmp_path / "data" / "mobile_perf.db")
    kit = make_solopi_kit(tmp_path, backend, store=store, keep_runs=1)

    stale = tmp_path / "runs" / "run-20260101-000000-ffff"
    stale.mkdir(parents=True)
    store.record_session({"run_id": "run-20260101-000000-ffff", "target": "com.old"})

    kit.run_collect(
        target="tv.danmaku.bili",
        items=["CPU"],
        duration_s=30.0,

        host_run_id="run-20261025-100000-abcd",
        runs_dir=tmp_path / "runs",
        supplement=False,  # 本测试只关注 integrity 事件，补采单独覆盖
    )

    assert not stale.exists()
    assert "run-20260101-000000-ffff" not in [r["run_id"] for r in store.fetch_sessions()]



def test_run_collect_writes_html_report_with_same_source_json(tmp_path):
    backend = SoloPiCsvBackend()
    adb = FakeDeviceAdb()
    kit = make_solopi_kit(tmp_path, backend, adb_runner=adb)

    report = kit.run_collect(
        target="tv.danmaku.bili",
        items=["Memory"],
        duration_s=30.0,
        host_run_id="run-html-1",
        runs_dir=tmp_path / "runs",
    )

    run_dir = tmp_path / "runs" / "run-html-1"
    html = (run_dir / "report.html").read_text(encoding="utf-8")
    on_disk = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    assert on_disk == report
    assert 'id="chart-mem_pss_mb"' in html
    assert "vendor:echarts BEGIN" in html  # 离线 ECharts 内嵌


def test_run_collect_records_device_profile_into_store_and_meta(tmp_path):
    backend = SoloPiCsvBackend()
    adb = FakeDeviceAdb()
    store = PerfStore(tmp_path / "data" / "mobile_perf.db")
    kit = make_solopi_kit(
        tmp_path, backend, adb_runner=adb, store=store, device_profile="PICO4-Android12"
    )

    kit.run_collect(
        target="tv.danmaku.bili",
        items=["Memory"],
        duration_s=30.0,
        host_run_id="run-profile-1",
        runs_dir=tmp_path / "runs",
    )

    rows = store.fetch_sessions(target="tv.danmaku.bili")
    assert rows[0]["device_profile"] == "PICO4-Android12"
    meta = json.loads(
        (tmp_path / "runs" / "run-profile-1" / "meta.json").read_text(encoding="utf-8")
    )
    assert meta["deviceProfile"] == "PICO4-Android12"
    assert json.loads(
        (tmp_path / "runs" / "run-profile-1" / "report.json").read_text(encoding="utf-8")
    )["deviceProfile"] == "PICO4-Android12"


def test_trend_returns_asc_runs_with_aggregates(tmp_path):
    store = PerfStore(tmp_path / "data" / "mobile_perf.db")
    for index, run_id in enumerate(["run-20261025-100000-aaaa", "run-20261025-110000-bbbb"]):
        store.record_session(
            {
                "run_id": run_id,
                "target": "tv.danmaku.bili",
                "device_profile": "PICO4-Android12",
                "serial": "749622c9",
                "started_at": f"2026-10-25T1{index}:00:00",
                "duration_s": 30.0,
            }
        )
        store.record_metrics(
            run_id,
            [
                {
                    "metric_key": "mem_pss_mb",
                    "unit": "MB",
                    "source": "solo_pi",
                    "min": 10.0 + index,
                    "median": 20.0 + index,
                    "mean": 21.0 + index,
                    "max": 30.0 + index,
                    "p90": 29.0 + index,
                    "sample_count": 3,
                }
            ],
        )
    kit = make_kit(tmp_path, FakeHarnessBackend(), store=store)

    trend = kit.trend("tv.danmaku.bili", "mem_pss_mb")

    assert trend["metric"] == "mem_pss_mb"
    assert trend["unit"] == "MB"
    assert trend["labelZh"] == "应用PSS内存"
    assert trend["profile"] == "PICO4-Android12"
    assert trend["profileMismatch"] is False
    assert [r["runId"] for r in trend["runs"]] == [
        "run-20261025-100000-aaaa",
        "run-20261025-110000-bbbb",
    ]
    assert [r["median"] for r in trend["runs"]] == [20.0, 21.0]
    assert trend["runs"][0]["sampleCount"] == 3


def test_trend_rejects_cross_profile_without_explicit_profile(tmp_path):
    store = PerfStore(tmp_path / "data" / "mobile_perf.db")
    for run_id, profile in [
        ("run-a", "PICO4-Android12"),
        ("run-b", "Mate60-Harmony4"),
    ]:
        store.record_session(
            {"run_id": run_id, "target": "tv.danmaku.bili", "device_profile": profile}
        )
    kit = make_kit(tmp_path, FakeHarnessBackend(), store=store)

    with pytest.raises(
        kit_module.ProfileMismatchError, match="显式传参|profile"
    ) as excinfo:
        kit.trend("tv.danmaku.bili", "mem_pss_mb")
    assert "PICO4-Android12" in str(excinfo.value)
    assert "Mate60-Harmony4" in str(excinfo.value)


def test_trend_explicit_profile_overrides_and_marks_caliber_mismatch(tmp_path):
    store = PerfStore(tmp_path / "data" / "mobile_perf.db")
    for run_id, profile, median in [
        ("run-a", "PICO4-Android12", 20.0),
        ("run-b", "Mate60-Harmony4", 99.0),
    ]:
        store.record_session(
            {"run_id": run_id, "target": "tv.danmaku.bili", "device_profile": profile}
        )
        store.record_metrics(
            run_id,
            [{"metric_key": "mem_pss_mb", "unit": "MB", "median": median, "sample_count": 1}],
        )
    kit = make_kit(tmp_path, FakeHarnessBackend(), store=store)

    trend = kit.trend("tv.danmaku.bili", "mem_pss_mb", profile="PICO4-Android12")

    assert [r["runId"] for r in trend["runs"]] == ["run-a"]
    assert trend["profileMismatch"] is True  # 口径不一致：另一档案的 run 被排除


def test_trend_unknown_metric_and_empty_data_raise(tmp_path):
    store = PerfStore(tmp_path / "data" / "mobile_perf.db")
    kit = make_kit(tmp_path, FakeHarnessBackend(), store=store)

    with pytest.raises(TrendError, match="mem_pss_mb"):
        kit.trend("tv.danmaku.bili", "no_such_metric")
    with pytest.raises(TrendError, match="tv.danmaku.bili"):
        kit.trend("tv.danmaku.bili", "mem_pss_mb")


class GateProbe:
    """探针 gate：记录借用窗口与 purpose，供编排断言。"""

    def __init__(self):
        self.held = False
        self.borrows = []
        self.held_at_command = []

    def borrow(self, serial, *, purpose, timeout_s=10.0):
        self.borrows.append((serial, purpose))
        probe = self

        class Token:
            def __enter__(self):
                probe.held = True
                return None

            def __exit__(self, *exc):
                probe.held = False
                return False

        return Token()


class ProbeBackend(FakeHarnessBackend):
    """在每类设备命令执行时记录 gate 持有状态。"""

    def __init__(self, probe):
        super().__init__()
        self._probe = probe
        self.held_at_command = []

    def start(self, target, items):
        self.held_at_command.append(("start", self._probe.held))
        return super().start(target, items)

    def stop(self, session_id, output_dir):
        self.held_at_command.append(("stop", self._probe.held))
        return super().stop(session_id, output_dir)


def test_run_collect_borrows_gate_per_command_and_free_during_recording(tmp_path):
    probe = GateProbe()
    backend = ProbeBackend(probe)
    held_during_recording = []
    kit = make_kit(
        tmp_path, backend, gate=probe,
        sleep=lambda seconds: held_during_recording.append(probe.held),
    )

    kit.run_collect(
        target="tv.danmaku.bili",
        items=["CPU"],
        duration_s=30.0,

        host_run_id="run-gate-1",
        runs_dir=tmp_path / "runs",
    )

    assert probe.borrows == [
        ("749622c9", "mobile_perf.start"),
        ("749622c9", "mobile_perf.clock"),  # 无巡检结论时补采对时短借用
        ("749622c9", "mobile_perf.stop_pull"),
    ]
    assert backend.held_at_command == [("start", True), ("stop", True)]
    assert held_during_recording == [False]  # 录制进行中绝不持锁


def test_run_collect_fails_fast_when_gate_busy_before_any_device_command(tmp_path):
    class BusyGate:
        def borrow(self, serial, *, purpose, timeout_s=10.0):
            raise GateBusyError(f"设备 {serial} 正被其他进程占用（purpose={purpose}）")

    backend = FakeHarnessBackend()
    kit = make_kit(tmp_path, backend, gate=BusyGate())

    with pytest.raises(GateBusyError, match="749622c9"):
        kit.run_collect(
            target="tv.danmaku.bili",
            items=["CPU"],
            duration_s=30.0,
            host_run_id="run-gate-busy",
            runs_dir=tmp_path / "runs",
        )
    assert backend.started == []  # fast-fail：未发出任何设备命令


def test_kit_defaults_to_file_lock_gate(tmp_path):
    kit = PerfKit(adb_path=fake_adb(tmp_path), serial="749622c9")

    assert isinstance(kit.gate, FileLockGate)


# ---- supplement 接线：采集期间旁路补采，stop 后合并入库（票 10） ----

SUPPLEMENT_STDOUT = (
    "1792895592\n"
    "Current Battery Service state:\n"
    "  temperature: 285\n"
    "GPU Utilization (last 1s): 30%\n"
)


class FakeSupplementAdb:
    """模拟 adb：补采轮询命令返回固定 dumpsys 输出。"""

    def __init__(self, stdout=SUPPLEMENT_STDOUT, returncode=0):
        self.stdout = stdout
        self.returncode = returncode
        self.cmds = []

    def __call__(self, cmd, timeout_s):
        self.cmds.append(list(cmd))
        result = type("R", (), {})()
        result.stdout = self.stdout
        result.returncode = self.returncode
        return result


def run_collect_with_supplement(tmp_path, adb_runner, **kwargs):
    store = PerfStore(tmp_path / "data" / "mobile_perf.db")
    kit = make_kit(tmp_path, FakeHarnessBackend(), adb_runner=adb_runner, store=store, **kwargs)
    report = kit.run_collect(
        target="tv.danmaku.bili",
        items=["CPU", "Memory"],
        duration_s=1.0,
        host_run_id="run-supplement-1",
        runs_dir=tmp_path / "runs",
        supplement_interval_s=0.05,
    )
    return kit, store, report


class TestRunCollectSupplement:
    def test_supplement_samples_ingested_and_merged_into_store(self, tmp_path):
        import time as time_module

        adb = FakeSupplementAdb()
        kit, store, report = run_collect_with_supplement(
            tmp_path, adb, sleep=lambda seconds: time_module.sleep(0.3)
        )

        run_dir = tmp_path / "runs" / "run-supplement-1"
        assert (run_dir / "artifacts" / "supplement_thermal_gpu.csv").is_file()

        rows = {row["metric_key"]: row for row in store.fetch_metrics("run-supplement-1")}
        assert rows["temp_battery_c"]["source"] == "pc_supplement"
        assert rows["mem_pss_mb"]["source"] == "solo_pi"  # SoloPi 聚合未被挤出
        assert rows["temp_battery_c"]["sample_count"] >= 1

        assert report["metricsSummary"]["temp_battery_c"]["source"] == "pc_supplement"
        assert report["series"]["temp_battery_c"]["source"] == "pc_supplement"
        assert report["series"]["temp_battery_c"]["points"]

        meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        assert meta["supplement"]["enabled"] is True
        assert meta["supplement"]["rounds"] >= 1
        assert meta["supplement"]["degraded"] == []
        assert store.fetch_events("run-supplement-1") == []

    def test_adb_failure_degrades_and_records_event_without_crashing(self, tmp_path):
        adb = FakeSupplementAdb(stdout="", returncode=1)
        kit, store, report = run_collect_with_supplement(tmp_path, adb)

        meta = json.loads(
            ((tmp_path / "runs" / "run-supplement-1") / "meta.json").read_text(encoding="utf-8")
        )
        assert meta["supplement"]["errors"] == meta["supplement"]["rounds"]
        keys = {entry["key"] for entry in meta["supplement"]["degraded"]}
        assert keys == {"temp_battery_c", "gpu_util_pct"}

        events = store.fetch_events("run-supplement-1")
        assert any(event["kind"] == "supplement_degraded" for event in events)
        rows = {row["metric_key"] for row in store.fetch_metrics("run-supplement-1")}
        assert "temp_battery_c" not in rows

    def test_charging_degrades_temperature_polling(self, tmp_path):

        adb = FakeSupplementAdb()
        store = PerfStore(tmp_path / "data" / "mobile_perf.db")
        kit = make_kit(tmp_path, FakeHarnessBackend(), adb_runner=adb, store=store)
        kit.last_readiness = type(
            "R", (), {"clock_offset_s": 3.0, "degraded_items": ("Battery", "Temperature")}
        )()
        kit.run_collect(
            target="tv.danmaku.bili",
            items=["CPU"],
            duration_s=0.5,
            host_run_id="run-supplement-2",
            runs_dir=tmp_path / "runs2",
            supplement_interval_s=0.05,
        )

        rows = {row["metric_key"] for row in store.fetch_metrics("run-supplement-2")}
        assert "temp_battery_c" not in rows
        assert "gpu_util_pct" in rows
        assert all("battery" not in cmd for cmd in adb.cmds)

    def test_supplement_disabled_by_parameter(self, tmp_path):
        store = PerfStore(tmp_path / "data" / "mobile_perf.db")
        kit = make_kit(tmp_path, FakeHarnessBackend(), store=store)
        kit.run_collect(
            target="tv.danmaku.bili",
            items=["CPU"],
            duration_s=1.0,
            host_run_id="run-nosupplement",
            runs_dir=tmp_path / "runs",
            supplement=False,
        )

        meta = json.loads(
            ((tmp_path / "runs" / "run-nosupplement") / "meta.json").read_text(encoding="utf-8")
        )
        assert meta["supplement"]["enabled"] is False
        rows = {row["metric_key"] for row in store.fetch_metrics("run-nosupplement")}
        assert "temp_battery_c" not in rows


class TestRunCollectClockSync:
    """无巡检结论时 run_collect 实测 device-host 时钟偏移作补采对时基准。"""

    def test_offset_measured_when_no_readiness(self, tmp_path):
        import time as time_module

        now_epoch = int(time_module.time())
        stdout = (
            f"{now_epoch}\n"
            "Current Battery Service state:\n"
            "  temperature: 285\n"
            "GPU Utilization (last 1s): 30%\n"
        )
        adb = FakeSupplementAdb(stdout=stdout)
        kit, store, report = run_collect_with_supplement(
            tmp_path, adb, sleep=lambda seconds: time_module.sleep(0.3)
        )

        meta = json.loads(
            ((tmp_path / "runs" / "run-supplement-1") / "meta.json").read_text(encoding="utf-8")
        )
        assert meta["supplement"]["clockOffsetSource"] == "measured"
        assert abs(meta["supplement"]["clockOffsetS"]) < 5.0

    def test_keeper_offset_preferred_over_measurement(self, tmp_path):

        adb = FakeSupplementAdb()
        store = PerfStore(tmp_path / "data" / "mobile_perf.db")
        kit = make_kit(tmp_path, FakeHarnessBackend(), adb_runner=adb, store=store)
        kit.last_readiness = type(
            "R", (), {"clock_offset_s": 1.5, "degraded_items": ()}
        )()
        kit.run_collect(
            target="tv.danmaku.bili",
            items=["CPU"],
            duration_s=0.5,
            host_run_id="run-offset-keeper",
            runs_dir=tmp_path / "runs",
            supplement_interval_s=0.05,
        )

        meta = json.loads(
            ((tmp_path / "runs" / "run-offset-keeper") / "meta.json").read_text(encoding="utf-8")
        )
        assert meta["supplement"]["clockOffsetSource"] == "keeper"
        assert meta["supplement"]["clockOffsetS"] == 1.5
