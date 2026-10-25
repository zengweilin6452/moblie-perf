"""启动编排（ticket 12）的单元测试：解析纯函数、cold/warm 编排决策、字典登记、judge 单样本。

启动动作发生在 SoloPi 采集窗口内（kit.collect start_mode），解析层只依赖
命令输出文本，编排层经由注入的设备通道验证命令序列，不碰真机。
"""

import json

import pytest

from mobile_perf import startup

COLD_AM_START_OUTPUT = (
    "Starting: Intent { act=android.intent.action.MAIN "
    "cat=[android.intent.category.LAUNCHER] cmp=com.sinosoft.trainapp/.MainActivity }\n"
    "Status: ok\n"
    "LaunchState: COLD\n"
    "Activity: com.sinosoft.trainapp/.MainActivity\n"
    "TotalTime: 487\n"
    "WaitTime: 502\n"
    "CompleteTime: 502\n"
)


def test_parse_am_start_w_extracts_total_wait_and_launch_state():
    parsed = startup.parse_am_start_w(COLD_AM_START_OUTPUT)

    assert parsed["status"] == "ok"
    assert parsed["launch_state"] == "COLD"
    assert parsed["total_time_ms"] == 487.0
    assert parsed["wait_time_ms"] == 502.0


def test_parse_am_start_w_legacy_output_without_launch_state():
    parsed = startup.parse_am_start_w(
        "Status: ok\nActivity: com.foo/.Main\nThisTime: 120\nTotalTime: 356\nWaitTime: 360\n"
    )

    assert parsed["status"] == "ok"
    assert parsed["launch_state"] is None
    assert parsed["this_time_ms"] == 120.0
    assert parsed["total_time_ms"] == 356.0


def test_parse_am_start_w_error_output_has_no_times():
    parsed = startup.parse_am_start_w(
        "Starting: Intent { cmp=com.foo/.Missing }\n"
        "Error: Activity class com.foo/.Missing does not exist.\n"
    )

    assert parsed["status"] is None
    assert parsed["total_time_ms"] is None


def test_parse_duration_ms_handles_seconds_and_total_suffix():
    assert startup.parse_duration_ms("+487ms") == 487.0
    assert startup.parse_duration_ms("+1s23ms") == 1023.0
    assert startup.parse_duration_ms("+745ms(total)") == 745.0


def test_parse_displayed_ms_takes_last_matching_line_for_package():
    stdout = (
        "10-25 09:00:01.100  1234  5678 I ActivityTaskManager: Displayed "
        "com.other.app/.MainActivity: +999ms\n"
        "10-25 09:00:02.300  1234  5678 I ActivityTaskManager: Displayed "
        "com.sinosoft.trainapp/.MainActivity: +745ms\n"
        "10-25 09:00:03.000  1234  5678 I ActivityTaskManager: Displayed "
        "com.sinosoft.trainapp/.MainActivity: +1s12ms\n"
    )

    assert startup.parse_displayed_ms(stdout, "com.sinosoft.trainapp") == 1012.0


def test_parse_displayed_ms_handles_for_user_infix_from_real_device():
    """真机实测形态：component 与时长之间带 " for user 0" 中缀。"""
    stdout = (
        "10-25 18:59:06.106  2542  2666 I ActivityTaskManager: Displayed "
        "com.sinosoft.trainapp/.MainActivity for user 0: +346ms\n"
    )

    assert startup.parse_displayed_ms(stdout, "com.sinosoft.trainapp") == 346.0


def test_parse_displayed_ms_returns_none_when_package_absent():
    assert startup.parse_displayed_ms("Displayed com.other/.Main: +10ms\n", "com.foo") is None
    assert startup.parse_displayed_ms("", "com.foo") is None


def test_parse_fully_drawn_ms_takes_last_matching_line():
    stdout = (
        "10-25 09:00:04.500  1234  5678 I ActivityTaskManager: Fully drawn "
        "com.sinosoft.trainapp/.MainActivity: +2s130ms\n"
    )

    assert startup.parse_fully_drawn_ms(stdout, "com.sinosoft.trainapp") == 2130.0


def test_parse_fully_drawn_ms_returns_none_when_absent():
    assert startup.parse_fully_drawn_ms("nothing here", "com.foo") is None


# ---- 编排决策：cold/warm 命令序列与双路采集 ----

WARM_AM_START_OUTPUT = (
    "Status: ok\nLaunchState: WARM\nActivity: com.sinosoft.trainapp/.MainActivity\n"
    "TotalTime: 96\nWaitTime: 130\n"
)

LOGCAT_DISPLAYED = (
    "10-25 09:00:02.300  1234  5678 I ActivityTaskManager: Displayed "
    "com.sinosoft.trainapp/.MainActivity: +501ms\n"
)

RESOLVE_OUTPUT = (
    "priority=0 preferredOrder=0 match=0x108000 specific=0x108000 Intent:\n"
    "  com.sinosoft.trainapp/.MainActivity\n"
)


class RecordingShell:
    """设备通道替身：记录命令，按命令形态返回脚本化输出。"""

    def __init__(
        self,
        *,
        am_start_output=COLD_AM_START_OUTPUT,
        logcat_output=LOGCAT_DISPLAYED,
        resolve_output=RESOLVE_OUTPUT,
    ):
        self.am_start_output = am_start_output
        self.logcat_output = logcat_output
        self.resolve_output = resolve_output
        self.cmds: list[list[str]] = []

    def __call__(self, args, timeout_s=15.0):
        from types import SimpleNamespace

        self.cmds.append(list(args))
        stdout = ""
        if args[:2] == ["am", "start"]:
            stdout = self.am_start_output
        elif "logcat" in args:
            stdout = self.logcat_output
        elif args[:3] == ["cmd", "package", "resolve-activity"]:
            stdout = self.resolve_output
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def test_resolve_launcher_component_parses_brief_output():
    shell = RecordingShell()

    component = startup.resolve_launcher_component(shell, "com.sinosoft.trainapp")

    assert component == "com.sinosoft.trainapp/.MainActivity"
    assert shell.cmds == [
        ["cmd", "package", "resolve-activity", "--brief", "com.sinosoft.trainapp"]
    ]


def test_resolve_launcher_component_fails_without_component_line():
    shell = RecordingShell(resolve_output="priority=0 preferredOrder=0 match=0x0:\n")

    with pytest.raises(startup.ResolveError):
        startup.resolve_launcher_component(shell, "com.foo")


def test_run_startup_cold_force_stops_then_launches_and_collects_both_paths():
    shell = RecordingShell()

    result = startup.run_startup(
        shell=shell, target="com.sinosoft.trainapp", mode="cold", sleep=lambda _s: None
    )

    assert [c[:2] for c in shell.cmds] == [
        ["am", "force-stop"],
        ["cmd", "package"],
        ["am", "start"],
        ["logcat", "-d"],
    ]
    assert shell.cmds[0] == ["am", "force-stop", "com.sinosoft.trainapp"]
    assert shell.cmds[2] == [
        "am", "start", "-W", "-n", "com.sinosoft.trainapp/.MainActivity"
    ]
    assert result.status == "ok"
    assert result.launch_state == "COLD"
    assert result.total_time_ms == 487.0
    assert result.wait_time_ms == 502.0
    assert result.ms == 487.0
    assert result.ms_source == "total_time"
    assert result.displayed_ms == 501.0  # 双路对照：logcat Displayed
    assert result.error is None


def test_run_startup_warm_homes_first_without_force_stop():
    shell = RecordingShell(am_start_output=WARM_AM_START_OUTPUT)

    result = startup.run_startup(
        shell=shell, target="com.sinosoft.trainapp", mode="warm", sleep=lambda _s: None
    )

    assert shell.cmds[0] == ["input", "keyevent", "KEYCODE_HOME"]
    assert not any(cmd[:2] == ["am", "force-stop"] for cmd in shell.cmds)
    assert shell.cmds[1] == [
        "cmd", "package", "resolve-activity", "--brief", "com.sinosoft.trainapp"
    ]
    assert result.launch_state == "WARM"
    assert result.total_time_ms == 96.0


def test_run_startup_warm_without_total_time_falls_back_to_wait_time():
    """真机实测形态：warm 拉起任务仅被带到前台时无 TotalTime，回退 WaitTime。"""
    shell = RecordingShell(
        am_start_output=(
            "Starting: Intent { cmp=com.sinosoft.trainapp/.MainActivity }\n"
            "Warning: Activity not started, its current task has been brought to the front\n"
            "Status: ok\nLaunchState: UNKNOWN (0)\n"
            "Activity: com.sinosoft.trainapp/.MainActivity\nWaitTime: 18\n"
        ),
        logcat_output="",
    )

    result = startup.run_startup(
        shell=shell, target="com.sinosoft.trainapp", mode="warm", sleep=lambda _s: None
    )

    assert result.status == "ok"
    assert result.total_time_ms is None
    assert result.ms == 18.0
    assert result.ms_source == "wait_time"
    assert result.error is None
    assert result.displayed_ms is None  # 无重绘则 Displayed 缺席，如实为 None


def test_run_startup_unknown_mode_rejected():
    with pytest.raises(ValueError, match="start_mode"):
        startup.run_startup(shell=RecordingShell(), target="com.foo", mode="hot")


def test_run_startup_records_error_when_am_start_fails():
    shell = RecordingShell(
        am_start_output=(
            "Starting: Intent { cmp=com.foo/.Missing }\n"
            "Error: Activity class com.foo/.Missing does not exist.\n"
        ),
        logcat_output="",
    )

    result = startup.run_startup(
        shell=shell, target="com.foo", mode="cold", sleep=lambda _s: None
    )

    assert result.status is None
    assert result.total_time_ms is None
    assert result.displayed_ms is None
    assert "does not exist" in result.error


def test_run_startup_displayed_unavailable_is_recorded_not_fatal():
    shell = RecordingShell(logcat_output="")

    result = startup.run_startup(
        shell=shell, target="com.sinosoft.trainapp", mode="cold", sleep=lambda _s: None
    )

    assert result.total_time_ms == 487.0
    assert result.displayed_ms is None


# ---- 字典登记（source=pc_orchestration）与单值指标聚合 ----

def test_startup_metrics_registered_in_dictionary():
    from mobile_perf import metrics as metrics_module

    cold = metrics_module.METRICS["app_cold_start_ms"]
    warm = metrics_module.METRICS["app_warm_start_ms"]

    for spec, zh, en in (
        (cold, "冷启动耗时", "Cold start time"),
        (warm, "温启动耗时", "Warm start time"),
    ):
        assert spec.unit == "ms"
        assert spec.source == "pc_orchestration"
        assert spec.label_zh == zh
        assert spec.label_en == en
        assert spec.solo_columns == ()  # 非 SoloPi 列，来源为编排层


def test_single_value_aggregate_fills_all_fields_with_value():
    from mobile_perf import metrics as metrics_module

    row = metrics_module.aggregate_row("app_cold_start_ms", [731.0])

    assert row["metric_key"] == "app_cold_start_ms"
    assert row["unit"] == "ms"
    assert row["source"] == "pc_orchestration"
    assert row["min"] == row["median"] == row["mean"] == row["max"] == row["p90"] == 731.0
    assert row["sample_count"] == 1


def test_charging_degradation_does_not_touch_startup_metrics():
    from mobile_perf import metrics as metrics_module

    keys = metrics_module.degraded_metric_keys(("Battery", "Temperature"))
    assert "app_cold_start_ms" not in keys
    assert "app_warm_start_ms" not in keys

    summary = {"app_cold_start_ms": {"median": 731.0}}
    metrics_module.mark_not_tested(summary, ("Battery", "Temperature"))
    assert "notTested" not in summary["app_cold_start_ms"]


# ---- judge 零改动：单样本阈值经 min_samples 覆盖直接可用 ----

def _startup_report(median=731.0):
    from mobile_perf import metrics as metrics_module

    row = metrics_module.aggregate_row("app_cold_start_ms", [median])
    return {
        "runId": "run-startup-judge",
        "startedAt": "2026-10-25T09:00:00",
        "stoppedAt": "2026-10-25T09:00:30",
        "deviceProfile": None,
        "metricsSummary": {
            "app_cold_start_ms": {
                "unit": row["unit"],
                "median": row["median"],
                "sampleCount": row["sample_count"],
            }
        },
    }


def test_judge_threshold_works_on_single_sample_with_min_samples_override():
    from mobile_perf.judge import JudgeSpec, judge

    spec = JudgeSpec(
        baseline="trainapp-v1",
        min_samples=1,
        baseline_data={"app_cold_start_ms": {"value": 900.0}},
        thresholds={"app_cold_start_ms": ("lt", 800.0, "fail")},
    )

    assert judge(_startup_report(731.0), spec).verdict == "passed"
    verdict = judge(_startup_report(950.0), spec)
    assert verdict.verdict == "failed"
    assert verdict.checks[0]["median"] == 950.0
    assert verdict.checks[0]["threshold"] == 800.0


def test_judge_default_min_samples_keeps_single_sample_not_tested():
    from mobile_perf.judge import VERDICT_NOT_TESTED, JudgeSpec, judge

    spec = JudgeSpec(
        baseline="trainapp-v1",
        baseline_data={"app_cold_start_ms": {"value": 900.0}},
        thresholds={"app_cold_start_ms": ("lt", 800.0, "fail")},
    )

    verdict = judge(_startup_report(731.0), spec)
    assert verdict.verdict == VERDICT_NOT_TESTED


# ---- 采集会话的 elapsed 读数（统一时间轴） ----

def test_collect_session_elapsed_s_after_start():
    from mobile_perf.session import CollectSession

    ticks = iter([0.0, 1.5, 2.75])
    session = CollectSession(
        target="com.foo",
        items=["CPU"],
        serial="s",
        start=lambda: "sid",
        stop=lambda sid, out: {},
        clock=lambda: next(ticks),
    )
    assert session.elapsed_s() is None  # 未开始
    session.start()
    assert session.elapsed_s() == 1.5


# ---- kit.collect 集成：start_mode 编排进采集窗口（假后端 + 设备通道替身） ----

def make_startup_device_call(
    *,
    am_start_output=COLD_AM_START_OUTPUT,
    logcat_output=LOGCAT_DISPLAYED,
    resolve_output=RESOLVE_OUTPUT,
):
    """巡检健康 + 启动命令脚本化的设备通道替身；返回 (call, recorded)。"""
    from tests.unit.test_kit import healthy_keeper_device_call

    base = healthy_keeper_device_call()
    recorded: list[list[str]] = []

    def call(args, timeout_s=15.0):
        # 设备通道约定：args 带 "-s <serial> shell" 前缀；测试记录归一化后的命令
        cmd = list(args[3:]) if args[:3] == ["-s", "749622c9", "shell"] else list(args)
        recorded.append(cmd)
        if cmd[:2] == ["am", "start"]:
            stdout = am_start_output
        elif "logcat" in cmd:
            stdout = logcat_output
        elif cmd[:3] == ["cmd", "package", "resolve-activity"]:
            stdout = resolve_output
        else:
            return base(args, timeout_s)
        from types import SimpleNamespace

        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    return call, recorded


def startup_cmds(recorded):
    """从设备通道记录里筛出启动编排相关命令（归一化后）。"""
    return [
        cmd
        for cmd in recorded
        if cmd[:1] == ["am"]
        or cmd[:1] == ["input"]
        or cmd[:1] == ["logcat"]
        or cmd[:2] == ["cmd", "package"]
    ]


class TickingClock:
    """无限递增时钟：启动 elapsed 回调会额外消费 tick，固定双 tick 不够用。"""

    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 1.0
        return self.t


def make_startup_kit(tmp_path, device_call, backend=None, **kwargs):
    from tests.unit.test_kit import FakeHarnessBackend, make_kit

    kwargs.setdefault("clock", TickingClock())
    return make_kit(tmp_path, backend or FakeHarnessBackend(), device_call=device_call, **kwargs)


def test_collect_cold_start_records_metric_event_and_meta(tmp_path):
    from mobile_perf.store import PerfStore

    call, recorded = make_startup_device_call()
    store = PerfStore(tmp_path / "data" / "mobile_perf.db")
    kit = make_startup_kit(tmp_path, call, store=store)

    with kit.collect(
        target="com.sinosoft.trainapp",
        items=["CPU"],
        host_run_id="run-cold-1",
        runs_dir=tmp_path / "runs",
        supplement=False,
        start_mode="cold",
    ) as scope:
        pass

    report = scope.finalize()
    summary = report["metricsSummary"]["app_cold_start_ms"]
    assert summary["median"] == 487.0
    assert summary["sampleCount"] == 1
    assert summary["source"] == "pc_orchestration"

    events = [e for e in report["events"] if e["kind"] == "startup"]
    assert len(events) == 1
    detail = events[0]["detail"]
    assert detail["mode"] == "cold"
    assert detail["metricKey"] == "app_cold_start_ms"
    assert detail["ms"] == 487.0
    assert detail["launchState"] == "COLD"
    assert detail["displayedMs"] == 501.0  # 双路对照
    assert detail["deviationMs"] == 14.0
    assert detail["elapsedS"] == 1.0  # 会话起点后 1 个 tick

    assert any(cmd[:2] == ["am", "force-stop"] for cmd in recorded)
    assert any(cmd[:2] == ["am", "start"] for cmd in recorded)

    meta = json.loads((tmp_path / "runs" / "run-cold-1" / "meta.json").read_text(encoding="utf-8"))
    assert meta["startup"]["metricKey"] == "app_cold_start_ms"
    rows = {row["metric_key"]: row for row in store.fetch_metrics("run-cold-1")}
    assert rows["app_cold_start_ms"]["median"] == 487.0
    assert rows["app_cold_start_ms"]["sample_count"] == 1


def test_collect_warm_start_homes_first_and_records_warm_metric(tmp_path):
    call, recorded = make_startup_device_call(am_start_output=WARM_AM_START_OUTPUT)
    kit = make_startup_kit(tmp_path, call)

    with kit.collect(
        target="com.sinosoft.trainapp",
        items=["CPU"],
        host_run_id="run-warm-1",
        runs_dir=tmp_path / "runs",
        supplement=False,
        start_mode="warm",
    ) as scope:
        pass

    report = scope.finalize()
    summary = report["metricsSummary"]["app_warm_start_ms"]
    assert summary["median"] == 96.0
    warm_cmds = startup_cmds(recorded)
    assert warm_cmds[0] == ["input", "keyevent", "KEYCODE_HOME"]
    assert warm_cmds[1] == [
        "cmd", "package", "resolve-activity", "--brief", "com.sinosoft.trainapp"
    ]
    assert not any(cmd[:2] == ["am", "force-stop"] for cmd in warm_cmds)


def test_collect_start_mode_none_never_touches_startup_commands(tmp_path):
    call, recorded = make_startup_device_call()
    kit = make_startup_kit(tmp_path, call)

    with kit.collect(
        target="com.sinosoft.trainapp",
        items=["CPU"],
        host_run_id="run-plain-1",
        runs_dir=tmp_path / "runs",
        supplement=False,
    ) as scope:
        pass
    scope.finalize()

    assert not any(
        cmd[:2] in (["am", "start"], ["am", "force-stop"], ["input", "keyevent"])
        or cmd[:3] == ["cmd", "package", "resolve-activity"]
        for cmd in recorded
    )
    assert scope.startup is None


def test_collect_startup_failure_degrades_to_event_without_metric(tmp_path):
    call, _ = make_startup_device_call(
        am_start_output=(
            "Starting: Intent { cmp=com.sinosoft.trainapp/.Missing }\n"
            "Error: Activity class com.sinosoft.trainapp/.Missing does not exist.\n"
        ),
        logcat_output="",
    )
    kit = make_startup_kit(tmp_path, call)

    with kit.collect(
        target="com.sinosoft.trainapp",
        items=["CPU"],
        host_run_id="run-fail-1",
        runs_dir=tmp_path / "runs",
        supplement=False,
        start_mode="cold",
    ) as scope:
        pass

    report = scope.finalize()
    assert "app_cold_start_ms" not in report["metricsSummary"]
    events = [e for e in report["events"] if e["kind"] == "startup"]
    assert "does not exist" in events[0]["detail"]["error"]
    assert report["startup"]["error"]


def test_collect_rejects_unknown_start_mode(tmp_path):
    call, _ = make_startup_device_call()
    kit = make_startup_kit(tmp_path, call)

    with pytest.raises(ValueError, match="start_mode"):
        kit.collect(
            target="com.sinosoft.trainapp",
            items=["CPU"],
            runs_dir=tmp_path / "runs",
            start_mode="hot",
        )


def test_session_spec_accepts_start_mode_and_rejects_unknown():
    from mobile_perf import SessionSpec

    spec = SessionSpec(target="com.foo", items=["CPU"], start_mode="cold")
    assert spec.start_mode == "cold"

    with pytest.raises(ValueError, match="start_mode"):
        SessionSpec(target="com.foo", items=["CPU"], start_mode="hot")


def test_run_collect_passes_start_mode_through(tmp_path):
    call, recorded = make_startup_device_call()
    kit = make_startup_kit(tmp_path, call)

    report = kit.run_collect(
        target="com.sinosoft.trainapp",
        items=["CPU"],
        duration_s=5.0,
        host_run_id="run-rc-1",
        runs_dir=tmp_path / "runs",
        supplement=False,
        start_mode="cold",
    )

    assert report["metricsSummary"]["app_cold_start_ms"]["median"] == 487.0
    assert any(cmd[:2] == ["am", "force-stop"] for cmd in recorded)
