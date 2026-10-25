"""归档原语：runId、run 目录、产物校验、元数据落盘。

产物落盘布局：runs/<runId>/artifacts（Harness pull 的原始产物）、
meta.json（采集元数据）、report.json（报告聚合，report.py 生成）。
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime
from pathlib import Path


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


def write_meta(run_dir: str | Path, meta: dict) -> Path:
    path = Path(run_dir) / "meta.json"
    path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path
