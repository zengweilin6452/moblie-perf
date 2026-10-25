"""run_collect 与 logwatch 的接线测试：注入 watcher 替身，不碰 adb 与真机。"""


from tests.unit.test_kit import FakeHarnessBackend, fake_adb

from mobile_perf.gate import FileLockGate
from mobile_perf.keeper import CheckResult
from mobile_perf.kit import PerfKit
from mobile_perf.store import PerfStore


class StubWatcher:
    """logwatch 替身：记录 start/stop，返回预设事件。"""

    def __init__(self, events=None, start_error=None):
        self.events = events or []
        self.start_error = start_error
        self.started = False
        self.stopped = False
        self.kwargs = None

    def start(self):
        if self.start_error:
            raise self.start_error
        self.started = True

    def stop(self, timeout_s=5.0):
        self.stopped = True
        return self.events


CRASH_EVENT = {
    "kind": "crash",
    "ts": "2026-01-02T12:00:02.001+08:00",
    "detail": {
        "package": "tv.danmaku.bili",
        "pid": 5982,
        "source": "logcat_main",
        "message": "FATAL EXCEPTION: main",
        "stack": ["FATAL EXCEPTION: main"],
        "tsSource": "device_epoch_calibrated",
        "deviceEpochS": 1767333600.0,
    },
}


def make_kit(tmp_path, **kwargs):
    store = PerfStore(tmp_path / "data" / "mobile_perf.db")
    kwargs.setdefault("adb_path", fake_adb(tmp_path))
    kwargs.setdefault("serial", "749622c9")
    kwargs.setdefault("runs_dir", tmp_path / "runs")
    kwargs.setdefault("backend", FakeHarnessBackend())
    kwargs.setdefault("clock", iter([0.0, 30.0]).__next__)
    kwargs.setdefault("sleep", lambda seconds: None)
    kwargs.setdefault("store", store)
    kwargs.setdefault("gate", FileLockGate(tmp_path / "gate-leases"))
    # run_collect 巡检先行（A2）：默认接健康巡检桩
    from tests.unit.test_kit import healthy_doctor_fn, healthy_keeper_device_call

    kwargs.setdefault("device_call", healthy_keeper_device_call())
    kwargs.setdefault("doctor_fn", healthy_doctor_fn())
    kwargs.setdefault(
        "dialog_probe",
        lambda: CheckResult("dialog_probe", "skipped", {"reason": "stub"}),
    )
    return PerfKit(**kwargs), store


def test_run_collect_writes_watcher_events_to_store_and_report(tmp_path):
    watcher = StubWatcher(events=[CRASH_EVENT])
    kit, store = make_kit(tmp_path, logwatch_factory=lambda **kw: watcher)

    report = kit.run_collect(
        target="tv.danmaku.bili",
        items=["CPU"],
        duration_s=30.0,
        host_run_id="run-watch-1",
        runs_dir=tmp_path / "runs",
    )

    assert watcher.started and watcher.stopped
    events = store.fetch_events("run-watch-1")
    crash_rows = [e for e in events if e["kind"] == "crash"]
    assert len(crash_rows) == 1
    assert crash_rows[0]["ts"] == CRASH_EVENT["ts"]
    assert crash_rows[0]["detail"]["package"] == "tv.danmaku.bili"

    assert report["events"] == events
    persisted = report  # report.json 与返回值一致（report.json 断言在 test_kit 覆盖）
    assert any(e["kind"] == "crash" for e in persisted["events"])


def test_run_collect_passes_target_packages_and_offset_to_factory(tmp_path):
    captured = {}

    def factory(**kwargs):
        captured.update(kwargs)
        return StubWatcher()

    kit, _ = make_kit(tmp_path, logwatch_factory=factory)
    kit.run_collect(
        target="tv.danmaku.bili",
        items=["CPU"],
        duration_s=30.0,
        runs_dir=tmp_path / "runs",
    )

    assert captured["packages"] == ["tv.danmaku.bili"]
    # run_collect 巡检先行（A2）：offset 来自 keeper 巡检结论（健康桩时钟同步 → 接近 0）
    assert captured["clock_offset_s"] is not None
    assert abs(captured["clock_offset_s"]) < 5.0


def test_run_collect_uses_calibrated_offset_from_last_readiness(tmp_path):
    from mobile_perf.keeper import CheckResult, ReadinessReport

    captured = {}

    def factory(**kwargs):
        captured.update(kwargs)
        return StubWatcher()

    kit, _ = make_kit(tmp_path, logwatch_factory=factory)
    kit.last_readiness = ReadinessReport(
        serial="749622c9",
        ok=True,
        needs_attention=False,
        healed=False,
        checks=(CheckResult("clock_offset", "ok", {}),),
        events=(),
        degraded_items=(),
        foreground_package=None,
        clock_offset_s=-1.5,
    )
    with kit.collect(
        target="tv.danmaku.bili",
        items=["CPU"],
        runs_dir=tmp_path / "runs",
    ):
        pass

    assert captured["clock_offset_s"] == -1.5


def test_watcher_never_borrows_gate(tmp_path):
    purposes: list[str] = []

    class SpyGate(FileLockGate):
        def borrow(self, serial, *, purpose, timeout_s=10.0):
            purposes.append(purpose)
            return super().borrow(serial, purpose=purpose, timeout_s=timeout_s)

    kit, _ = make_kit(
        tmp_path,
        gate=SpyGate(tmp_path / "spy-leases"),
        logwatch_factory=lambda **kw: StubWatcher(events=[CRASH_EVENT]),
    )
    kit.run_collect(
        target="tv.danmaku.bili",
        items=["CPU"],
        duration_s=30.0,
        runs_dir=tmp_path / "runs",
    )

    assert purposes  # start/stop 照常借用
    assert all("logwatch" not in purpose for purpose in purposes)


def test_run_collect_survives_watcher_start_failure(tmp_path):
    watcher = StubWatcher(start_error=RuntimeError("adb not found"))
    kit, store = make_kit(tmp_path, logwatch_factory=lambda **kw: watcher)

    report = kit.run_collect(
        target="tv.danmaku.bili",
        items=["CPU"],
        duration_s=30.0,
        host_run_id="run-watch-err",
        runs_dir=tmp_path / "runs",
    )

    assert report["runId"] == "run-watch-err"
    kinds = [e["kind"] for e in store.fetch_events("run-watch-err")]
    assert "logwatch_warning" in kinds
    assert "crash" not in kinds
