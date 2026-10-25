"""pytest 钩子 ``--mobile-perf``（对称 moblie-auto 的 ``--mobile-evidence``，设计方案 §6.5）。

opt-in：不开 ``--mobile-perf`` 时 ``perf`` fixture 显式报错，零行为。开启后
``perf.session(target, items=...)`` 给出采集作用域（块内 ``mark()`` 打标），
退出自动 finalize；指定 baseline（含阈值）时自动 judge，结论经
``report.sections``（标题 mobile-perf）进 JUnit/HTML 等报告展示。阈值判
failed 不改变用例结论——是否拉低由宿主断言 ``perf.results`` 决定。

serial/items/基线/阈值经 CLI 或 ini 传入（CLI 优先）。kit 构造集中在
``mobile_perf_kit`` fixture：宿主 conftest 同名覆盖即可整体替换（注入宿主
adb 路径、DeviceGate 适配、device_profile、共享 store 等）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

_SECTION_TITLE = "mobile-perf"

_DEFAULT_ITEMS = "CPU,Memory"


def pytest_addoption(parser):
    group = parser.getgroup("mobile-perf")
    group.addoption(
        "--mobile-perf",
        action="store_true",
        default=False,
        help="启用 perf fixture：用例内 perf.session(...) 声明性能采集作用域(mobile-perf)",
    )
    group.addoption("--mobile-perf-serial", action="store", default=None, help="设备 serial")
    group.addoption(
        "--mobile-perf-items",
        action="store",
        default=None,
        metavar="ITEMS",
        help="默认采集项(逗号分隔；session(items=...) 优先)",
    )
    group.addoption("--mobile-perf-runs-dir", action="store", default=None, help="run 归档目录")
    group.addoption("--mobile-perf-db", action="store", default=None, help="SQLite store 路径")
    group.addoption(
        "--mobile-perf-baseline",
        action="store",
        default=None,
        help="session 未显式指定时的判读基线名",
    )
    group.addoption(
        "--mobile-perf-threshold",
        action="append",
        default=None,
        metavar="METRIC:OP:LIMIT:LEVEL",
        help="session 未显式指定时的阈值(op: lt/le/gt/ge, level: fail/warn)，可重复",
    )
    parser.addini("mobile_perf_serial", "设备 serial(--mobile-perf-serial 优先)", default=None)
    parser.addini("mobile_perf_items", "默认采集项(--mobile-perf-items 优先)", default=None)
    parser.addini(
        "mobile_perf_thresholds",
        "默认阈值,逗号分隔 METRIC:OP:LIMIT:LEVEL(--mobile-perf-threshold 优先)",
        default=None,
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "mobile_perf_baseline(name): 用例级判读基线"
        "（优先于 --mobile-perf-baseline，session(baseline=) 显式参数再优先于标记）",
    )


def _parse_threshold(token: str) -> tuple[str, tuple[str, float, str]]:
    metric, op, limit, level = token.strip().split(":")
    return metric, (op, float(limit), level)


def _default_thresholds(config) -> dict[str, tuple[str, float, str]]:
    tokens = config.getoption("--mobile-perf-threshold")
    if not tokens:
        raw = config.getini("mobile_perf_thresholds") or ""
        tokens = [t for t in raw.split(",") if t.strip()] if raw else []
    thresholds: dict[str, tuple[str, float, str]] = {}
    for token in tokens:
        metric, spec = _parse_threshold(token)
        thresholds[metric] = spec
    return thresholds


def _default_items(config) -> list[str]:
    raw = config.getoption("--mobile-perf-items") or config.getini("mobile_perf_items")
    raw = raw or _DEFAULT_ITEMS
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass
class PerfResult:
    """一次 perf.session 的收口结果：报告 + 可选 verdict，供 sections 与宿主断言。"""

    run_id: str
    target: str
    report: dict[str, Any] | None = None
    verdict: Any | None = None
    error: str | None = None
    judge_error: str | None = None
    marks: list[dict[str, Any]] = field(default_factory=list)

    def section_text(self) -> str:
        lines = [f"runId: {self.run_id}", f"target: {self.target}"]
        if self.report is not None:
            lines.append(f"durationS: {self.report.get('durationS')}")
        if self.verdict is not None:
            v = self.verdict
            head = f"verdict: {v.verdict} (baseline {v.baseline})"
            if v.reason:
                head += f" — {v.reason}"
            lines.append(head)
            for check in v.checks:
                lines.append(
                    f"  {check['metricKey']}: median={check['median']} "
                    f"{check['op']} {check['threshold']} -> "
                    f"{'ok' if check['ok'] else check['level']}"
                )
        if self.judge_error is not None:
            lines.append(f"judgeError: {self.judge_error}")
        if self.error is not None:
            lines.append(f"error: {self.error}")
        return "\n".join(lines)


class _PerfSession:
    """perf.session(...) 返回的上下文：进入开采集，退出 finalize + 可选 judge。"""

    def __init__(
        self,
        fixture: PerfFixture,
        target: str,
        items: list[str],
        judge_spec: Any | None,
        host_run_id: str | None,
    ):
        self._fixture = fixture
        self._target = target
        self._items = items
        self._judge_spec = judge_spec
        self._host_run_id = host_run_id
        self.scope: Any | None = None

    def __enter__(self):
        # 巡检先行（A2）：keeper 事件随本用例 run 入库，降级结论供标注使用；
        # 设备不可用（含 needs_attention）在此暴露，不进采集窗口
        self._fixture.kit.ensure_ready()
        self.scope = self._fixture.kit.collect(
            target=self._target,
            items=self._items,
            host_run_id=self._host_run_id,
        )
        self.scope.__enter__()
        return self.scope

    def __exit__(self, exc_type, exc, tb) -> bool:
        result = PerfResult(
            run_id=getattr(self.scope, "run_id", "?"), target=self._target
        )
        try:
            self.scope.__exit__(exc_type, exc, tb)  # 无条件 finalize（块内失败也收口）
            result.report = self.scope.report
            result.run_id = (result.report or {}).get("runId", result.run_id)
            result.marks = (result.report or {}).get("marks", [])
            if self._judge_spec is not None and result.report is not None:
                result.verdict = self._fixture.kit.judge(result.report, self._judge_spec)
        except Exception as exc:  # noqa: BLE001 - 采集失败也要留在 sections 里
            result.error = repr(exc)
        self._fixture.results.append(result)
        self._record_on_node(result)
        return False  # 块内异常原样传播，报告结论已记录

    def _record_on_node(self, result: PerfResult) -> None:
        node = self._fixture.request.node
        seen = getattr(node, "_mobile_perf_results", None)
        if seen is None:
            seen = []
            node._mobile_perf_results = seen
        seen.append(result)


class PerfFixture:
    """``perf`` fixture 门面：``.session(...)`` 声明采集作用域，results 留存结论。"""

    def __init__(self, kit: Any, request: pytest.FixtureRequest):
        self.kit = kit
        self.request = request
        self.results: list[PerfResult] = []

    def session(
        self,
        target: str,
        items: list[str] | None = None,
        *,
        baseline: str | None = None,
        thresholds: dict[str, tuple[str, float, str]] | None = None,
        min_samples: int | None = None,
        host_run_id: str | None = None,
    ) -> _PerfSession:
        from .judge import JudgeSpec

        config = self.request.config
        if baseline is None:
            # 用例级标记（A6）优先于全局选项；session(baseline=) 显式参数最优先
            marker = self.request.node.get_closest_marker("mobile_perf_baseline")
            if marker is not None and marker.args:
                baseline = marker.args[0]
        if baseline is None:
            baseline = config.getoption("--mobile-perf-baseline")
        if thresholds is None:
            thresholds = _default_thresholds(config) or None
        judge_spec = None
        if baseline is not None:
            judge_spec = JudgeSpec(
                baseline=baseline, thresholds=thresholds or {}, min_samples=min_samples
            )
        return _PerfSession(
            self,
            target,
            items if items is not None else _default_items(config),
            judge_spec,
            host_run_id,
        )


@pytest.fixture(scope="session")
def mobile_perf_kit(request: pytest.FixtureRequest):
    """组件 kit 的默认构造（宿主 conftest 同名覆盖即可整体替换）。"""
    if not request.config.getoption("--mobile-perf"):
        yield None
        return
    from .kit import PerfKit

    config = request.config
    serial = config.getoption("--mobile-perf-serial") or config.getini(
        "mobile_perf_serial"
    )
    kwargs: dict[str, Any] = {"serial": serial or None}
    if not kwargs["serial"]:
        # kit 本身允许 serial=None（只读门面），但 perf fixture 必然触设备，快速失败
        raise pytest.UsageError(
            "--mobile-perf 需要设备 serial（--mobile-perf-serial 或 ini "
            "mobile_perf_serial）；宿主也可 conftest 覆盖 mobile_perf_kit"
        )
    if config.getoption("--mobile-perf-runs-dir"):
        kwargs["runs_dir"] = config.getoption("--mobile-perf-runs-dir")
    if config.getoption("--mobile-perf-db"):
        kwargs["db_path"] = config.getoption("--mobile-perf-db")
    try:
        kit = PerfKit(**kwargs)
    except ValueError as exc:
        raise pytest.UsageError(f"--mobile-perf 配置无效:{exc}") from exc
    yield kit
    # 会话结束归还 SQLite 连接：Windows 下不关闭会锁住 db 文件
    store = getattr(kit, "_store", None)
    if store is not None:
        store.close()


@pytest.fixture
def perf(request: pytest.FixtureRequest, mobile_perf_kit):
    if mobile_perf_kit is None:
        pytest.fail(
            "perf 需要 --mobile-perf 开启(serial 经 --mobile-perf-serial 或 "
            "ini mobile_perf_serial 提供);宿主也可 conftest 覆盖 mobile_perf_kit",
            pytrace=False,
        )
    return PerfFixture(mobile_perf_kit, request)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when != "call":
        return
    results = getattr(item, "_mobile_perf_results", None)
    if not results:
        return
    report.sections.append(
        (_SECTION_TITLE, "\n\n".join(r.section_text() for r in results))
    )
