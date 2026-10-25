"""ADB 可执行文件解析：显式参数 > 环境变量（MOBILE_PERF_ADB > ADB）> 随包默认。

随包默认指本仓库 `SoloPi/platform-tools/adb.exe`（platform-tools 37.0.1）。
宿主换用其他 adb 版本时，通过显式参数传入并自行保证兼容。
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

BUNDLED_ADB_REL_PATH = Path("SoloPi") / "platform-tools" / "adb.exe"

Runner = Callable[..., object]


class AdbResolutionError(RuntimeError):
    """三级解析均未得到可用的 adb 可执行文件。"""


def default_repo_root() -> Path:
    """包默认所在的仓库根（src 布局下 src/mobile_perf 的上两级）。"""
    return Path(__file__).resolve().parents[2]


def bundled_adb_path(repo_root: str | os.PathLike[str] | None = None) -> Path:
    root = Path(repo_root) if repo_root is not None else default_repo_root()
    return root / BUNDLED_ADB_REL_PATH


def resolve_adb(
    explicit: str | os.PathLike[str] | None,
    env: dict[str, str] | None = None,
    repo_root: str | os.PathLike[str] | None = None,
) -> Path:
    """按 显式参数 > 环境变量 > 随包默认 的顺序解析 adb，找不到即抛错。"""
    environ = os.environ if env is None else env

    for source, path in (
        ("显式参数", explicit),
        ("环境变量 MOBILE_PERF_ADB", environ.get("MOBILE_PERF_ADB")),
        ("环境变量 ADB", environ.get("ADB")),
    ):
        if not path:
            continue
        candidate = Path(path)
        if not candidate.is_file():
            raise AdbResolutionError(f"{source}指向的 adb 不存在: {candidate}")
        return candidate

    bundled = bundled_adb_path(repo_root)
    if not bundled.is_file():
        raise AdbResolutionError(f"随包 adb 不存在: {bundled}")
    return bundled


def _adb_runner(cmd: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - 命令由组件自身组装
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        check=False,
    )


def list_devices(adb_path: str, runner: Runner | None = None, timeout_s: float = 15.0) -> list[str]:
    """解析 `adb devices`，返回处于 device 状态的 serial 列表。"""
    run = (runner or _adb_runner)([str(adb_path), "devices"], timeout_s)
    devices: list[str] = []
    for line in (run.stdout or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    return devices


def first_online_device(adb_path: str, runner: Runner | None = None) -> str | None:
    """唯一在线设备时返回其 serial；无设备返回 None；多台由调用方决定策略。"""
    devices = list_devices(adb_path, runner=runner)
    if len(devices) == 1:
        return devices[0]
    return None
