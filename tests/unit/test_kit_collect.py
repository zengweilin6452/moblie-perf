"""kit.collect 交互式采集窗口的单元测试：假后端注入，不碰 adb 与真机。

collect() 是设计方案 §6.2 的宿主 API：with 块内 UI 驱动 + mark 打标，
finalize 停止采集并走完整归档/入库/报告链路——pytest 钩子与其余宿主
共用这条接缝，run_collect 是它的定时收口特例。
"""

import pytest

from tests.unit.test_kit import FakeHarnessBackend, make_kit


def _counting_kit(tmp_path, backend=None):
    ticks = iter(float(i) for i in range(10_000))
    return make_kit(
        tmp_path, backend or FakeHarnessBackend(), clock=lambda: next(ticks)
    )


def test_collect_context_marks_and_finalize_returns_report(tmp_path):
    kit = _counting_kit(tmp_path)

    with kit.collect(
        target="tv.danmaku.bili", items=["CPU"], host_run_id="run-ctx-1"
    ) as scope:
        scope.mark("scroll_done")

    report = scope.finalize()

    assert report["runId"] == "run-ctx-1"
    assert report["marks"] == [{"label": "scroll_done", "elapsedS": 1.0}]
    assert report["durationS"] == 2.0  # start=0, stop=2（mark 计 1）
    assert (tmp_path / "runs" / "run-ctx-1" / "report.json").is_file()


def test_collect_finalize_is_idempotent(tmp_path):
    kit = _counting_kit(tmp_path)

    with kit.collect(target="tv.danmaku.bili", items=["CPU"]) as scope:
        pass
    first = scope.finalize()

    assert scope.finalize() is first


def test_collect_finalizes_even_when_body_raises(tmp_path):
    """with 块内用例失败也照常收口：报告仍产出，供 pytest 钩子写结论。"""
    kit = _counting_kit(tmp_path)

    with pytest.raises(RuntimeError, match="UI 步骤失败"):
        with kit.collect(
            target="tv.danmaku.bili", items=["CPU"], host_run_id="run-ctx-2"
        ) as scope:
            scope.mark("before_failure")
            raise RuntimeError("UI 步骤失败")

    report = scope.report
    assert report["runId"] == "run-ctx-2"
    assert report["marks"] == [{"label": "before_failure", "elapsedS": 1.0}]


class ForegroundFakeAdb:
    """模拟 adb：dumpsys 按脚本序列返回前台组件，记录全部命令。"""

    def __init__(self, foreground_script):
        self.script = list(foreground_script)
        self.calls = []

    def __call__(self, cmd, timeout_s):
        class Result:
            def __init__(self, stdout):
                self.stdout = stdout
                self.returncode = 0

        self.calls.append(list(cmd))
        if "dumpsys" in cmd:
            line = self.script.pop(0) if self.script else ""
            stdout = (
                f"  mResumedActivity: ActivityRecord{{8412a4 u0 {line} t42}}\n"
                if line
                else ""
            )
            return Result(stdout)
        return Result("")


def test_collect_restores_foreground_when_solo_pi_steals_it(tmp_path):
    """Q16：start 后前台被 SoloPi 控制面抢走 → 守卫还原并记事件；stop 后不再漂移。"""
    adb = ForegroundFakeAdb(
        [
            "tv.danmaku.bili/.ui.MainActivity",  # start 前
            "com.alipay.hulu/com.alipay.hulu.common.scheme.AdbSchemeActivity",  # start 后被抢
            "tv.danmaku.bili/.ui.MainActivity",  # stop 后
        ]
    )
    kit = make_kit(
        tmp_path,
        FakeHarnessBackend(),
        adb_runner=adb,
    )

    with kit.collect(
        target="tv.danmaku.bili",
        items=["CPU"],
        host_run_id="run-fg-1",
        supplement=False,  # 补采轮询会另行调用 adb，与守卫断言无关
    ) as scope:
        pass

    report = scope.finalize()
    events = [e for e in report["events"] if e["kind"] == "foreground"]
    assert len(events) == 1
    detail = events[0]["detail"]
    assert detail["phase"] == "after_start"
    assert detail["before"] == "tv.danmaku.bili"
    assert detail["observed"] == "com.alipay.hulu"
    assert detail["restored"] is True
    assert detail["restoreVia"] == "component"
    assert any(cmd[-4:-1] == ["am", "start", "-n"] for cmd in adb.calls)


def test_collect_skips_foreground_guard_when_restore_disabled(tmp_path):
    adb = ForegroundFakeAdb([])
    kit = make_kit(
        tmp_path,
        FakeHarnessBackend(),
        adb_runner=adb,
        foreground_restore=False,  # kit 级开关
    )

    with kit.collect(
        target="tv.danmaku.bili",
        items=["CPU"],
        host_run_id="run-fg-2",
        supplement=False,
    ) as scope:
        pass
    scope.finalize()

    assert not any("dumpsys" in cmd for cmd in adb.calls)
    assert scope.foreground_events == []
