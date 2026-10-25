# 07: logwatch——crash/ANR 监听入时间轴

**What to build:** logwatch.py：采集期间监听 logcat 的 crash/ANR（§4 #7），写入 events 表（kind=crash/anr，统一时间轴 ts），报告中与会话/打标同轴呈现。监听不持有设备权（短事务语义，与 keeper 一致）。

**Blocked by:** 03

**Status:** done（2026-10-25，perf/ticket-07 三笔提交已合并进 mobile-perf-dev（kit.py 冲突已解：事件回填后统一写盘双产物）；213 unit/contract + 真机 device 通过（am crash 实测入时间轴）。发现修复：logcat 需 -T 1 防历史 crash 重放。真机注意：MIUI 上 am crash 崩的是子进程，主缓冲可能无 FATAL，events buffer am_crash 路径已覆盖）

- [ ] 采集期间人为制造 crash（可测 App）被捕获并入库，时间轴与会话/打标对齐
- [ ] crash/anr 事件出现在该 run 的报告时间轴上
- [ ] 监听进程/线程不阻塞宿主 UI 自动化借设备（短事务验证）

## Comments
