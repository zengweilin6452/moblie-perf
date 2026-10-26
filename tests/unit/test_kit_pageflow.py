"""整页加载耗时的编排接线（ticket 19）：页面打标 → 帧时刻换算 → 指标/事件/报告。

合成链路：设备替身给出 framestats 与 `/proc/uptime`，宿主在采集窗口内 mark_page，
验证从打标到 page_load_ms 入库、事件与报告呈现的完整路径（含三种降级）。
"""

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

# 帧完成时刻用设备 uptime 秒表示；合成会话里宿主时钟与设备 uptime 差 1000 s，
# 故 completed_s = 1000 + 会话相对秒（锚点测量会把偏移量测出来）
DEVICE_OFFSET_S = 1000.0


def _framestats_payload(completed_uptimes_s, duration_ms=8.0):
    """按给定的帧完成时刻（设备 uptime 秒）构造 framestats 输出。"""
    header = (
        "Flags,FrameTimelineVsyncId,IntendedVsync,Vsync,InputEventId,HandleInputStart,"
        "AnimationStart,PerformTraversalsStart,DrawStart,FrameDeadline,FrameInterval,"
        "FrameStartTime,SyncQueued,SyncStart,IssueDrawCommandsStart,SwapBuffers,"
        "FrameCompleted,DequeueBufferDuration,QueueBufferDuration,GpuCompleted,"
        "SwapBuffersCompleted,DisplayPresentTime,CommandSubmissionCompleted,"
    )
    rows = []
    for index, completed_s in enumerate(completed_uptimes_s):
        completed_ns = int(completed_s * 1e9)
        intended_ns = completed_ns - int(duration_ms * 1e6)
        rows.append(
            f"0,{index + 1},{intended_ns},{intended_ns},0,0,0,0,0,0,16670000,0,"
            f"0,0,0,0,{completed_ns},0,0,0,0,0,0,"
        )
    return "\n".join(["---PROFILEDATA---", header, *rows, "---PROFILEDATA---"])


class _FakeClock:
    """宿主时钟替身：每次读前进 50 ms（真实 adb 往返就是这个量级）。

    时钟必须真的前进：括号式偏移测量要读两次时钟，固定值会让 rttMs 恒为 0，
    测不出"取中点消掉一半往返"。步长也不能大——0.5 s 的步长会让偏移误差本身
    超过本指标的判据粒度，测出来的数就没有意义了。
    """

    STEP_S = 0.05

    def __init__(self):
        self.value = 0.0

    def __call__(self) -> float:
        current = self.value
        self.value += self.STEP_S
        return current

    def now(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        """模拟"又过了这么久"：设备 uptime 由同一个时钟推导，两边同步前进。"""
        self.value += seconds


def _page_device_call(frames_text, clock: _FakeClock | None = None, uptime_readable=True):
    """设备替身：巡检照旧；gfxinfo 给帧数据，`cat /proc/uptime` 给设备 uptime。

    设备 uptime 随宿主时钟同步前进（真实设备两条时钟本来就同速），否则括号式
    测量算出的偏移会随会话时长漂移，测出来的就不是偏移而是"时钟走得不一样快"。
    """
    base = healthy_keeper_device_call()

    def call(args, timeout_s=15.0):
        if "gfxinfo" in args:
            return SimpleNamespace(returncode=0, stdout=frames_text, stderr="")
        if args[-1] == "/proc/uptime":
            if not uptime_readable:
                return SimpleNamespace(returncode=1, stdout="", stderr="Permission denied")
            uptime = (clock.now() if clock is not None else 0.0) + DEVICE_OFFSET_S
            return SimpleNamespace(returncode=0, stdout=f"{uptime:.2f} 0.00\n", stderr="")
        return base(args, timeout_s)

    return call


def _kit_with_clock(tmp_path, device_call, clock: _FakeClock | None = None):
    """注入假时钟的 kit（会话起点 0，读数按 _FakeClock 前进）。"""
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


def test_mark_page_produces_page_load_metric_and_event(tmp_path):
    """主路径：页面打标 + 逐帧 → page_load_ms 入库，事件与报告都有逐页结论。"""
    # 帧在会话相对 1.0/1.1/1.2 s 完成（设备 uptime 各自 +1000），之后静默
    frames = _framestats_payload([DEVICE_OFFSET_S + 1.0, DEVICE_OFFSET_S + 1.1,
                                 DEVICE_OFFSET_S + 1.2])
    clock = _FakeClock()
    kit = _kit_with_clock(tmp_path, _page_device_call(frames, clock), clock)

    with kit.collect(
        target="tv.danmaku.bili",
        items=["CPU"],
        host_run_id="run-page-1",
        supplement=False,
        frame_poll=True,
    ) as scope:
        scope.mark_page("home")
        clock.advance(5.0)  # 末帧在 1.2 s，之后 3.8 s 无新帧 → 稳定

    report = scope.finalize()

    entry = report["metricsSummary"]["page_load_ms"]
    assert entry["unit"] == "ms"
    assert entry["source"] == "pc_orchestration"
    assert entry["sampleCount"] == 1
    # 帧在会话相对 1.0–1.2 s 完成，末帧后静默 → 稳定点 = 1.2 s；页面起点略早于
    # 首帧，故耗时落在 1.2 s 上下（偏移测量有 25 ms 级的往返残差）
    assert entry["median"] == pytest.approx(1200.0, abs=100.0)
    assert report["pages"]["clockOffsetSource"] == "uptime_bracket"
    assert report["pages"]["clockOffsetS"] == pytest.approx(-DEVICE_OFFSET_S, abs=1.0)
    assert report["pages"]["clockRttMs"] == pytest.approx(50.0, abs=5.0)
    assert report["pages"]["pages"][0]["page"] == "home"
    assert report["pages"]["pages"][0]["status"] == pageflow.STATUS_OK
    events = [event for event in report["events"] if event["kind"] == "page_load"]
    assert len(events) == 1
    assert events[0]["detail"]["page"] == "home"


def test_page_marks_are_distinguishable_from_plain_marks(tmp_path):
    """页面打标与普通打标共用时间轴但语义不同：marks 里 kind 可区分。"""
    frames = _framestats_payload([DEVICE_OFFSET_S + 1.0])
    clock = _FakeClock()
    kit = _kit_with_clock(tmp_path, _page_device_call(frames, clock), clock)

    with kit.collect(
        target="tv.danmaku.bili",
        items=["CPU"],
        host_run_id="run-page-2",
        supplement=False,
        frame_poll=True,
    ) as scope:
        scope.mark("start")
        scope.mark_page("home")

    report = scope.finalize()

    kinds = {mark["label"]: mark.get("kind") for mark in report["marks"]}
    assert kinds == {"start": None, "home": "page"}


def test_multiple_pages_each_get_their_own_result(tmp_path):
    """多页面：每页一条结论与一条事件，聚合行把每页当样本。"""
    frames = _framestats_payload(
        [DEVICE_OFFSET_S + 1.0, DEVICE_OFFSET_S + 1.1, DEVICE_OFFSET_S + 5.0]
    )
    clock = _FakeClock()
    kit = _kit_with_clock(tmp_path, _page_device_call(frames, clock), clock)

    with kit.collect(
        target="tv.danmaku.bili",
        items=["CPU"],
        host_run_id="run-page-3",
        supplement=False,
        frame_poll=True,
    ) as scope:
        scope.mark_page("home")
        scope.mark_page("detail")
        clock.advance(12.0)

    report = scope.finalize()

    pages = report["pages"]["pages"]
    assert [item["page"] for item in pages] == ["home", "detail"]
    assert report["metricsSummary"]["page_load_ms"]["sampleCount"] == 2
    events = [event for event in report["events"] if event["kind"] == "page_load"]
    assert len(events) == 2


def test_page_mark_without_frame_poll_degrades_honestly(tmp_path):
    """没开逐帧：页面标照常进时间轴，但 page_load_ms 如实 not_tested 并说原因。"""
    kit = _kit_with_clock(tmp_path, _page_device_call(""))

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


def test_uptime_unreadable_degrades_without_fabricating_times(tmp_path):
    """取不到设备 uptime：不换算帧时刻，如实 not_tested（不拿错位时刻硬算）。"""
    frames = _framestats_payload([DEVICE_OFFSET_S + 1.0])
    clock = _FakeClock()
    kit = _kit_with_clock(
        tmp_path, _page_device_call(frames, clock, uptime_readable=False), clock
    )

    with kit.collect(
        target="tv.danmaku.bili",
        items=["CPU"],
        host_run_id="run-page-5",
        supplement=False,
        frame_poll=True,
    ) as scope:
        scope.mark_page("home")
        clock.advance(5.0)  # 末帧在 1.2 s，之后 3.8 s 无新帧 → 稳定

    report = scope.finalize()

    entry = report["metricsSummary"]["page_load_ms"]
    assert entry["notTested"] is True
    assert report["pages"]["clockOffsetSource"] == "no_uptime_offset"
    assert report["pages"]["clockOffsetS"] is None


def test_no_page_mark_leaves_no_page_keys(tmp_path):
    """没打页面标：不产出 page_load_ms，报告也没有 pages 段（不硬造）。"""
    frames = _framestats_payload([DEVICE_OFFSET_S + 1.0])
    clock = _FakeClock()
    kit = _kit_with_clock(tmp_path, _page_device_call(frames, clock), clock)

    report = kit.run_collect(
        target="tv.danmaku.bili",
        items=["CPU"],
        duration_s=5.0,
        host_run_id="run-page-6",
        runs_dir=tmp_path / "runs",
        supplement=False,
        frame_poll=True,
    )

    assert "page_load_ms" not in report["metricsSummary"]
    assert "pages" not in report


def test_mark_page_before_scope_entry_raises(tmp_path):
    kit = _kit_with_clock(tmp_path, _page_device_call(""))
    scope = kit.collect(target="tv.danmaku.bili", items=["CPU"], supplement=False)

    with pytest.raises(RuntimeError, match="mark_page"):
        scope.mark_page("home")


def test_empty_page_name_is_rejected(tmp_path):
    frames = _framestats_payload([DEVICE_OFFSET_S + 1.0])
    clock = _FakeClock()
    kit = _kit_with_clock(tmp_path, _page_device_call(frames, clock), clock)

    with kit.collect(
        target="tv.danmaku.bili",
        items=["CPU"],
        host_run_id="run-page-7",
        supplement=False,
        frame_poll=True,
    ) as scope:
        with pytest.raises(ValueError, match="page"):
            scope.mark_page("")


def test_degraded_capabilities_do_not_block_each_other(tmp_path, monkeypatch):
    """工单 20：mitmproxy 缺席 + 弱网档 + 页面打标同时进行——各自降级，其余照常。

    这是"任一子能力不可用时其余照常"的组合验收：弱网指标 not_tested 不影响
    整页耗时照常出值，整页的降级也不影响采集闭环与会话入库。
    """
    from types import SimpleNamespace

    from tests.unit.test_kit_netprof import _patch_profiler

    _patch_profiler(monkeypatch, [], available=False)  # mitmproxy 缺席
    frames = _framestats_payload([DEVICE_OFFSET_S + 0.2, DEVICE_OFFSET_S + 0.3,
                                 DEVICE_OFFSET_S + 0.4])
    clock = _FakeClock()
    base = healthy_keeper_device_call()

    def call(args, timeout_s=15.0):
        if "reverse" in args or "http_proxy" in args:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "gfxinfo" in args:
            return SimpleNamespace(returncode=0, stdout=frames, stderr="")
        if args[-1] == "/proc/uptime":
            uptime = clock.now() + DEVICE_OFFSET_S
            return SimpleNamespace(returncode=0, stdout=f"{uptime:.2f} 0.00\n", stderr="")
        return base(args, timeout_s)

    kit = _kit_with_clock(tmp_path, call, clock)

    with kit.collect(
        target="tv.danmaku.bili",
        items=["CPU"],
        host_run_id="run-mixed-degrade",
        supplement=False,
        frame_poll=True,
        net_profile="weak",
    ) as scope:
        scope.mark_page("home")
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
