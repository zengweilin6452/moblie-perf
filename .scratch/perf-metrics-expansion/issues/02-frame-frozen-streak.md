# 02 · 帧级冻帧占比 + 最长连续超时帧段

Status: resolved
Type: task

## 目标

frameinfo 逐帧数据派生两个会话级单样本指标：`app_frame_frozen_ratio_pct`（%，>700ms 冻帧占比）、`app_frame_jank_streak_max`（帧，连续超时帧最长段）。

## 改动面

- frameinfo.py：`FROZEN_FRAME_MS=700.0`；`frame_stats` 增加 frozenCount/frozenRatioPct/jankStreakMax（按采样顺序扫连续 janky 段）；`STAT_METRIC_KEYS`/`aggregate_from_samples` +2 行。
- metrics.py：登记两 key（pc_supplement）。
- kit.py finalize：frame_meta 增加 frozenThresholdMs/frozenCount/jankStreakMax。
- report.py `_frame_markup`：rows_spec +2 行，detail 补冻帧判据。
- GLOSSARY「逐帧口径」补冻帧/连续段口径。

## 验收

- 构造样本：已知冻帧数/占比/最长段手算对照；空样本 → 无行（不伪造 0）；全正常帧 → 占比 0、段 0 是有效结论。
- 与窗口级卡顿分级并列不互算（报告文案已有）。

## Answer

2026-11-10 完成：frame_stats 增 frozenCount/frozenRatioPct/jankStreakMax（>700ms 严格大于；连续段按采样序列），聚合 5→7 行，frame_meta/逐帧小节/GLOSSARY 同步。手算对照（3/7 冻帧、最长段 3）+ 边界（恰 700ms 不计）+ 缺周期时冻帧仍计数等反例绿；契约锁新增。ruff 绿。

## Comments