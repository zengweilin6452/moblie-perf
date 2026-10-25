"""Harness 适配层：唯一知道 SoloPi Harness 的模块（ADR-0002）。

以子进程调用 Harness CLI（solopi_ai.py），以 JSON stdout 为契约，
绝不 import Harness 内部模块——Harness 在 import 时会向 sys.path 注入
自身目录并以顶级名导入兄弟模块（timing 等），进程边界是硬性要求。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adb import default_repo_root

HARNESS_ENTRY_REL_PATH = (
    Path("SoloPi")
    / "solopi-skill"
    / "scripts"
    / "harness"
    / "src"
    / "solopi_harness"
    / "solopi_ai.py"
)

PERF_START_TIMEOUT_S = 180.0
PERF_STOP_TIMEOUT_S = 300.0

Runner = Callable[..., Any]


class HarnessError(RuntimeError):
    """Harness 调用失败：非零退出且无可解析 JSON，或 stdout 不是 JSON。"""

    def __init__(self, message: str, *, exit_code: int, stdout: str, stderr: str):
        super().__init__(message)
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


@dataclass(frozen=True)
class HarnessResult:
    """一次 Harness CLI 调用的结果；data 为解析出的 JSON 载荷。"""

    success: bool
    exit_code: int
    data: dict[str, Any]
    stdout: str
    stderr: str


def harness_entry(repo_root: str | os.PathLike[str] | None = None) -> Path:
    root = Path(repo_root) if repo_root is not None else default_repo_root()
    return root / HARNESS_ENTRY_REL_PATH


def default_runner(
    cmd: list[str], timeout_s: float, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - 命令由组件自身组装，不接收外部 shell 片段
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        env=env,
        check=False,
    )


def subprocess_env() -> dict[str, str]:
    """子进程环境：透传宿主环境并强制 UTF-8，绝不注入 PYTHONPATH。"""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("PYTHONPATH", None)
    return env


def _parse_payload(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        raise ValueError("stdout 为空，未收到 JSON 载荷")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        last_line = text.splitlines()[-1].strip()
        payload = json.loads(last_line)  # --pretty 多行 JSON 场景兜底
    if not isinstance(payload, dict):
        raise ValueError("stdout JSON 载荷不是对象")
    return payload


def run_harness(
    args: list[str],
    *,
    adb_path: str | os.PathLike[str] | None = None,
    serial: str | None = None,
    entry: str | os.PathLike[str] | None = None,
    repo_root: str | os.PathLike[str] | None = None,
    timeout_s: float = 120.0,
    runner: Runner | None = None,
) -> HarnessResult:
    """以子进程调用 Harness CLI 并解析 JSON stdout 契约。"""
    entry_path = Path(entry) if entry is not None else harness_entry(repo_root)
    cmd = [sys.executable, str(entry_path)]
    if adb_path is not None:
        cmd += ["--adb", str(adb_path)]
    if serial is not None:
        cmd += ["--serial", serial]
    cmd += [str(arg) for arg in args]

    run = (runner or default_runner)(cmd, timeout_s, subprocess_env())
    stdout = run.stdout or ""
    stderr = run.stderr or ""
    exit_code = int(run.returncode)

    try:
        payload = _parse_payload(stdout)
    except (ValueError, json.JSONDecodeError) as exc:
        subcommand = args[0] if args else "(no-args)"
        raise HarnessError(
            f"Harness 调用 {subcommand} 未返回 JSON 契约(exit={exit_code}): {exc}",
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
        ) from exc

    success = payload.get("success")
    if not isinstance(success, bool):
        success = exit_code == 0
    return HarnessResult(
        success=success,
        exit_code=exit_code,
        data=payload,
        stdout=stdout,
        stderr=stderr,
    )


def perf_start(
    adb_path: str | os.PathLike[str],
    serial: str,
    target_package: str,
    items: list[str],
    repo_root: str | os.PathLike[str] | None = None,
    runner: Runner | None = None,
    entry: str | os.PathLike[str] | None = None,
    timeout_s: float = PERF_START_TIMEOUT_S,
) -> HarnessResult:
    """启动性能采集并等待端侧确认；成功时 data 含 sessionId。"""
    return run_harness(
        [
            "perf-start",
            "--target-package",
            target_package,
            "--items",
            ",".join(items),
        ],
        adb_path=adb_path,
        serial=serial,
        repo_root=repo_root,
        timeout_s=timeout_s,
        runner=runner,
        entry=entry,
    )


def perf_stop(
    adb_path: str | os.PathLike[str],
    serial: str,
    session_id: str,
    output_dir: str | os.PathLike[str],
    repo_root: str | os.PathLike[str] | None = None,
    runner: Runner | None = None,
    entry: str | os.PathLike[str] | None = None,
    timeout_s: float = PERF_STOP_TIMEOUT_S,
) -> HarnessResult:
    """停止采集；--output 触发 Harness 把设备端产物 pull 到全新本地目录。"""
    return run_harness(
        [
            "perf-stop",
            "--session-id",
            session_id,
            "--output",
            str(output_dir),
        ],
        adb_path=adb_path,
        serial=serial,
        repo_root=repo_root,
        timeout_s=timeout_s,
        runner=runner,
        entry=entry,
    )


def doctor(
    adb_path: str | os.PathLike[str],
    serial: str,
    repo_root: str | os.PathLike[str] | None = None,
    runner: Runner | None = None,
    entry: str | os.PathLike[str] | None = None,
) -> HarnessResult:
    """借用 Harness 的 doctor 做设备巡检（快照版本契约，升级只回归本模块测试）。"""
    return run_harness(
        ["doctor"],
        adb_path=adb_path,
        serial=serial,
        repo_root=repo_root,
        timeout_s=120.0,
        runner=runner,
        entry=entry,
    )


def perf_start_session_id(payload: dict[str, Any]) -> str | None:
    """从 perf-start 载荷提取 sessionId；未提供或缺席返回 None。"""
    if not payload.get("success"):
        raise HarnessError(
            f"perf-start 失败: {payload.get('error', '未知错误')}",
            exit_code=2,
            stdout="",
            stderr="",
        )
    session_id = payload.get("sessionId")
    if session_id is None:
        return None
    if not isinstance(session_id, str) or not session_id:
        raise HarnessError(
            "perf-start 载荷的 sessionId 无效",
            exit_code=4,
            stdout=json.dumps(payload, ensure_ascii=False),
            stderr="",
        )
    return session_id


def perf_stop_artifacts(payload: dict[str, Any]) -> dict[str, Any]:
    """从 perf-stop 载荷提取 pull 产物信息；未 pull 时返回空 dict。"""
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict):
        return {}
    performance = artifacts.get("performance")
    return performance if isinstance(performance, dict) else {}
