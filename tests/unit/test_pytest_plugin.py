"""pytest 钩子 --mobile-perf 的单元测试：pytester 内嵌运行，不碰 adb 与真机。

假 kit 经 conftest 覆盖 mobile_perf_kit 注入（与文档声明给宿主的覆盖点
一致），端到端验证 fixture 接缝：opt-in 门控、自动 finalize、judge 结论
进报告 sections。runs_dir/store/lease 都落在 pytester 临时目录。
"""


# 内嵌 conftest：覆盖 mobile_perf_kit 为假后端 kit（不碰 adb/设备）
_INNER_CONFTEST = """
import time
from types import SimpleNamespace

import pytest
from mobile_perf import PerfKit
from mobile_perf.gate import FileLockGate
from mobile_perf.keeper import CheckResult
from mobile_perf.store import PerfStore

class FakeBackend:
    def __init__(self):
        self.started = []

    def start(self, target, items):
        self.started.append((target, items))
        return "sid-fake-1"

    def stop(self, session_id, output_dir):
        from pathlib import Path
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        rows = ["RecordTime,PSS-main-18390(MB),extra,SimpleTime"]
        rows += [f"{1792895595000 + i},{42.0 + i},,{i}" for i in range(3)]
        Path(output_dir).joinpath("performance.csv").write_text(
            "\\n".join(rows) + "\\n", encoding="gb18030"
        )
        return {
            "success": True,
            "state": "stopped",
            "sessionId": session_id,
            "artifacts": {"performance": {"localDir": str(output_dir)}},
        }

def keeper_call():
    def call(args, timeout_s=15.0):
        if args == ["devices"]:
            stdout = "List of devices attached\\nFAKESERIAL\\tdevice\\n"
        elif "accessibility" in args:
            stdout = (
                "ACCESSIBILITY MANAGER SERVICES (dumpsys accessibility)\\n"
                "  Enabled services:\\n"
                "    Service[com.alipay.hulu/com.alipay.hulu.shared.event."
                "accessibility.AccessibilityServiceImpl]\\n"
                "  Bound services:\\n"
                "    Service[{com.alipay.hulu/com.alipay.hulu.shared.event."
                "accessibility.AccessibilityServiceImpl}]\\n"
            )
        elif "battery" in args:
            stdout = "AC powered: false\\nUSB powered: false\\nstatus: 4\\n"
        elif "activity" in args:
            stdout = (
                "  mResumedActivity: ActivityRecord{1 u0 "
                "tv.danmaku.bili/.ui.MainActivity t42}\\n"
            )
        elif "date" in args:
            stdout = str(int(time.time()))
        else:
            stdout = ""
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    return call

@pytest.fixture(scope="session")
def mobile_perf_kit(request, tmp_path_factory):
    base = tmp_path_factory.mktemp("perf-kit")
    (base / "adb.exe").write_bytes(b"x")
    ticks = iter(float(i) for i in range(10_000))
    kit = PerfKit(
        adb_path=str(base / "adb.exe"),
        serial="FAKESERIAL",
        runs_dir=base / "runs",
        backend=FakeBackend(),
        store=PerfStore(base / "data" / "mobile_perf.db"),
        clock=lambda: next(ticks),
        sleep=lambda seconds: None,
        gate=FileLockGate(base / "leases"),
        foreground_restore=False,
        device_call=keeper_call(),
        doctor_fn=lambda: SimpleNamespace(
            success=True, data={"failedChecks": [], "missingPermissions": []}
        ),
        dialog_probe=lambda: CheckResult("dialog_probe", "skipped", {"reason": "stub"}),
    )
    yield kit
    kit.store.close()  # 归还 SQLite 连接：Windows 下不关闭会锁 db，毒化共享 basetemp
"""

_INNER_TEST = """
def test_home_scroll(perf):
    with perf.session("tv.danmaku.bili", items=["CPU"], host_run_id="run-hook-1") as scope:
        scope.mark("swipe_done")
    assert scope.report["marks"] == [{"label": "swipe_done", "elapsedS": 1.0}]
"""


class SectionRecorder:
    """随 runpytest 注入的记录插件：汇总各用例 report.sections。"""

    def __init__(self):
        self.sections: dict[str, str] = {}

    def pytest_runtest_logreport(self, report):
        for title, text in report.sections:
            self.sections[title] = self.sections.get(title, "") + "\n" + text


def _collected_sections(recorder: SectionRecorder) -> dict[str, str]:
    return recorder.sections


def test_perf_fixture_requires_opt_in(pytester):
    pytester.makepyfile("def test_inner(perf):\n    pass\n")
    result = pytester.runpytest()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*--mobile-perf*"])


def test_perf_session_finalizes_and_writes_section(pytester):
    pytester.makeconftest(_INNER_CONFTEST)
    pytester.makepyfile(_INNER_TEST)
    recorder = SectionRecorder()
    result = pytester.runpytest(plugins=[recorder])
    result.assert_outcomes(passed=1)

    sections = _collected_sections(recorder)
    assert "mobile-perf" in sections  # 结论进 report.sections（JUnit/HTML 展示源）
    assert "runId: run-hook-1" in sections["mobile-perf"]


def test_perf_session_judges_against_saved_baseline(pytester, tmp_path):
    pytester.makeconftest(_INNER_CONFTEST)
    pytester.makepyfile(
        """
        def test_judged(perf):
            kit = perf.kit
            kit.store.record_baseline(
                name="bili-v1", target_pkg="tv.danmaku.bili",
                device_profile=None, metric_key="mem_pss_mb",
                value=43.0, source_run_id="run-baseline",
            )
            with perf.session(
                "tv.danmaku.bili",
                items=["CPU"],
                baseline="bili-v1",
                thresholds={"mem_pss_mb": ("lt", 100, "fail")},
                min_samples=1,
            ) as scope:
                pass
            assert scope.report["runId"]
            assert perf.results[0].verdict.verdict == "passed"
            assert perf.results[0].verdict.checks[0]["metricKey"] == "mem_pss_mb"
        """
    )
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)


def test_perf_session_judged_failure_lands_in_section(pytester):
    """阈值判 failed 时结论照实进 sections；是否拉低用例结论由宿主决定。"""
    pytester.makeconftest(_INNER_CONFTEST)
    pytester.makepyfile(
        """
        def test_judged(perf):
            kit = perf.kit
            kit.store.record_baseline(
                name="bili-v1", target_pkg="tv.danmaku.bili",
                device_profile=None, metric_key="mem_pss_mb",
                value=43.0, source_run_id="run-baseline",
            )
            with perf.session(
                "tv.danmaku.bili",
                items=["CPU"],
                baseline="bili-v1",
                thresholds={"mem_pss_mb": ("lt", 10, "fail")},
                min_samples=1,
            ) as scope:
                pass
            assert perf.results[0].verdict.verdict == "failed"
        """
    )
    recorder = SectionRecorder()
    result = pytester.runpytest(plugins=[recorder])
    result.assert_outcomes(passed=1)
    sections = _collected_sections(recorder)
    assert "verdict: failed" in sections["mobile-perf"]


def test_perf_session_runs_ensure_ready_before_collect(pytester):
    """A2：plugin 路径巡检先行——keeper 结论（设备状态）随用例入库。"""
    pytester.makeconftest(_INNER_CONFTEST)
    pytester.makepyfile(
        """
        def test_ready(perf):
            with perf.session("tv.danmaku.bili", items=["CPU"]) as scope:
                pass
            (row,) = perf.kit.store.fetch_devices()
            assert row["serial"] == "FAKESERIAL"
            assert row["status"] == "online"
        """
    )
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)


_MARKER_TEST = """
import pytest

def _save(kit, name):
    kit.store.record_baseline(
        name=name, target_pkg="tv.danmaku.bili", device_profile=None,
        metric_key="mem_pss_mb", value=43.0, source_run_id="run-baseline",
    )

@pytest.mark.mobile_perf_baseline("bili-marked")
def test_marked(perf):
    _save(perf.kit, "bili-marked")
    with perf.session(
        "tv.danmaku.bili", items=["CPU"],
        thresholds={"mem_pss_mb": ("lt", 100, "fail")}, min_samples=1,
    ) as scope:
        pass
    assert perf.results[0].verdict.baseline == "bili-marked"

def test_global(perf):
    _save(perf.kit, "bili-global")
    with perf.session(
        "tv.danmaku.bili", items=["CPU"],
        thresholds={"mem_pss_mb": ("lt", 100, "fail")}, min_samples=1,
    ) as scope:
        pass
    assert perf.results[0].verdict.baseline == "bili-global"
"""


def test_case_level_baseline_marker_wins_over_global(pytester):
    """A6：@pytest.mark.mobile_perf_baseline 用例级优先于全局 --mobile-perf-baseline。"""
    pytester.makeconftest(_INNER_CONFTEST)
    pytester.makepyfile(_MARKER_TEST)
    result = pytester.runpytest("--mobile-perf-baseline", "bili-global")
    result.assert_outcomes(passed=2)


def test_session_baseline_param_wins_over_marker(pytester):
    """优先级：session(baseline=) 显式参数 > 用例标记 > 全局选项。"""
    pytester.makeconftest(_INNER_CONFTEST)
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.mobile_perf_baseline("bili-marked")
        def test_param_wins(perf):
            perf.kit.store.record_baseline(
                name="bili-param", target_pkg="tv.danmaku.bili", device_profile=None,
                metric_key="mem_pss_mb", value=43.0, source_run_id="run-baseline",
            )
            with perf.session(
                "tv.danmaku.bili", items=["CPU"], baseline="bili-param",
                thresholds={"mem_pss_mb": ("lt", 100, "fail")}, min_samples=1,
            ) as scope:
                pass
            assert perf.results[0].verdict.baseline == "bili-param"
        """
    )
    result = pytester.runpytest("--mobile-perf-baseline", "bili-global")
    result.assert_outcomes(passed=1)


def test_mobile_perf_baseline_marker_is_registered(pytester):
    """标记已注册：--strict-markers 下用例标记不再被判为未知标记。"""
    pytester.makeconftest(_INNER_CONFTEST)
    pytester.makepyfile(_MARKER_TEST)
    result = pytester.runpytest("--mobile-perf-baseline", "bili-global", "--strict-markers")
    result.assert_outcomes(passed=2)
