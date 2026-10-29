"""整页加载耗时的编排接线（ticket 19）：页面打标 → 帧时刻换算 → 指标/事件/报告。

合成链路：设备替身按宿主时钟推进产出 framestats，宿主在采集窗口内 mark_page，
验证从打标到 page_load_ms 入库、事件与报告呈现的完整路径（含三种降级）。

设备时钟域：帧完成时刻是设备 ``CLOCK_MONOTONIC`` 秒，与宿主时间轴差一个常数
（合成会话取 1000 s）。偏移不靠 ``/proc/uptime``（那是 CLOCK_BOOTTIME，含休眠，
真机实测可差 3 天以上），而由逐帧轮询自身的锚点估计——所以替身必须让帧时刻
**随宿主时钟真实推进**，否则测出来的是"时钟走得不一样快"。
"""

import time
from types import SimpleNamespace

import pytest
from tests.unit.test_kit import (  # noqa: E402 - 复用既有替身
    FakeHarnessBackend,
    fake_adb,
    healthy_doctor_fn,
    healthy_keeper_device_call,
)

from mobile_perf import pageflow
from mobile_perf.keeper import CheckResult
from mobile_perf.kit import PerfKit
from mobile_perf.store import PerfStore

# 合成会话里宿主时钟与设备单调时钟差 1000 s
DEVICE_OFFSET_S = 1000.0

# 轮询间隔：测试要等帧"产生"，间隔小才跑得快（真机默认 2 s）
FRAME_INTERVAL_S = 0.1


def _framestats_payload(completed_s, duration_ms=8.0):
    """按给定的帧完成时刻（设备单调时钟秒）构造 framestats 输出。"""
    header = (
        "Flags,FrameTimelineVsyncId,IntendedVsync,Vsync,InputEventId,HandleInputStart,"
        "AnimationStart,PerformTraversalsStart,DrawStart,FrameDeadline,FrameInterval,"
        "FrameStartTime,SyncQueued,SyncStart,IssueDrawCommandsStart,SwapBuffers,"
        "FrameCompleted,DequeueBufferDuration,QueueBufferDuration,GpuCompleted,"
        "SwapBuffersCompleted,DisplayPresentTime,CommandSubmissionCompleted,"
    )
    rows = []
    for index, completed in enumerate(completed_s):
        completed_ns = int(completed * 1e9)
        intended_ns = completed_ns - int(duration_ms * 1e6)
        rows.append(
            f"0,{index + 1},{intended_ns},{intended_ns},0,0,0,0,0,0,16670000,0,"
            f"0,0,0,0,{completed_ns},0,0,0,0,0,0,"
        )
    return "\n".join(["---PROFILEDATA---", header, *rows, "---PROFILEDATA---"])


class _FakeClock:
    """宿主时钟替身：随真实时间推进（轮询线程与主线程同时读，必须线程安全）。

    读不改变状态，只有 ``advance`` 才跳时间——跳代表"宿主在这里等待/闲置了
    这么久"。用读时自增的假时钟会让两个线程互相偷步，测出来的偏移不可复现。

    ``base`` 故意取一个大值（模拟宿主开机以来的单调时钟）：会话相对秒与
    绝对读数因此不同，锚点若错用了绝对读数会立刻暴露成巨大的偏移误差。
    """

    def __init__(self, base: float = 50_000.0):
        self.base = base
        self._offset = 0.0
        self._t0 = time.monotonic()

    def __call__(self) -> float:
        return self.base + self._offset + (time.monotonic() - self._t0)

    def now(self) -> float:
        return self()

    def rel(self) -> float:
        """会话相对秒（会话从 base 起算）。"""
        return self() - self.base

    def advance(self, seconds: float) -> None:
        """跳过这么久（设备时钟由同一读数推导，两边同步前进）。"""
        self._offset += seconds


def _page_device_call(frames_s, clock: _FakeClock):
    """设备替身：巡检照旧；gfxinfo 只回"此刻已经产生"的帧。

    ``frames_s`` 是**会话相对秒**：第 i 帧在宿主时钟走到 ``frames_s[i]`` 时产生，
    其设备时刻为 ``frames_s[i] + DEVICE_OFFSET_S``。这样轮询读到的帧集合随时间
    增长，与真机一致（真机的帧也不是一次性全在那里）。
    """
    base = healthy_keeper_device_call()

    def call(args, timeout_s=15.0):
        if "gfxinfo" in args:
            produced = [t for t in frames_s if t <= clock.rel()]
            payload = _framestats_payload([t + DEVICE_OFFSET_S for t in produced])
            return SimpleNamespace(returncode=0, stdout=payload, stderr="")
        return base(args, timeout_s)

    return call


def _kit_with_clock(tmp_path, device_call, clock: _FakeClock | None = None):
    """注入假时钟的 kit（宿主读数从 base 起算，会话相对秒由 kit 自己减起点）。"""
    clock = clock or _FakeClock()
    return PerfKit(
        adb_path=fake_adb(tmp_path),
        serial="749622c9",
        runs_dir=tmp_path / "runs",
        backend=FakeHarnessBackend(),
        clock=clock,
        sleep=lambda seconds: None,
        store=PerfStore(tmp_path / "data" / "mobile_perf.db"),
        device_call=device_call,
        doctor_fn=healthy_doctor_fn(),
        dialog_probe=lambda: CheckResult("dialog_probe", "skipped", {"reason": "stub"}),
    )


def _wait_until(clock: _FakeClock, seconds: float) -> None:
    """等宿主时钟走到会话相对 seconds（帧按会话相对秒产生，等它们出现）。"""
    deadline = time.monotonic() + max(0.0, seconds - clock.rel()) + 1.0
    while clock.rel() < seconds and time.monotonic() < deadline:
        time.sleep(0.02)


def test_mark_page_produces_page_load_metric_and_event(tmp_path):
    """主路径：页面打标 + 逐帧 → page_load_ms 入库，事件与报告都有逐页结论。"""
    frames = [0.2, 0.3, 0.4]  # 三帧后静默 → 稳定点 0.4 s
    clock = _FakeClock()
    kit = _kit_with_clock(tmp_path, _page_device_call(frames, clock), clock)

    with kit.collect(
        target="tv.danmaku.bili",
        items=["CPU"],
        host_run_id="run-page-1",
        supplement=False,
        frame_poll=True,
        frame_interval_s=FRAME_INTERVAL_S,
    ) as scope:
        scope.mark_page("home")  # t0 略早于首帧
        _wait_until(clock, 0.7)
        clock.advance(5.0)  # 末帧后 5 s 无新帧 → 稳定

    report = scope.finalize()

    entry = report["metricsSummary"]["page_load_ms"]
    assert entry["unit"] == "ms"
    assert entry["source"] == "pc_orchestration"
    assert entry["sampleCount"] == 1
    # 稳定点 0.4 s 减去页面起点（≈0.05 s）；偏移估计误差在一个轮询间隔内
    assert entry["median"] == pytest.approx(350.0, abs=200.0)
    assert report["pages"]["clockOffsetSource"] == "frame_anchor"
    assert report["pages"]["clockOffsetS"] == pytest.approx(-DEVICE_OFFSET_S, abs=300.0)
    assert report["pages"]["clockAnchorCount"] >= 1
    # 可信度随锚点一起入库：合成链路里每次 adb 往返是瞬时的，精度≈0
    assert report["pages"]["clockPrecisionMs"] is not None
    assert report["pages"]["pages"][0]["page"] == "home"
    assert report["pages"]["pages"][0]["status"] == pageflow.STATUS_OK
    events = [event for event in report["events"] if event["kind"] == "page_load"]
    assert len(events) == 1
    assert events[0]["detail"]["page"] == "home"


def test_page_marks_are_distinguishable_from_plain_marks(tmp_path):
    """页面打标与普通打标共用时间轴但语义不同：marks 里 kind 可区分。"""
    clock = _FakeClock()
    kit = _kit_with_clock(tmp_path, _page_device_call([0.2], clock), clock)

    with kit.collect(
        target="tv.danmaku.bili",
        items=["CPU"],
        host_run_id="run-page-2",
        supplement=False,
        frame_poll=True,
        frame_interval_s=FRAME_INTERVAL_S,
    ) as scope:
        scope.mark("start")
        scope.mark_page("home")
        _wait_until(clock, 0.5)

    report = scope.finalize()

    kinds = {mark["label"]: mark.get("kind") for mark in report["marks"]}
    assert kinds == {"start": None, "home": "page"}


def test_multiple_pages_each_get_their_own_result(tmp_path):
    """多页面：每页一条结论与一条事件，聚合行把每页当样本。"""
    clock = _FakeClock()
    kit = _kit_with_clock(tmp_path, _page_device_call([0.2, 0.5], clock), clock)

    with kit.collect(
        target="tv.danmaku.bili",
        items=["CPU"],
        host_run_id="run-page-3",
        supplement=False,
        frame_poll=True,
        frame_interval_s=FRAME_INTERVAL_S,
    ) as scope:
        scope.mark_page("home")
        scope.mark_page("detail")
        _wait_until(clock, 0.8)
        clock.advance(5.0)

    report = scope.finalize()

    pages = report["pages"]["pages"]
    assert [item["page"] for item in pages] == ["home", "detail"]
    assert report["metricsSummary"]["page_load_ms"]["sampleCount"] == 2
    events = [event for event in report["events"] if event["kind"] == "page_load"]
    assert len(events) == 2


def test_page_mark_without_frame_poll_degrades_honestly(tmp_path):
    """没开逐帧：页面标照常进时间轴，但 page_load_ms 如实 not_tested 并说原因。"""
    kit = _kit_with_clock(tmp_path, _page_device_call([], _FakeClock()))

    with kit.collect(
        target="tv.danmaku.bili",
        items=["CPU"],
        host_run_id="run-page-4",
        supplement=False,
        frame_poll=False,
    ) as scope:
        scope.mark_page("home")

    report = scope.finalize()

    entry = report["metricsSummary"]["page_load_ms"]
    assert entry["notTested"] is True
    assert "--frame-poll" in entry["notTestedReason"]
    assert report["pages"]["clockOffsetSource"] == "no_frame_poll"
    degraded = [e for e in report["events"] if e["kind"] == "page_load_degraded"]
    assert degraded and degraded[0]["detail"]["pages"] == ["home"]


def test_no_frames_degrades_without_fabricating_times(tmp_path):
    """逐帧窗口内一帧都没有：没有锚点可换算，如实 not_tested（不拿错位时刻硬算）。"""
    clock = _FakeClock()
    kit = _kit_with_clock(tmp_path, _page_device_call([], clock), clock)

    with kit.collect(
        target="tv.danmaku.bili",
        items=["CPU"],
        host_run_id="run-page-5",
        supplement=False,
        frame_poll=True,
        frame_interval_s=FRAME_INTERVAL_S,
    ) as scope:
        scope.mark_page("home")
        _wait_until(clock, 0.3)

    report = scope.finalize()

    entry = report["metricsSummary"]["page_load_ms"]
    assert entry["notTested"] is True
    assert report["pages"]["clockOffsetSource"] == "no_frames_anchor"
    assert report["pages"]["clockOffsetS"] is None
    assert report["pages"]["clockAnchorCount"] is None


def test_no_page_mark_leaves_no_page_keys(tmp_path):
    """没打页面标：不产出 page_load_ms，报告也没有 pages 段（不硬造）。"""
    clock = _FakeClock()
    kit = _kit_with_clock(tmp_path, _page_device_call([0.2], clock), clock)

    report = kit.run_collect(
        target="tv.danmaku.bili",
        items=["CPU"],
        duration_s=5.0,
        host_run_id="run-page-6",
        runs_dir=tmp_path / "runs",
        supplement=False,
        frame_poll=True,
        frame_interval_s=FRAME_INTERVAL_S,
    )

    assert "page_load_ms" not in report["metricsSummary"]
    assert "pages" not in report


def test_mark_page_before_scope_entry_raises(tmp_path):
    clock = _FakeClock()
    kit = _kit_with_clock(tmp_path, _page_device_call([], clock), clock)
    scope = kit.collect(target="tv.danmaku.bili", items=["CPU"], supplement=False)

    with pytest.raises(RuntimeError, match="mark_page"):
        scope.mark_page("home")


def test_empty_page_name_is_rejected(tmp_path):
    clock = _FakeClock()
    kit = _kit_with_clock(tmp_path, _page_device_call([0.2], clock), clock)

    with kit.collect(
        target="tv.danmaku.bili",
        items=["CPU"],
        host_run_id="run-page-7",
        supplement=False,
        frame_poll=True,
        frame_interval_s=FRAME_INTERVAL_S,
    ) as scope:
        with pytest.raises(ValueError, match="page"):
            scope.mark_page("")


def test_degraded_capabilities_do_not_block_each_other(tmp_path, monkeypatch):
    """工单 20：mitmproxy 缺席 + 弱网档 + 页面打标同时进行——各自降级，其余照常。

    这是"任一子能力不可用时其余照常"的组合验收：弱网指标 not_tested 不影响
    整页耗时照常出值，整页的降级也不影响采集闭环与会话入库。
    """
    from tests.unit.test_kit_netprof import _patch_profiler

    _patch_profiler(monkeypatch, [], available=False)  # mitmproxy 缺席
    clock = _FakeClock()
    base_call = _page_device_call([0.2, 0.3, 0.4], clock)
    base = healthy_keeper_device_call()

    def call(args, timeout_s=15.0):
        if "reverse" in args or "http_proxy" in args:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return base_call(args, timeout_s) if "gfxinfo" in args else base(args, timeout_s)

    kit = _kit_with_clock(tmp_path, call, clock)

    with kit.collect(
        target="tv.danmaku.bili",
        items=["CPU"],
        host_run_id="run-mixed-degrade",
        supplement=False,
        frame_poll=True,
        frame_interval_s=FRAME_INTERVAL_S,
        net_profile="weak",
    ) as scope:
        scope.mark_page("home")
        _wait_until(clock, 0.7)
        clock.advance(5.0)

    report = scope.finalize()

    # 弱网缺席 → 连接级指标如实 not_tested，附安装提示
    net_entry = report["metricsSummary"]["net_conn_duration_ms"]
    assert net_entry["notTested"] is True
    assert "MOBILE_PERF_MITMDUMP" in net_entry["hint"]
    # 整页耗时不受弱网降级影响，照常出值
    page_entry = report["metricsSummary"]["page_load_ms"]
    assert page_entry["sampleCount"] == 1
    assert report["pages"]["pages"][0]["status"] == pageflow.STATUS_OK
    # 档位照常入会话表（跑了 weak 档与是否生效无关），采集闭环完整
    assert kit.store.fetch_session(report["runId"])["net_profile"]["name"] == "weak"
    assert report["marks"][0]["kind"] == "page"
