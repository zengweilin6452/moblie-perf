# 性能指标扩充 A+（耗电/冻帧/稳定性计数/线程FD/热状态）

> 来源：2026-11-10 会话「还有什么 app 性能指标可以加进去」，用户拍板「先做 A 组三个 + 线程/FD + 热状态」。
> 基线：perf/m6-dev，指标字典 30 项（metrics.py）。术语以 GLOSSARY.md 为准。

## 范围与口径决策

| # | 指标 | canonical key | 单位 | source | 口径 |
|---|---|---|---|---|---|
| 01 | 场景耗电量 | `battery_consumed_mah` | mAh | solo_pi（派生） | 实时电流时序梯形积分；**只计放电段**（真机校准：放电为负值，取 max(0, -mA)；正值=充电/回灌不计）；dt 取 RecordTime 毫秒差，只积 dt>0 区间；<2 样本或零时长 → 不出数 |
| 02 | 帧级冻帧占比 | `app_frame_frozen_ratio_pct` | % | pc_supplement | 逐帧耗时 >700ms（Android vitals 冻帧判据）的帧占比；与窗口级 >700ms 严重卡顿不可互算 |
| 02 | 最长连续超时帧段 | `app_frame_jank_streak_max` | 帧 | pc_supplement | 采样序列中连续「耗时 > 自身刷新周期×1.5」帧的最长段；未渲染的帧不在 framestats 里，不计入段 |
| 03 | crash 会话计数 | `app_crash_count` | 次 | pc_supplement | logwatch 事件按 kind 计数；**只计 detail.package == 目标包**的事件；未归属（package=None）事件单独计数落 meta，不计入 app 计数 |
| 03 | ANR 会话计数 | `app_anr_count` | 次 | pc_supplement | 同上；监听未启动/停止失败 → not_tested（不伪造 0）；监听正常且零事件 → 0 是有效结论 |
| 04 | 线程数 | `app_thread_count` | 个 | pc_supplement | /proc/\<pid\>/status 的 Threads 字段；pid 取 pidof 唯一才采（同显存口径） |
| 04 | FD 数 | `app_fd_count` | 个 | pc_supplement | ls -1 /proc/\<pid\>/fd 计数；输出含 Permission denied/No such file → None（**不伪造 0**） |
| 05 | 热状态档 | `device_thermal_status` | 级 | pc_supplement | dumpsys thermalservice 的 thermal status（0 NONE–6 SHUTDOWN）；**只记录不判定**（混杂因子，阈值归宿主） |
| 05 | CPU 当前最大频率 | `cpu_freq_max_mhz` | MHz | pc_supplement | 各核 scaling_cur_freq 取最大（kHz→MHz）；降频上下文，只记录不判定 |

## 共同约束

- 全部经指标字典登记（双语标签/单位/source），过 tests/contract/test_metrics_dictionary.py。
- 降级口径沿用现有哲学：拿不到 → not_tested/degraded 事件带原因，不硬造 0；充电降级由 mark_not_tested 的 battery_* 前缀自动覆盖 01。
- 04/05 挂进 SupplementPoller 同一 adb 短事务（段标记分隔），不持 gate 设备权；poller 级默认开，kit 无需新参数。
- 04/05 的 adb 命令含 `$(pidof <pkg>)` 内联替换与 sysfs glob，真机验收确认 shell 语义（C-116：能力按 OS 版本降级）。
- 报告：聚合表/曲线自动吸收新 key；02 进逐帧小节；01 口径随 report["battery"] 落 JSON。

## 工单

- 01 场景耗电量（battery.py + collector 时序读取助手）
- 02 帧级冻帧/连续超时（frameinfo.py + report 逐帧小节）
- 03 crash/ANR 计数（logwatch.py + kit.finalize 接线）
- 04 线程/FD（supplement.py）
- 05 热状态/降频（supplement.py）—— Blocked by: 04（同文件，先 04 后 05）

## Comments

- 2026-11-10 建档，口径经会话推荐清单用户拍板。