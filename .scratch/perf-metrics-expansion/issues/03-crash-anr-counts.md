# 03 · crash/ANR 会话计数指标化

Status: resolved
Type: task

## 目标

logwatch 事件聚合为会话级单样本指标 `app_crash_count` / `app_anr_count`（次，pc_supplement），进 judge/基线通道。

## 改动面

- logwatch.py：`count_events(events, target_pkg)` 纯函数 → {crash, anr, unattributed}；只计 detail.package == 目标包；package=None 计入 unattributed（留时间轴人工判读，不进 app 计数）。
- metrics.py：登记两 key。
- kit.py：`_stop_crash_watch` 失败/未启动返回 None（区别于正常零事件 []）；finalize 据此标 notTested 带原因（不伪造 0），正常时产出行（0 有效）；meta["logwatch"] 落 enabled/crashCount/anrCount/unattributedCount。

## 验收

- 注入替身：目标包 2 crash + 1 anr + 1 未归属 → 计数 2/1，unattributed=1 落 meta。
- 监听启动失败 → 两 key notTested 且原因含告警；正常零事件 → 0/0 有效行。
- 既有 test_kit_logwatch 全绿（事件入库行为不变）。

## Answer

2026-11-10 完成：logwatch.count_events/aggregate_rows + 字典登记 + kit 接线（_stop_crash_watch 不可用返回 None → notTested 带告警原因；正常零事件 → 0/0 有效行；meta["logwatch"] 含 unattributedCount；report 透传）。GLOSSARY 增「稳定性计数」。count_events 5 例 + kit 接线 4 例（含 stop 异常反例）绿；契约锁新增。ruff 绿。

## Comments