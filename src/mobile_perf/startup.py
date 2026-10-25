"""启动编排（ticket 12）：collect 窗口内完成冷/温启动并双路计时。

cold = force-stop 后 ``am start -W`` 拉起；warm = 先退后台（Home 键）再拉起。
计时双路互证：``am start -W`` 的 TotalTime 为入库主值，logcat ``Displayed``
（及目标 App 支持时的 ``Fully drawn``）作对照，偏差如实记录进事件 detail，
不强制一致。启动发生在 SoloPi 采集窗口内，使启动期资源曲线覆盖启动时刻。
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# am start -W 输出是 "Key: value" 行；时间字段单位恒为 ms
_AM_START_FIELDS = {
    "Status": "status",
    "LaunchState": "launch_state",
    "Activity": "activity",
    "ThisTime": "this_time_ms",
    "TotalTime": "total_time_ms",
    "WaitTime": "wait_time_ms",
    "ErrorType": "error_type",
    "Error": "error",
}
_NUMERIC_FIELDS = ("this_time_ms", "total_time_ms", "wait_time_ms")

_DURATION_RE = re.compile(r"^\+?(?:(\d+)s)?(\d+)ms")
# Displayed/Fully drawn 行："<tag>: <verb> <component>[: +1s23ms]"，真机在
# component 与时长间可能带 " for user 0" 中缀（(total) 后缀可选）
_LOG_LINE_RE = re.compile(
    r"(Displayed|Fully drawn)\s+(\S+?)/\S+?(?:\s+for\s+user\s+\d+)?:"
    r"\s*(\+[\dsms]+(\(total\))?)"
)
_DISPLAYED_VERB = "Displayed"
_FULLY_DRAWN_VERB = "Fully drawn"

KEYCODE_HOME = "KEYCODE_HOME"
# 退后台/force-stop 后的固定暂歇：等窗口动画与进程回收落定（可注入 sleep 测试）
SETTLE_S = 0.5


def parse_duration_ms(text: str | None) -> float | None:
    """解析 logcat 时长记法（``+487ms`` / ``+1s23ms`` / 尾缀 ``(total)``）；不合法返回 None。"""
    if not text:
        return None
    match = _DURATION_RE.match(text.strip())
    if match is None:
        return None
    seconds = int(match.group(1)) if match.group(1) else 0
    return seconds * 1000.0 + int(match.group(2))


def parse_am_start_w(stdout: str) -> dict[str, str | float | None]:
    """解析 ``am start -W`` 输出：状态/LaunchState/TotalTime 等字段，缺失为 None。"""
    parsed: dict[str, str | float | None] = {
        value: None for value in _AM_START_FIELDS.values()
    }
    for line in (stdout or "").splitlines():
        if ": " not in line:
            continue
        key, _, value = line.partition(": ")
        mapped = _AM_START_FIELDS.get(key.strip())
        if mapped is None:
            continue
        if mapped in _NUMERIC_FIELDS:
            try:
                parsed[mapped] = float(value.strip())
            except ValueError:
                parsed[mapped] = None
        else:
            parsed[mapped] = value.strip()
    return parsed


def _parse_log_ms_lines(stdout: str, verb: str, package: str) -> list[float]:
    values: list[float] = []
    for match in _LOG_LINE_RE.finditer(stdout or ""):
        if match.group(1) != verb or match.group(2) != package:
            continue
        ms = parse_duration_ms(match.group(3))
        if ms is not None:
            values.append(ms)
    return values


def parse_displayed_ms(stdout: str, package: str) -> float | None:
    """logcat 中目标包最后一行 ``Displayed`` 计时（ms）；无则 None。"""
    values = _parse_log_ms_lines(stdout, _DISPLAYED_VERB, package)
    return values[-1] if values else None


def parse_fully_drawn_ms(stdout: str, package: str) -> float | None:
    """logcat 中目标包最后一行 ``Fully drawn`` 计时（ms，App 调 reportFullyDrawn 才有）。"""
    values = _parse_log_ms_lines(stdout, _FULLY_DRAWN_VERB, package)
    return values[-1] if values else None


# ---------- 编排：collect 窗口内完成启动并双路计时 ----------

STARTUP_MODES = ("cold", "warm")

# logcat 旁路 dump：Displayed/Fully drawn 由 ActivityTaskManager（旧版本
# ActivityManager）输出，按 tag 过滤避免整缓冲拉回
_LOGCAT_ARGS = ["logcat", "-d", "ActivityTaskManager:I", "ActivityManager:I", "*:S"]


class ResolveError(RuntimeError):
    """无法从 cmd package resolve-activity 解析出目标包的启动 Activity。"""


def resolve_launcher_component(shell: Callable[..., Any], package: str) -> str:
    """解析目标包的 launcher Activity（`cmd package resolve-activity --brief`）。"""
    result = shell(["cmd", "package", "resolve-activity", "--brief", package])
    for line in reversed((getattr(result, "stdout", "") or "").splitlines()):
        component = line.strip()
        if "/" in component:
            return component
    raise ResolveError(
        f"无法解析包 {package} 的启动 Activity（resolve-activity 输出无组件行）"
    )


@dataclass(frozen=True)
class StartupResult:
    """一次启动编排的完整结论：am start -W 主值 + logcat 对照，失败如实带 error。

    ms 为入库主值：TotalTime 优先；warm 拉起若任务只是被带到前台（无
    TotalTime 或为 0，真机实测），回退 WaitTime 并以 ms_source 如实标注。
    """

    mode: str
    status: str | None = None
    launch_state: str | None = None
    total_time_ms: float | None = None
    wait_time_ms: float | None = None
    this_time_ms: float | None = None
    ms: float | None = None
    ms_source: str | None = None
    displayed_ms: float | None = None
    fully_drawn_ms: float | None = None
    component: str | None = None
    error: str | None = None
    ts: str = ""
    elapsed_s: float | None = None


def run_startup(
    *,
    shell: Callable[..., Any],
    target: str,
    mode: str,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], str] = lambda: datetime.now().astimezone().isoformat(
        timespec="seconds"
    ),
    elapsed: Callable[[], float] | None = None,
) -> StartupResult:
    """在采集窗口内执行启动编排并返回双路计时结论（shell 为注入的设备命令通道）。

    cold：force-stop → 暂歇 → 拉起；warm：Home 退后台 → 暂歇 → 拉起。
    启动计时取 am start -W TotalTime；logcat Displayed / Fully drawn 作对照
    （仅在启动成功时采集对照路）。任何失败都不抛异常，error 随结论返回，
    由编排层决定入库与降级形态。elapsed 注入会话时钟回调，记录 am start
    返回时刻在统一时间轴上的秒数。
    """
    if mode not in STARTUP_MODES:
        raise ValueError(f"start_mode 只支持 {', '.join(STARTUP_MODES)} 或 None，收到 {mode!r}")
    component: str | None = None
    try:
        if mode == "cold":
            shell(["am", "force-stop", target])
            sleep(SETTLE_S)
        else:
            shell(["input", "keyevent", KEYCODE_HOME])
            sleep(SETTLE_S)
        component = resolve_launcher_component(shell, target)
        result = shell(["am", "start", "-W", "-n", component])
        elapsed_s = elapsed() if elapsed is not None else None
        parsed = parse_am_start_w(getattr(result, "stdout", "") or "")
    except Exception as exc:  # noqa: BLE001 - 启动失败如实降级，不抛出
        return StartupResult(mode=mode, component=component, error=str(exc), ts=now())
    returncode = getattr(result, "returncode", 1)
    status = parsed["status"]
    total = parsed["total_time_ms"]
    wait = parsed["wait_time_ms"]
    # 入库主值：TotalTime 优先，无 TotalTime 或为 0 时回退 WaitTime
    #（warm 拉起任务仅被带到前台的真机实测形态），ms_source 如实标注
    ms, ms_source = total, "total_time"
    if ms is None or ms <= 0:
        ms, ms_source = (wait, "wait_time") if wait is not None and wait > 0 else (None, None)
    error: str | None = None
    if returncode != 0:
        error = f"am start 命令失败（returncode={returncode}）"
    elif status != "ok" or ms is None:
        error = (
            f"am start 未成功启动: "
            f"{parsed.get('error_type') or parsed.get('error') or status or '无计时字段'}"
        )
    displayed_ms = fully_drawn_ms = None
    if error is None:
        logcat_out = getattr(shell(_LOGCAT_ARGS), "stdout", "") or ""
        displayed_ms = parse_displayed_ms(logcat_out, target)
        fully_drawn_ms = parse_fully_drawn_ms(logcat_out, target)
    return StartupResult(
        mode=mode,
        status=status,
        launch_state=parsed["launch_state"],
        total_time_ms=total,
        wait_time_ms=wait,
        this_time_ms=parsed["this_time_ms"],
        ms=ms,
        ms_source=ms_source,
        displayed_ms=displayed_ms,
        fully_drawn_ms=fully_drawn_ms,
        component=component,
        error=error,
        ts=now(),
        elapsed_s=elapsed_s,
    )
