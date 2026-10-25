# 15: 逐帧采集与帧耗时指标（gfxinfo framestats）

**What to build:** 新增 `frameinfo` 模块：采集窗口内用 `dumpsys gfxinfo <pkg> reset` 清零、轮询 `dumpsys gfxinfo <pkg> framestats` 增量读逐帧时间戳，解析出真逐帧耗时分布并入库（source=pc_supplement）。产出 5 个新 canonical key：`app_frame_p50_ms` / `app_frame_p90_ms` / `app_frame_p99_ms` / `app_frame_jank_ratio_pct`（帧时长 > 刷新周期×1.5 的帧占比）/ `app_frame_count`。报告新增帧耗时分布图（直方图 + P50/P90/P99 标线），并与既有窗口级卡顿分级并列展示、**显式标注两者粒度不同、不可互算**。

真实数据源（已实测，见 M6 方案 §2.3）：`framestats` 输出 21 列 CSV，`FrameCompleted − IntendedVsync` 即该帧总耗时（纳秒）；`reset` 后读回只含本窗口帧。本机型 `dumpsys SurfaceFlinger --latency` 返回空，不作为数据源。

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] 解析器为纯函数：对真实 framestats 样本（含空窗口、单帧、`reset` 前有历史帧三种情形）解析正确，unit 覆盖
- [ ] 轮询器独立线程、间隔 ≥1 s、不持 gate 设备权；`dumpsys` 不触发渲染（不影响被测 App）
- [ ] 5 个 key 进指标字典并入库；`app_frame_count` 作为样本下限依据参与 judge 的样本数判定
- [ ] 报告分布图与窗口级卡顿分级并列，页面明确写出两种口径的差异
- [ ] 目标 App 无渲染（后台/零帧）时如实 `not_tested` + 原因，不产出 0 值冒充

## Comments
