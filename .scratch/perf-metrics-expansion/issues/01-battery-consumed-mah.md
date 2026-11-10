# 01 · 场景耗电量 mAh（电流时序积分）

Status: resolved
Type: task

## 目标

对 SoloPi「实时电流(mA)」时序积分得会话级 `battery_consumed_mah`（mAh，source=solo_pi 派生，无原始列映射）。

## 改动面

- collector.py：新增 `read_metric_series(csv_path, metric_key)` → [(RecordTime 毫秒, 值)]，复用现有表头映射/编码/数值解析口径；时间不可解析的行跳过。
- battery.py（新）：`read_source_samples`（多文件合并、按时间排序）、`consumed_mah`（梯形积分，只计放电段 max(0,-mA)，dt>0）、`aggregate_from_artifacts`（单样本行；<2 样本或零时长返回空）。
- metrics.py：登记 `battery_consumed_mah`（mAh，solo_pi，场景耗电量/Energy consumed）。
- kit.py finalize：并入聚合；`"Battery" in items` 且无行 → metricsSummary 标 notTested 带原因；充电降级由 mark_not_tested 自动覆盖（battery_* 前缀）。
- report.py build_report：含该 key 时落 `report["battery"]` 口径段（sourceMetric/method/granularity）。

## 验收

- 真机夹具电流 CSV（放电为负）积分手算对照；全正电流 → 0 mAh 有效行；单样本/零时长 → 无行；无 Battery 采集 → 无行不标 notTested。
- 契约测试锁新 key 无原始列映射、双语标签齐全。
- 反例：时间倒序/重复时间戳不产生负积分。

## Answer

2026-11-10 完成：collector.read_metric_series + battery.py（梯形积分/只计放电段）+ 字典登记 + kit finalize 接线（Battery 项无时序 → notTested；充电由 mark_not_tested 覆盖）+ report["battery"] 口径段。tests/unit/test_battery.py 22 项绿（含真机夹具正电量 sanity、全正电流=0、倒序/重复时间戳反例）；契约锁新增。ruff 绿。

## Comments