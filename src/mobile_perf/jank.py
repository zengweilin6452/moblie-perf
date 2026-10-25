"""卡顿分级（ticket 14）：把每采样窗口的最长延迟按阈值分桶统计。

粒度约束（以 tests/fixtures/solopi_csv_sample 真机 CSV 校准）：SoloPi FPS
采集只有 ``延迟次数(次)`` / ``延迟占比(%)`` / ``最长延迟时间(ms)`` 三列，
其中 ``最长延迟时间`` 是每个采样窗口（约 1 s）的单值，不是逐帧延迟分布。
因此本组件无法给出逐帧卡顿分级，采用"会话内超阈值窗口次数"口径：一个
采样窗口的最长延迟超过某档阈值即记该档一次，分桶互斥（只计最高档）。
与设计方案的偏差如实记录于此，不硬造逐帧数据。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from . import metrics as metrics_module

# 分级口径的数据源列（SoloPi FPS 采集，每采样窗口一个值）
SOURCE_METRIC_KEY = "frame_delay_max_ms"

# 分级名称与默认阈值（ms，严格大于才计入；宿主可覆盖）
GRADE_LIGHT = "light"
GRADE_HEAVY = "heavy"
GRADE_SEVERE = "severe"
GRADES = (GRADE_LIGHT, GRADE_HEAVY, GRADE_SEVERE)
DEFAULT_THRESHOLDS_MS: dict[str, float] = {
    GRADE_LIGHT: 100.0,
    GRADE_HEAVY: 300.0,
    GRADE_SEVERE: 700.0,
}

# 分级 → 入库 canonical key（metrics.METRICS 中登记）
GRADE_METRIC_KEYS: dict[str, str] = {
    GRADE_LIGHT: "app_jank_light_count",
    GRADE_HEAVY: "app_jank_heavy_count",
    GRADE_SEVERE: "app_jank_severe_count",
}


def validate_thresholds(thresholds: Mapping[str, float]) -> dict[str, float]:
    """校验并规范化阈值表：三档齐全、均为正数且严格递增（light<heavy<severe）。"""
    missing = [grade for grade in GRADES if grade not in thresholds]
    if missing:
        raise ValueError(f"阈值表缺少分级 {', '.join(missing)}")
    normalized = {grade: float(thresholds[grade]) for grade in GRADES}
    for grade, value in normalized.items():
        if value <= 0:
            raise ValueError(f"分级 {grade} 的阈值必须为正数，收到 {value}")
    if not (normalized[GRADE_LIGHT] < normalized[GRADE_HEAVY] < normalized[GRADE_SEVERE]):
        raise ValueError(
            "分级阈值必须严格递增（light<heavy<severe），收到 "
            f"{normalized[GRADE_LIGHT]}/{normalized[GRADE_HEAVY]}/{normalized[GRADE_SEVERE]}"
        )
    return normalized


def grade_samples(
    samples: Iterable[float],
    thresholds_ms: Mapping[str, float] | None = None,
) -> dict[str, int]:
    """按每采样窗口最长延迟分桶计数（严格大于阈值；分桶互斥取最高档）。"""
    thresholds = validate_thresholds(thresholds_ms or DEFAULT_THRESHOLDS_MS)
    counts = {grade: 0 for grade in GRADES}
    for value in samples:
        for grade in reversed(GRADES):  # severe → light，命中最高档即止
            if value > thresholds[grade]:
                counts[grade] += 1
                break
    return counts


def read_source_samples(artifacts_dir: str | Path) -> list[float]:
    """从 pull 产物读取「最长延迟时间」逐窗口样本；无 FPS 采集返回空列表。

    复用 collector 的 CSV 读取通道（同一编码/列名映射/数值解析口径），不重复
    实现解析。原始 CSV 只读不落库。
    """
    from . import collector  # 局部导入：collector 依赖本模块会形成循环

    artifacts_dir = Path(artifacts_dir)
    if not artifacts_dir.is_dir():
        return []
    samples: list[float] = []
    for path in sorted(artifacts_dir.rglob("*.csv")):
        if not path.is_file() or path.stat().st_size == 0:
            continue
        samples.extend(collector._read_metric_values(path).get(SOURCE_METRIC_KEY, ()))
    return samples


def aggregate_from_artifacts(
    artifacts_dir: str | Path,
    thresholds_ms: Mapping[str, float] | None = None,
) -> list[dict[str, Any]]:
    """把 artifacts 的逐窗口最长延迟分桶为三个可入库聚合行（无 FPS 数据返回空）。

    每个分级是"会话内超阈值窗口数"的单样本指标：聚合行的 min=median=mean=
    max=p90=计数，sample_count=1（与启动编排单样本指标同形）。计数为 0 也
    入库——"0 个超阈值窗口"本身就是有效结论。
    """
    samples = read_source_samples(artifacts_dir)
    if not samples:
        return []
    counts = grade_samples(samples, thresholds_ms)
    return [
        metrics_module.aggregate_row(GRADE_METRIC_KEYS[grade], [float(counts[grade])])
        for grade in GRADES
    ]
