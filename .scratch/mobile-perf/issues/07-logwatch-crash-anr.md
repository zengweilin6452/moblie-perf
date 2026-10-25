# 07: logwatch——crash/ANR 监听入时间轴

**What to build:** logwatch.py：采集期间监听 logcat 的 crash/ANR（§4 #7），写入 events 表（kind=crash/anr，统一时间轴 ts），报告中与会话/打标同轴呈现。监听不持有设备权（短事务语义，与 keeper 一致）。

**Blocked by:** 03

**Status:** ready-for-agent

- [ ] 采集期间人为制造 crash（可测 App）被捕获并入库，时间轴与会话/打标对齐
- [ ] crash/anr 事件出现在该 run 的报告时间轴上
- [ ] 监听进程/线程不阻塞宿主 UI 自动化借设备（短事务验证）

## Comments
