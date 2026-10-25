# 组件边界：零依赖宿主，设备准入用 DeviceGate 依赖反转

mobile_perf 不 import moblie-auto、不复用其配置与租约实现（避免背上 Python≥3.12 与依赖链），反向由宿主注入 adb 路径、serial、host_run_id 与 DeviceGate。DeviceGate 是本项目定义的短事务设备准入协议：只在发起设备命令的短窗口持有，采集进行中不持有设备权——否则与宿主 DeviceSession 的整生命周期持锁互斥，联动场景必然死锁。独立 CLI 模式下按 moblie-auto 的锁文件协议（`~/.mobile-auto/leases/<sha256(serial)[:24]>.lock` + msvcrt 1 字节区域锁 + holder.json 记 pid/purpose）自实现并互操作；非 Windows 降级为 passthrough 并告警。

## Considered Options

- **import mobile_auto.core.lease.DeviceLease**：耦合宿主版本与依赖链，否决；改用 tests/contract 对真实 DeviceLease 互抢的协议契约测试锁住不漂移。
- **采集会话全程持锁**：采集是分钟级而 PC 侧大多空闲，长持锁把 UI 自动化挡在门外，否决；moblie-auto 的 scrcpy 通道已有"短取后释放"先例。
