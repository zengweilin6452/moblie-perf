"""场景耗电量（perf-metrics-expansion 01）：实时电流时序积分，会话级 mAh。

口径（对真机 CSV 校准，tests/fixtures/solopi_csv_sample 的实时电流列）：

- 该机型**放电电流为负值**（充电/回灌为正）；只计放电段 ``max(0, -mA)``，
  正值不贡献耗电。充电中的会话由 keeper 巡检降级（``battery_*`` 前缀指标
  统一标 not_tested），不在本模块判断充电状态。
- 梯形积分：相邻样本 ``(c[i]+c[i+1])/2 × dt``；dt 取 RecordTime 毫秒差换算
  小时，只积 dt>0 的区间（重复/倒序时间戳不产生负积分）。
- 会话级单样本结论（聚合字段填单值，与启动编排/卡顿分级同形）；
  <2 个样本或有效时长为 0 → 不出数，不硬造 0。全正电流积出 0 mAh 是
  有效结论（窗口内无放电），照常入库。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import collector
from . import metrics as metrics_module

SOURCE_METRIC_KEY = "battery_current_ma"
METRIC_KEY = "battery_consumed_mah"

_MS_PER_HOUR = 3_600_000.0


def read_source_samples(artifacts_dir: str | Path) -> list[tuple[float, float]]:
    """从 pull 产物读 (RecordTime 毫秒, 电流 mA) 时序；无 Battery 采集返回空列表。

    复用 collector 的 CSV 时序读取通道（同一编码/列名映射/数值解析口径），
    多文件样本直接合并（排序在积分时做）。原始 CSV 只读不落库。
    """
    artifacts_dir = Path(artifacts_dir)
    if not artifacts_dir.is_dir():
        return []
    samples: list[tuple[float, float]] = []
    for path in sorted(artifacts_dir.rglob("*.csv")):
        if not path.is_file() or path.stat().st_size == 0:
            continue
        samples.extend(collector.read_metric_series(path, SOURCE_METRIC_KEY))
    return samples


def consumed_mah(samples: list[tuple[float, float]]) -> float | None:
    """梯形积分放电电流得 mAh；样本不足或有效时长为 0 返回 None。

    输入为 (RecordTime 毫秒, 电流 mA) 列表；内部按时间排序，dt<=0 的区间
    跳过。放电电流取 ``max(0, -mA)``（真机校准：放电为负）。
    """
    if len(samples) < 2:
        return None
    ordered = sorted(samples, key=lambda item: item[0])
    total = 0.0
    integrated = False
    for (t0, v0), (t1, v1) in zip(ordered, ordered[1:], strict=False):
        dt_h = (t1 - t0) / _MS_PER_HOUR
        if dt_h <= 0:
            continue
        c0 = max(0.0, -v0)
        c1 = max(0.0, -v1)
        total += (c0 + c1) / 2.0 * dt_h
        integrated = True
    return total if integrated else None


def aggregate_from_artifacts(artifacts_dir: str | Path) -> list[dict[str, Any]]:
    """把 artifacts 的电流时序积分为一个可入库聚合行（无法积分时返回空）。"""
    value = consumed_mah(read_source_samples(artifacts_dir))
    if value is None:
        return []
    return [metrics_module.aggregate_row(METRIC_KEY, [value])]
