"""报告聚合骨架：每个 run 产出同内容 JSON（M3 补齐单文件 HTML + 三源同时间轴）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


def build_report(
    *,
    meta: dict[str, Any],
    record: dict[str, Any],
    artifacts: list[str],
    metrics_summary: dict[str, Any] | None = None,
    integrity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把采集元数据、会话记录与产物清单聚合为报告字典。"""
    return {
        "schemaVersion": SCHEMA_VERSION,
        "runId": meta["runId"],
        "target": meta["target"],
        "items": meta["items"],
        "serial": meta["serial"],
        "hostRunId": meta.get("hostRunId"),
        "startedAt": meta["startedAt"],
        "stoppedAt": meta["stoppedAt"],
        "durationS": meta["durationS"],
        "soloSessionId": record.get("sessionId"),
        "marks": record.get("marks", []),
        "artifacts": artifacts,
        "metricsSummary": metrics_summary,
        "integrity": integrity,
        # M3 填实：SoloPi / pc_supplement / events 三源同时间轴聚合
    }


def write_report(run_dir: str | Path, report: dict[str, Any]) -> Path:
    path = Path(run_dir) / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
