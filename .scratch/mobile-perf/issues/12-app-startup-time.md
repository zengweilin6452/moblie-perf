# 12: App 启动时间采集（冷/温启动，与资源曲线同轴）

**What to build:** 新增启动时间指标：编排层在采集窗口内完成启动动作（cold：force-stop → 启动计时 → 首帧；warm：退后台 → 再前台），取 `am start -W`（TotalTime/LaunchState）与 logcat `Displayed`/`reportFullyDrawn` 双路互证；canonical key `app_cold_start_ms` / `app_warm_start_ms`（单位 ms，命名法合规）；**关键卖点：启动发生在 SoloPi 采集窗口内**——启动期的 CPU/FPS/内存曲线与启动时刻同轴（定位启动抖动的资源原因）。指标经字典入库（source 需要新值 `pc_orchestration`，store/字典同步扩展），judge/report/trend 无需改造自动纳入。库 API 与 CLI（`mobile-perf collect` 加 `--start-mode cold|warm` 之类）双入口。

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] 真机（device 门控）冷/温启动各采得：启动毫秒数入库、`LaunchState`/`Displayed` 双路一致性记录
- [ ] 启动期资源曲线覆盖启动时刻（启动点作为事件或零点标记入统一时间轴）
- [ ] judge 可对 app_cold_start_ms 设阈值产出三态；report/trend 呈现（双语标签）
- [ ] 充电/无障碍等前置巡检与现有 ensure_ready 流程兼容（幂等）

## Comments
