# 15: 逐帧采集与帧耗时指标（gfxinfo framestats）

**What to build:** 新增 `frameinfo` 模块：采集窗口内用 `dumpsys gfxinfo <pkg> reset` 清零、轮询 `dumpsys gfxinfo <pkg> framestats` 增量读逐帧时间戳，解析出真逐帧耗时分布并入库（source=pc_supplement）。产出 5 个新 canonical key：`app_frame_p50_ms` / `app_frame_p90_ms` / `app_frame_p99_ms` / `app_frame_jank_ratio_pct`（帧时长 > 刷新周期×1.5 的帧占比）/ `app_frame_count`。报告新增帧耗时分布图（直方图 + P50/P90/P99 标线），并与既有窗口级卡顿分级并列展示、**显式标注两者粒度不同、不可互算**。

真实数据源（已实测，见 M6 方案 §2.3）：`framestats` 输出 21 列 CSV，`FrameCompleted − IntendedVsync` 即该帧总耗时（纳秒）；`reset` 后读回只含本窗口帧。本机型 `dumpsys SurfaceFlinger --latency` 返回空，不作为数据源。

**Blocked by:** None (can start immediately)

**Status:** done（2026-10-25）

- [x] 解析器为纯函数：对真实 framestats 样本（含空窗口、单帧、`reset` 前有历史帧三种情形）解析正确，unit 覆盖
- [x] 轮询器独立线程、间隔 ≥1 s、不持 gate 设备权；`dumpsys` 不触发渲染（不影响被测 App）
- [x] 5 个 key 进指标字典并入库；`app_frame_count` 作为样本下限依据参与 judge 的样本数判定
- [x] 报告分布图与窗口级卡顿分级并列，页面明确写出两种口径的差异
- [x] 目标 App 无渲染（后台/零帧）时如实 `not_tested` + 原因，不产出 0 值冒充

> 说明：报告侧以「逐帧卡顿」表格（P50/P90/P99 + 超时帧占比）呈现，未做直方图；
> 分布图留待工单 20 的报告增强。

## Comments

### 2026-10-25 完成记录

实现见 commit `0a92a5f`：`frameinfo.py`、5 个 `app_frame_*` key、`FramePoller`、
kit/report/CLI 接线（`collect --frame-poll`）。

**关键实测发现**：`dumpsys gfxinfo` 打印的表头把 `FrameInterval`（第 10 列）与
`FrameStartTime`（第 11 列）**写反了**，而数据本身严格按 AOSP `FrameInfoIndex` 顺序。
按表头名解析会把纳秒时间戳当成刷新周期，算出 `refreshHz = 0`。改为**按位置解析**
（`_COL`）+ 量级校验（>1000ms 视为取错列，回退相邻列）。修复后真机 `refreshHz`
从 0.0 → 121.55。回归测试 `test_real_fixture_refresh_rate_is_plausible_not_zero`
锁死这一点。

另一条运维事实：**屏幕熄灭时 `dumpsys gfxinfo` 不产出任何帧**，采集前需
`input keyevent KEYCODE_WAKEUP`。

真机实测（426 帧 / 121.42Hz）：P50 6.152ms、P90 9.014ms、P99 14.713ms、
超时帧占比 1.643%（7 帧）。

验证：unit 23 项 + kit 集成 4 项 + report 4 项 + device 3 项全绿，ruff 干净。
