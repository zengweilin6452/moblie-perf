"""内存泄漏趋势分析（ticket 13）：跨 run 的 median 随墙钟时间回归，纯函数。

口径归组件（设计方案 §5/§6.3/§10）：对同一 target_pkg + device_profile 的
历史 run，以 run 的 startedAt 为横轴（墙钟小时，线性插值到"天"）、该 run
的 ``mem_pss_mb`` 聚合 median 为纵轴，做普通最小二乘（OLS）线性回归，斜率
单位 MB/day（``mem_pss_slope_mb_per_day``）。阈值归宿主：``leak_verdict``
显式接收斜率阈值并产出三态结论。

样本与置信度口径（如实标注、不隐藏局限）：

- run 数低于下限（默认 3，可覆盖）→ 斜率不可算，confidence=insufficient；
- 时间跨度 < 24h → 斜率照算，但标注 confidence=low（回归对短跨度极敏感）；
- 时间跨度为 0（所有 run 同一时刻）→ 无法拟合，confidence=insufficient。

本模块不 import 其他业务模块：依赖只有标准库，run 序列是唯一数据入口。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .judge import VERDICT_FAILED, VERDICT_NOT_TESTED, VERDICT_PASSED, Verdict

# 回归所用指标的 canonical key（口径唯一来源：metrics.METRICS）
LEAK_METRIC_KEY = "mem_pss_mb"
# 斜率产物的 canonical key（标签登记在 metrics.METRICS，CLI/宿主统一引用）
SLOPE_METRIC_KEY = "mem_pss_slope_mb_per_day"
SLOPE_UNIT = "MB/day"

# 样本下限：低于此值的 run 数不计算斜率（组件默认常量，analyze 可按参覆盖）
DEFAULT_MIN_RUNS = 3
# 可信跨度下限（小时）：短于此跨度仍算斜率，但标注低置信度而非拒绝
MIN_SPAN_HOURS = 24.0

CONFIDENCE_OK = "ok"
CONFIDENCE_LOW = "low"
CONFIDENCE_INSUFFICIENT = "insufficient"

_SECONDS_PER_HOUR = 3600.0
_HOURS_PER_DAY = 24.0


@dataclass(frozen=True)
class LeakTrend:
    """一个 (pkg, profile) 的泄漏趋势分析结果（序列 + 斜率 + 置信度）。

    runs 按 startedAt 升序；slope_mb_per_day 为 None 表示样本不足或无法拟合。
    """

    runs: list[dict[str, Any]] = field(default_factory=list)
    slope_mb_per_day: float | None = None
    confidence: str = CONFIDENCE_INSUFFICIENT
    confidence_reason: str = ""
    run_count: int = 0
    span_hours: float | None = None
    first_median: float | None = None
    last_median: float | None = None

    @property
    def insufficient(self) -> bool:
        return self.slope_mb_per_day is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "runs": self.runs,
            "slopeMbPerDay": self.slope_mb_per_day,
            "confidence": self.confidence,
            "confidenceReason": self.confidence_reason,
            "runCount": self.run_count,
            "spanHours": self.span_hours,
            "firstMedian": self.first_median,
            "lastMedian": self.last_median,
        }


def analyze_leak_trend(
    runs: list[dict[str, Any]],
    *,
    min_runs: int = DEFAULT_MIN_RUNS,
    min_span_hours: float = MIN_SPAN_HOURS,
) -> LeakTrend:
    """对 run 序列做 OLS 回归（纯函数：不读库、不改输入）。

    每个 run 需含 ``startedAt``（ISO 8601 带时区）与 ``median``；缺时间或
    缺 median 的 run 被排除（不参与回归，也不进序列）。返回按时间升序的
    序列与斜率；样本不足/跨度为 0 时 slope 为 None 并说明原因。
    """
    usable = [
        run
        for run in runs
        if _parse_time(run.get("startedAt")) is not None and run.get("median") is not None
    ]
    ordered = sorted(usable, key=lambda run: _parse_time(run["startedAt"]))
    run_count = len(ordered)

    if run_count < min_runs:
        return LeakTrend(
            runs=ordered,
            confidence=CONFIDENCE_INSUFFICIENT,
            confidence_reason=(
                f"可用 run 数 {run_count} 低于样本下限 {min_runs}，"
                "不足以回归斜率（不拒绝数据，仅不给结论）"
            ),
            run_count=run_count,
        )

    origin = _parse_time(ordered[0]["startedAt"])
    hours = [
        (_parse_time(run["startedAt"]) - origin).total_seconds() / _SECONDS_PER_HOUR
        for run in ordered
    ]
    medians = [float(run["median"]) for run in ordered]
    span_hours = hours[-1] - hours[0]

    if span_hours <= 0:
        return LeakTrend(
            runs=ordered,
            confidence=CONFIDENCE_INSUFFICIENT,
            confidence_reason="所有 run 的 startedAt 相同，时间跨度为 0，无法回归斜率",
            run_count=run_count,
            span_hours=0.0,
            first_median=medians[0],
            last_median=medians[-1],
        )

    slope_per_hour = _ols_slope(hours, medians)
    slope_per_day = slope_per_hour * _HOURS_PER_DAY

    if span_hours < min_span_hours:
        confidence = CONFIDENCE_LOW
        reason = (
            f"时间跨度 {span_hours:.1f}h 低于可信跨度 {min_span_hours:.0f}h，"
            "斜率对短跨度样本敏感，仅作低置信度参考"
        )
    else:
        confidence = CONFIDENCE_OK
        reason = f"时间跨度 {span_hours:.1f}h，满足可信跨度 {min_span_hours:.0f}h"

    return LeakTrend(
        runs=ordered,
        slope_mb_per_day=slope_per_day,
        confidence=confidence,
        confidence_reason=reason,
        run_count=run_count,
        span_hours=span_hours,
        first_median=medians[0],
        last_median=medians[-1],
    )


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _ols_slope(xs: list[float], ys: list[float]) -> float:
    """普通最小二乘斜率（分母非零由调用方保证：跨度 > 0）。"""
    count = len(xs)
    mean_x = sum(xs) / count
    mean_y = sum(ys) / count
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    variance = sum((x - mean_x) ** 2 for x in xs)
    return covariance / variance


def leak_verdict(
    analysis: LeakTrend,
    *,
    threshold: float,
    level: str = "fail",
    baseline: str = "leak_trend",
) -> Verdict:
    """斜率阈值判定（阈值归宿主）：slope > threshold 为不满足，三态出结论。

    样本不足（斜率不可算）→ not_tested；低置信度不拒绝判定，但把置信度
    说明如实放进 warnings；level=warn 时不拉低结论，只记 warnings。
    """
    if level not in ("fail", "warn"):
        raise ValueError(f"level 只支持 fail/warn，收到 {level!r}")
    if analysis.insufficient:
        return Verdict(
            verdict=VERDICT_NOT_TESTED,
            baseline=baseline,
            reason=(
                f"泄漏趋势样本不足，无法判定：{analysis.confidence_reason or '斜率不可算'}"
            ),
        )

    slope = analysis.slope_mb_per_day
    check = {
        "metricKey": SLOPE_METRIC_KEY,
        "metric": LEAK_METRIC_KEY,
        "slopeMbPerDay": slope,
        "op": "le",
        "threshold": threshold,
        "level": level,
        "ok": slope <= threshold,
        "runCount": analysis.run_count,
        "spanHours": analysis.span_hours,
        "confidence": analysis.confidence,
        "confidenceReason": analysis.confidence_reason,
        "firstMedian": analysis.first_median,
        "lastMedian": analysis.last_median,
        "runIds": [run.get("runId") for run in analysis.runs],
    }
    warnings: list[dict[str, Any]] = []
    if analysis.confidence != CONFIDENCE_OK:
        warnings.append(
            {
                "metricKey": SLOPE_METRIC_KEY,
                "level": "warn",
                "ok": None,
                "confidence": analysis.confidence,
                "reason": analysis.confidence_reason,
            }
        )
    if not check["ok"]:
        if level == "fail":
            return Verdict(
                verdict=VERDICT_FAILED,
                baseline=baseline,
                checks=[check],
                warnings=warnings,
            )
        warnings.append(check)
    return Verdict(
        verdict=VERDICT_PASSED,
        baseline=baseline,
        checks=[check],
        warnings=warnings,
    )
