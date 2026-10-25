"""门禁判定：纯函数，report 数据进、三态 Verdict 出（设计方案 §6.2/§10，Q7）。

阈值归宿主（JudgeSpec 显式构造、不入库），口径归组件：对 report.metricsSummary
的每个指标取 median，在 report 的时间窗（startedAt/stoppedAt）内判定，样本数
低于下限不判定。样本不足 / baseline 的 device_profile 与 run 不匹配 / 前置
（metricsSummary、baseline 数据、阈值引用的指标）不满足 → not_tested。
阈值越界按宿主指定级别 fail（结论 failed）或 warn（结论仍 passed，记 warnings）。

本模块不 import 其他业务模块：依赖只有标准库，报告字典是唯一数据入口。
"""

from __future__ import annotations

import operator
from dataclasses import dataclass, field, replace
from typing import Any

# 统计口径的样本数下限：低于此值的 run 视为不可判定（组件默认常量，
# JudgeSpec.min_samples 可按 spec 覆盖）
DEFAULT_MIN_SAMPLES = 10

# 阈值比较符：median <op> threshold，边界语义同 Python 比较运算符
_OPS = {
    "lt": operator.lt,
    "le": operator.le,
    "gt": operator.gt,
    "ge": operator.ge,
}

# 阈值不满足时的级别：fail 拉低结论，warn 只记证据
_LEVELS = ("fail", "warn")

VERDICT_PASSED = "passed"
VERDICT_FAILED = "failed"
VERDICT_NOT_TESTED = "not_tested"


@dataclass(frozen=True)
class JudgeSpec:
    """宿主显式构造的门禁契约：基线名 + 阈值表（阈值不入库）。

    thresholds 形如 {"mem_pss_mb": ("lt", 220, "fail")}：metric_key →
    (比较符, 阈值, 不满足时的级别)。baseline_profile 与 baseline_data 由
    kit.judge 从 store 解析后注入（replace 出解析态），纯函数只读不取数。
    """

    baseline: str
    thresholds: dict[str, tuple[str, float, str]]
    min_samples: int | None = None
    baseline_profile: str | None = None
    baseline_data: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self):
        for metric_key, (op, _threshold, level) in self.thresholds.items():
            if op not in _OPS:
                raise ValueError(
                    f"阈值 {metric_key} 的 op {op!r} 不合法，可选: {', '.join(_OPS)}"
                )
            if level not in _LEVELS:
                raise ValueError(
                    f"阈值 {metric_key} 的 level {level!r} 不合法，可选: {', '.join(_LEVELS)}"
                )

    @property
    def effective_min_samples(self) -> int:
        return self.min_samples if self.min_samples is not None else DEFAULT_MIN_SAMPLES

    def resolved(self, *, profile: str | None, data: dict[str, dict[str, Any]]) -> JudgeSpec:
        """返回注入基线解析结果的副本（kit 门面用，JudgeSpec 本身保持宿主构造态）。"""
        return replace(self, baseline_profile=profile, baseline_data=data)


@dataclass(frozen=True)
class Verdict:
    """三态门禁结论（GLOSSARY：passed / failed / not_tested）+ 证据引用。"""

    verdict: str
    baseline: str
    checks: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "baseline": self.baseline,
            "reason": self.reason,
            "checks": self.checks,
            "warnings": self.warnings,
        }


def judge(report: dict[str, Any], spec: JudgeSpec) -> Verdict:
    """对单个 run 报告执行门禁判定（纯函数：不读库、不连设备、不改输入）。"""
    metrics_summary = report.get("metricsSummary")
    if not metrics_summary:
        return _not_tested(spec, "run 报告没有 metricsSummary 聚合数据，无法判定")

    run_profile = report.get("deviceProfile")
    if spec.baseline_profile and run_profile != spec.baseline_profile:
        return _not_tested(
            spec,
            f"设备档案不匹配：baseline {spec.baseline!r} 绑定 "
            f"{spec.baseline_profile!r}，run 为 {run_profile!r}（口径不可比）",
        )
    if not spec.baseline_data:
        return _not_tested(
            spec, f"baseline {spec.baseline!r} 不存在或没有任何指标数据，无法判定"
        )

    window = {
        "startedAt": report.get("startedAt"),
        "stoppedAt": report.get("stoppedAt"),
    }
    run_id = report.get("runId")

    checks: list[dict[str, Any]] = []
    for metric_key, (op, threshold, level) in sorted(spec.thresholds.items()):
        entry = metrics_summary.get(metric_key)
        if entry is None:
            return _not_tested(spec, f"run 缺少阈值引用的指标 {metric_key!r} 的聚合数据")
        sample_count = entry.get("sampleCount") or 0
        if sample_count < spec.effective_min_samples:
            return _not_tested(
                spec,
                f"指标 {metric_key!r} 样本数 {sample_count} 低于下限 "
                f"{spec.effective_min_samples}，统计口径不可判定",
            )
        median = entry.get("median")
        baseline_entry = spec.baseline_data.get(metric_key)
        if median is None or baseline_entry is None or baseline_entry.get("value") is None:
            return _not_tested(
                spec, f"指标 {metric_key!r} 缺少 median 或 baseline 参照值，无法判定"
            )
        checks.append(
            {
                "metricKey": metric_key,
                "median": median,
                "op": op,
                "threshold": threshold,
                "level": level,
                "ok": _OPS[op](median, threshold),
                "baselineValue": baseline_entry["value"],
                "baselineSourceRunId": baseline_entry.get("sourceRunId"),
                "sampleCount": sample_count,
                "sourceRunId": run_id,
                "window": window,
            }
        )

    warnings = [check for check in checks if not check["ok"] and check["level"] == "warn"]
    if any(not check["ok"] and check["level"] == "fail" for check in checks):
        return Verdict(
            verdict=VERDICT_FAILED, baseline=spec.baseline, checks=checks, warnings=warnings
        )
    return Verdict(
        verdict=VERDICT_PASSED, baseline=spec.baseline, checks=checks, warnings=warnings
    )


def _not_tested(spec: JudgeSpec, reason: str) -> Verdict:
    return Verdict(verdict=VERDICT_NOT_TESTED, baseline=spec.baseline, reason=reason)
