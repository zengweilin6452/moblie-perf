"""权限弹窗代点的降级备选（设计方案 §4 备选路径）：独立模块、默认关。

App 主路径——弹窗免重复开关（KEY_SUPPRESS_INFO_DIALOG，自编译 App 控制面，
票 06）——已闭环并由 keeper 巡检确认；本模块仅当宿主显式传入
``dialog_dismiss`` 探针时才挂上（``PerfKit(dialog_dismiss=...)``），作为
App 改造受阻时的兜底：检测 SoloPi 的 PermissionDialogActivity 抢前台
（dumpsys window 样本）→ ``adb shell input tap`` 点"我知道了"。点按坐标
分辨率相关、需一次人工标定后经 ``coords`` 传入，默认 None=不启用；代点
人工确认与 Harness 哲学略冲突，故不接入默认 collect 流程。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# SoloPi 告知型权限弹窗 Activity：dumpsys window 前台样本中出现即视为弹窗在前台
PERMISSION_DIALOG_ACTIVITY = "PermissionDialogActivity"

WINDOW_TIMEOUT_S = 10.0

DeviceCaller = Callable[..., Any]  # call_device(args, timeout_s) -> .stdout/.returncode


def is_permission_dialog_in_foreground(window_stdout: str | None) -> bool:
    """从 dumpsys window 输出判断权限弹窗是否在前台（纯文本匹配，不猜）。"""
    return PERMISSION_DIALOG_ACTIVITY in (window_stdout or "")


def decide_dismiss(window_stdout: str | None, coords: tuple[int, int] | None) -> dict[str, Any]:
    """纯决策：弹窗在前台且坐标已配置才 tap；coords 默认 None=不启用。"""
    present = is_permission_dialog_in_foreground(window_stdout)
    enabled = coords is not None
    return {"enabled": enabled, "present": present, "tapped": enabled and present}


def build_tap_args(serial: str, coords: tuple[int, int]) -> list[str]:
    """组装点"我知道了"的 adb 命令（坐标为人工标定值）。"""
    x, y = coords
    return ["-s", serial, "shell", "input", "tap", str(x), str(y)]


def make_dialog_dismiss_probe(
    call_device: DeviceCaller,
    serial: str,
    coords: tuple[int, int] | None,
) -> Callable[[], dict[str, Any]]:
    """构造可挂到 PerfKit(dialog_dismiss=...) 的探针：读前台 → 按决策点按。

    coords 为 None（默认）时直接返回未启用结论，不发起任何设备命令。
    """

    def probe() -> dict[str, Any]:
        if coords is None:
            return {"enabled": False, "present": None, "tapped": False}
        result = call_device(
            ["-s", serial, "shell", "dumpsys", "window"], WINDOW_TIMEOUT_S
        )
        verdict = decide_dismiss(getattr(result, "stdout", ""), coords)
        if verdict["tapped"]:
            call_device(build_tap_args(serial, coords), WINDOW_TIMEOUT_S)
            verdict["coords"] = list(coords)
        return verdict

    return probe
