# mobile_perf 组件包 — Spec

规范全文：[docs/设计方案-App性能测试组件.md](../../docs/设计方案-App性能测试组件.md)（v0.3 盘问共识版，20 项决策）。
术语：[GLOSSARY.md](../../GLOSSARY.md)。架构决策：[docs/adr/0001~0003](../../docs/adr/)。

拆票说明：11 条 tracer-bullet 垂直切片，对应里程碑 M1（01-04）、M2（05-08）、M3（09-10）、M4（11）。
06 为**软依赖**（blocked by 04）：等 M1 组件稳定后再动 SoloPi App 与真机 APK 单活线切换（ADR-0003，不可逆）。

## 关联资产

- 采集内核：本仓库 SoloPi 全链路（Harness 纯标准库 CLI + 真机 App），Windows 实测通过
- 基线数据：`perf-output/`（真实 CSV，指标字典校准用，已 gitignore）
- 宿主参照：`D:\workspace\claudecode-test\moblie-auto`（Python 3.12 包形态、测试三段式、租约协议）
- 真机：serial `749622c9`（SoloPi 内部 adb 走手机 tcpip 5555）
