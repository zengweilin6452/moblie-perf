# 02: DeviceGate 短事务准入 + 契约测试

**What to build:** gate.py 默认实现：按 moblie-auto 锁文件协议**对等参与**（`~/.mobile-auto/leases/<sha256(serial)[:24]>.lock` msvcrt 1 字节区域锁 + holder.json 写 pid/hostname/purpose）。collect 全链路中每类设备命令（start/stop/pull）各为短事务借用，**采集进行中不持锁**；获取失败 → `GateBusyError` fast-fail（不内置重试）；非 Windows passthrough + warning 事件（ADR-0001）。

**Blocked by:** 01

**Status:** done（2026-10-25，perf/ticket-02 三笔提交已合并进 mobile-perf-dev @ e841952；74 unit/contract 通过，含与真实 DeviceLease 双向互抢契约（真跨进程）。ensure_ready 属票 05 keeper，届时复用 PerfKit._borrowed；stop+pull 合并为一个短事务（Harness 侧本是一次 perf-stop --output））

- [ ] moblie-auto DeviceSession 持锁期间发起 collect → GateBusyError 立即失败，不等待
- [ ] collect 录制进行中，moblie-auto 侧能正常借到同一设备（短事务语义实测）
- [ ] 嵌入模式：holder.json 的 pid 为本进程 → 重入放行
- [ ] tests/contract 含与真实 moblie-auto DeviceLease 互抢的协议契约测试（防漂移）；unit 用模拟锁覆盖竞争分支；非 Windows 分支有单测

## Comments
