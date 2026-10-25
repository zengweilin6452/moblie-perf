"""归档原语：runId、run 目录、产物校验、聚合、行数校验与设备端清理、本地保留。

产物落盘布局：runs/<runId>/artifacts（Harness pull 的原始产物）、
meta.json（采集元数据）、report.json（报告聚合，report.py 生成）。
"""

from __future__ import annotations

import csv
import json
import logging
import re
import secrets
import shutil
import subprocess  # noqa: S404 - adb 命令由组件自身组装，不接收外部 shell 片段
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import metrics as metrics_module

logger = logging.getLogger(__name__)

# SoloPi 真机 CSV 为 GBK 家族编码（表头含中文）
CSV_ENCODING = "gb18030"

# 值列之外的信息列：列名映射时跳过
_NON_METRIC_COLUMNS = {"RecordTime", "extra", "SimpleTime"}

ADB_TIMEOUT_S = 30.0

# 本组件 run 目录名的固定形状：run-<日期>-<时间>-<4位随机>
RUN_DIR_PATTERN = re.compile(r"^run-\d{8}-\d{6}-[0-9a-f]{4}$")


class RunDirError(RuntimeError):
    """run 目录无法创建（已存在等）。"""


class ArtifactError(RuntimeError):
    """pull 产物校验失败（找不到非空性能 CSV 等）。"""


def new_run_id(now: datetime | None = None) -> str:
    """生成 run-<日期>-<时间>-<4位随机> 形式的宿主运行标识。"""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"run-{stamp}-{secrets.token_hex(2)}"


def create_run_dir(runs_dir: str | Path, run_id: str) -> Path:
    run_dir = Path(runs_dir) / run_id
    if run_dir.exists():
        raise RunDirError(f"run 目录已存在: {run_dir}")
    run_dir.mkdir(parents=True)
    return run_dir


def validate_artifacts(artifacts_dir: str | Path) -> list[Path]:
    """校验 pull 产物：至少存在一个非空 CSV；返回非空 CSV 列表。"""
    artifacts_dir = Path(artifacts_dir)
    csv_files = [
        path
        for path in sorted(artifacts_dir.rglob("*.csv"))
        if path.is_file() and path.stat().st_size > 0
    ]
    if not csv_files:
        raise ArtifactError(f"产物目录中没有非空性能 CSV: {artifacts_dir}")
    return csv_files


def _read_metric_values(csv_path: Path) -> dict[str, list[float]]:
    """把单个 CSV 按指标字典拆成 {canonical key: 值列表}；字典外列跳过。"""
    values: dict[str, list[float]] = {}
    with csv_path.open(encoding=CSV_ENCODING, newline="") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            return values
        column_specs: list[tuple[int, metrics_module.MetricSpec]] = []
        for index, name in enumerate(header):
            name = name.strip()
            if name in _NON_METRIC_COLUMNS:
                continue
            spec = metrics_module.column_to_metric(name)
            if spec is not None:
                column_specs.append((index, spec))
        for row in reader:
            for index, spec in column_specs:
                if index >= len(row):
                    continue
                try:
                    value = float(row[index])
                except ValueError:
                    continue
                values.setdefault(spec.key, []).append(value)
    return values


def aggregate_artifacts(artifacts_dir: str | Path) -> list[dict[str, Any]]:
    """对 pull 产物做聚合：每个 canonical key 一行 min/median/mean/max/p90。

    原始 CSV 永不入库；同一 canonical key 出现在多个文件时合并样本后聚合。
    没有任何可映射样本即视为产物异常。
    """
    csv_files = validate_artifacts(artifacts_dir)
    samples: dict[str, list[float]] = {}
    for path in csv_files:
        for key, values in _read_metric_values(path).items():
            samples.setdefault(key, []).extend(values)
    if not samples:
        raise ArtifactError(f"产物 CSV 中没有可映射到 canonical key 的样本: {artifacts_dir}")

    aggregates: list[dict[str, Any]] = []
    for key in sorted(samples):
        aggregates.append(metrics_module.aggregate_row(key, samples[key]))
    return aggregates


def write_meta(run_dir: str | Path, meta: dict) -> Path:
    path = Path(run_dir) / "meta.json"
    path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


# ---- pull 完整性：设备端行数校验与设备端会话目录清理 ----

# 设备端安全路径：仅允许 /sdcard/ 或 /storage/emulated/ 下的 performance-* 会话目录
_SAFE_REMOTE_SESSION = re.compile(
    r"^/(?:sdcard|storage/emulated)/[\S]*records/performance-[A-Za-z0-9][A-Za-z0-9._-]{0,115}$"
)

AdbRunner = Any  # callable(cmd: list[str], timeout_s: float) -> object(.stdout/.returncode)


def _adb(adb_path: str, serial: str, shell_args: list[str], runner: AdbRunner | None):
    run = (runner or _default_adb_runner)(
        [adb_path, "-s", serial, "shell", *shell_args], ADB_TIMEOUT_S
    )
    stdout = run.stdout or ""
    return int(getattr(run, "returncode", 1)), stdout


def _default_adb_runner(cmd: list[str], timeout_s: float):
    return subprocess.run(  # noqa: S603 - 命令由组件自身组装
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        check=False,
    )


def _local_line_count(path: Path) -> int:
    """本地行数按换行符计（与 wc -l 同口径）。"""
    return path.read_bytes().count(b"\n")


@dataclass
class IntegrityReport:
    """pull 完整性结果：设备端与本地逐文件行数对照。"""

    ok: bool
    checked_files: list[str] = field(default_factory=list)
    mismatches: list[dict[str, Any]] = field(default_factory=list)
    probe_error: str | None = None


def verify_pull_integrity(
    adb_path: str,
    serial: str,
    remote_dir: str,
    artifacts_dir: str | Path,
    runner: AdbRunner | None = None,
) -> IntegrityReport:
    """逐文件比较设备端 wc -l 与本地行数；任何不一致都判 pull 不完整。"""
    code, stdout = _adb(adb_path, serial, ["ls", remote_dir], runner)
    if code != 0:
        return IntegrityReport(ok=False, probe_error=f"adb ls 失败(exit={code}): {stdout.strip()}")

    # 部分设备 shell ls 会在行首附带额外 token，文件名取行尾字段
    device_files = sorted(
        line.split()[-1]
        for line in stdout.splitlines()
        if line.strip().lower().endswith(".csv")
    )
    artifacts_dir = Path(artifacts_dir)

    # 行数一次 glob 探完（设备端展开，避免逐文件传中文文件名）
    device_lines: dict[str, int] = {}
    probe_failed: list[str] = []
    if device_files:
        code, stdout = _adb(adb_path, serial, ["wc", "-l", f"{remote_dir}/*.csv"], runner)
        if code != 0:
            probe_failed = list(device_files)
            logger.warning("wc -l 探针失败: remote_dir=%s exit=%s", remote_dir, code)
        else:
            for line in stdout.splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) == 2 and parts[0].isdigit():
                    device_lines[parts[1].rsplit("/", 1)[-1]] = int(parts[0])
            probe_failed = [name for name in device_files if name not in device_lines]

    report = IntegrityReport(ok=True)
    for name in device_files:
        local_path = artifacts_dir / name
        if not local_path.is_file():
            report.mismatches.append({"file": name, "reason": "missing_local"})
            continue
        if name in probe_failed:
            report.mismatches.append({"file": name, "reason": "probe_failed"})
            continue
        local_lines = _local_line_count(local_path)
        if device_lines[name] != local_lines:
            report.mismatches.append(
                {
                    "file": name,
                    "reason": "row_count",
                    "deviceLines": device_lines[name],
                    "localLines": local_lines,
                }
            )
            continue
        report.checked_files.append(name)
    report.ok = not report.mismatches
    if not report.ok:
        logger.warning(
            "pull 完整性校验失败: remote_dir=%s mismatches=%s", remote_dir, report.mismatches
        )
    return report


def remove_device_session_dir(
    adb_path: str,
    serial: str,
    remote_dir: str,
    runner: AdbRunner | None = None,
) -> bool:
    """删除设备端该会话目录；路径不合法一律拒绝（只清本组件会话目录）。"""
    if not _SAFE_REMOTE_SESSION.match(remote_dir):
        logger.warning("拒绝删除设备端目录（不合法路径）: %s", remote_dir)
        return False
    code, stdout = _adb(adb_path, serial, ["rm", "-rf", remote_dir], runner)
    if code != 0:
        logger.warning("删除设备端会话目录失败: %s exit=%s %s", remote_dir, code, stdout.strip())
        return False
    logger.info("已删除设备端会话目录: %s", remote_dir)
    return True


def cleanup_after_pull(
    adb_path: str,
    serial: str,
    remote_dir: str | None,
    artifacts_dir: str | Path,
    runner: AdbRunner | None = None,
) -> tuple[IntegrityReport, bool]:
    """pull 后的完整编排：行数校验 → 通过才删设备端会话目录。

    返回 (完整性报告, 是否已删除设备端目录)。校验不过或无法确认时保留设备端。
    """
    report = verify_pull_integrity(
        adb_path, serial, remote_dir or "", artifacts_dir, runner=runner
    )
    if not report.ok or not remote_dir:
        return report, False
    return report, remove_device_session_dir(adb_path, serial, remote_dir, runner=runner)


# ---- 本地数据保留：最近 N run / M 天，仅清理本组件 run 目录 ----


def enforce_retention(
    runs_dir: str | Path,
    store: Any,
    *,
    keep_runs: int = 200,
    keep_days: float = 30.0,
    now: datetime | None = None,
) -> list[str]:
    """清理本地 runs/<runId> 超出保留策略的数据，并删除对应 DB 行。

    只碰形状为 run-<日期>-<时间>-<4位随机> 的目录（本组件产生），其他内容不动。
    返回被清理的 run_id 列表。keep_runs/keep_days 任一不满足即清理：
    超过 keep_days 的旧 run 与超出最新 keep_runs 个之外的旧 run。
    """
    if keep_runs <= 0 or keep_days <= 0:
        raise ValueError("keep_runs 与 keep_days 必须为正数")
    runs_dir = Path(runs_dir)
    if not runs_dir.is_dir():
        return []
    current = now or datetime.now()
    cutoff = current - timedelta(days=keep_days)

    run_dirs: list[tuple[datetime, str, Path]] = []
    for path in runs_dir.iterdir():
        if not path.is_dir() or not RUN_DIR_PATTERN.match(path.name):
            continue
        stamp = path.name[len("run-") : len("run-") + len("YYYYMMDD-HHMMSS")]
        try:
            started = datetime.strptime(stamp, "%Y%m%d-%H%M%S")
        except ValueError:
            continue
        run_dirs.append((started, path.name, path))
    run_dirs.sort(key=lambda item: item[0], reverse=True)  # 新 → 旧

    victims = [
        run_id
        for index, (started, run_id, path) in enumerate(run_dirs)
        if started < cutoff or index >= keep_runs
    ]
    for run_id in victims:
        path = runs_dir / run_id
        shutil.rmtree(path, ignore_errors=True)
        store.delete_run(run_id)
        logger.info("保留策略清理 run: %s", run_id)
    return victims
