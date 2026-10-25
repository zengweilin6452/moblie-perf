"""kit.collect 交互式采集窗口的单元测试：假后端注入，不碰 adb 与真机。

collect() 是设计方案 §6.2 的宿主 API：with 块内 UI 驱动 + mark 打标，
finalize 停止采集并走完整归档/入库/报告链路——pytest 钩子与其余宿主
共用这条接缝，run_collect 是它的定时收口特例。
"""

from pathlib import Path

import pytest
from tests.unit.test_kit import FakeHarnessBackend, fake_adb, make_kit


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


def test_ensure_ready_restores_foreground_after_doctor_steals(tmp_path):
    """Q16 证据：Harness doctor 抢前台 → ensure_ready 守卫还原并随报告返回事件。"""
    from types import SimpleNamespace

    from mobile_perf.gate import FileLockGate
    from mobile_perf.keeper import CheckResult
    from mobile_perf.kit import PerfKit
    from mobile_perf.store import PerfStore

    dumpsys_script = iter(
        [
            "  ResumedActivity: ActivityRecord{1 u0 tv.danmaku.bili/.MainActivityV2 t1}\n",
            "  ResumedActivity: ActivityRecord{1 u0 tv.danmaku.bili/.MainActivityV2 t1}\n",
            "  ResumedActivity: ActivityRecord{2 u0 com.alipay.hulu/.activity.SplashActivity t2}\n",
        ]
    )
    recorded = []

    def fake_call(args, timeout_s=15.0):
        class Result:
            def __init__(self, stdout, returncode=0, stderr=""):
                self.stdout = stdout
                self.returncode = returncode
                self.stderr = stderr

        recorded.append(list(args))
        if args == ["devices"]:
            return Result("List of devices attached\n749622c9\tdevice\n")
        if "accessibility" in args:
            return Result("")
        if "battery" in args:
            return Result("AC powered: false\nUSB powered: false\n")
        if "activity" in args:
            return Result(next(dumpsys_script))
        if "date" in args:
            return Result("1792895595")
        return Result("")

    doctor = SimpleNamespace(success=True, data={"failedChecks": [], "missingPermissions": []})
    kit = PerfKit(
        adb_path=fake_adb(tmp_path),
        serial="749622c9",
        runs_dir=tmp_path / "runs",
        store=PerfStore(tmp_path / "data" / "mobile_perf.db"),
        gate=FileLockGate(tmp_path / "gate-leases"),
        device_call=fake_call,
        doctor_fn=lambda: doctor,
        dialog_probe=lambda: CheckResult("dialog_probe", "skipped", {"reason": "test"}),
    )

    report = kit.ensure_ready()

    fg_events = [e for e in report.events if e.get("kind") == "foreground"]
    assert len(fg_events) == 1
    detail = fg_events[0]["detail"]
    assert detail["phase"] == "after_ready"
    assert detail["observed"] == "com.alipay.hulu"
    assert detail["restored"] is True
    assert any(args[-4:-1] == ["am", "start", "-n"] for args in recorded)


# ---- A5：SessionSpec 库 API 主形态（§6.2） ----

def test_collect_accepts_session_spec(tmp_path):
    from mobile_perf import SessionSpec

    kit = _counting_kit(tmp_path)
    spec = SessionSpec(target="tv.danmaku.bili", items=["CPU"], host_run_id="run-spec-1")

    with kit.collect(spec) as scope:
        scope.mark("done")

    report = scope.finalize()
    assert report["runId"] == "run-spec-1"
    assert report["target"] == "tv.danmaku.bili"
    assert report["items"] == ["CPU"]


def test_collect_kwargs_override_spec_fields(tmp_path):
    from mobile_perf import SessionSpec

    kit = _counting_kit(tmp_path)
    spec = SessionSpec(target="tv.danmaku.bili", items=["CPU"], host_run_id="run-spec-a")

    with kit.collect(spec, host_run_id="run-spec-b") as scope:
        pass

    assert scope.finalize()["runId"] == "run-spec-b"  # 显式 kwargs 优先于 spec 字段


def test_session_spec_defaults_and_export():
    from mobile_perf import SessionSpec
    from mobile_perf.supplement import DEFAULT_INTERVAL_S

    spec = SessionSpec(target="tv.danmaku.bili", items=["CPU", "FPS"])

    assert spec.host_run_id is None
    assert spec.runs_dir is None
    assert spec.supplement is True
    assert spec.supplement_interval_s == DEFAULT_INTERVAL_S


def test_collect_without_spec_or_target_raises(tmp_path):
    kit = _counting_kit(tmp_path)

    with pytest.raises(TypeError, match="SessionSpec|target"):
        kit.collect()


class FpsFakeBackend(FakeHarnessBackend):
    """FPS 采集后端：产物含「最长延迟时间」逐窗口列，覆盖三档分级。"""

    def stop(self, session_id, output_dir):
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        rows = ["RecordTime,最长延迟时间(ms),extra,SimpleTime"]
        rows += [
            f"{1792895595000 + i * 1000},{value},tv.danmaku.bili,{i}"
            for i, value in enumerate([20.0, 150.0, 400.0, 900.0])
        ]
        Path(output_dir).joinpath("最长延迟时间_FPS_fake.csv").write_text(
            "\n".join(rows) + "\n", encoding="gb18030"
        )
        return {
            "success": True,
            "state": "stopped",
            "sessionId": session_id,
            "artifacts": {"performance": {"localDir": str(output_dir)}},
        }


def test_collect_ingests_jank_grades_from_fps_artifacts(tmp_path):
    """FPS 采集自动带出分级：新 key 进 metricsSummary、入库、报告双语标签。"""
    kit = make_kit(tmp_path, FpsFakeBackend())

    report = kit.run_collect(
        target="tv.danmaku.bili",
        items=["CPU", "FPS"],
        duration_s=30.0,
        host_run_id="run-jank-1",
        runs_dir=tmp_path / "runs",
        supplement=False,
    )

    summary = report["metricsSummary"]
    assert summary["app_jank_light_count"]["max"] == 1.0
    assert summary["app_jank_heavy_count"]["max"] == 1.0
    assert summary["app_jank_severe_count"]["max"] == 1.0
    assert summary["app_jank_severe_count"]["labelZh"] == "严重卡顿窗口数(>700ms)"
    assert summary["app_jank_severe_count"]["labelEn"] == "Severe jank windows (>700ms)"

    rows = {row["metric_key"]: row for row in kit.store.fetch_metrics("run-jank-1")}
    assert rows["app_jank_severe_count"]["max"] == 1.0
    assert rows["app_jank_severe_count"]["unit"] == "次"


def test_collect_without_fps_does_not_invent_jank_keys(tmp_path):
    """只采 CPU/Memory 时不产出分级 key（不硬造 0 值）。"""
    kit = make_kit(tmp_path, FakeHarnessBackend())

    report = kit.run_collect(
        target="tv.danmaku.bili",
        items=["CPU", "Memory"],
        duration_s=30.0,
        host_run_id="run-jank-2",
        runs_dir=tmp_path / "runs",
        supplement=False,
    )

    assert not [key for key in report["metricsSummary"] if key.startswith("app_jank_")]


def test_collect_accepts_custom_jank_thresholds(tmp_path):
    """阈值归宿主：collect 可显式传分级阈值，聚合与报告口径一致。"""
    kit = make_kit(tmp_path, FpsFakeBackend())

    report = kit.run_collect(
        target="tv.danmaku.bili",
        items=["CPU", "FPS"],
        duration_s=30.0,
        host_run_id="run-jank-3",
        runs_dir=tmp_path / "runs",
        supplement=False,
        jank_thresholds_ms={"light": 100.0, "heavy": 500.0, "severe": 800.0},
    )

    summary = report["metricsSummary"]
    # 20/150/400/900：light=150+400 两例；900 超 800 只记 severe（互斥取最高档）
    assert summary["app_jank_light_count"]["max"] == 2.0
    assert summary["app_jank_heavy_count"]["max"] == 0.0
    assert summary["app_jank_severe_count"]["max"] == 1.0
    assert report["jank"]["thresholdsMs"] == {"light": 100.0, "heavy": 500.0, "severe": 800.0}
