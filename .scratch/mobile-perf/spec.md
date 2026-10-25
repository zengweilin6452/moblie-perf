# mobile_perf 组件包 — Spec

规范全文：[docs/设计方案-App性能测试组件.md](../../docs/设计方案-App性能测试组件.md)（v0.3 盘问共识版，20 项决策）。
术语：[GLOSSARY.md](../../GLOSSARY.md)。架构决策：[docs/adr/0001~0003](../../docs/adr/)。

拆票说明：11 条 tracer-bullet 垂直切片，对应里程碑 M1（01-04）、M2（05-08）、M3（09-10）、M4（11）——**已全部完成并合并（2026-10-25，含 code-review 修复）**。
指标扩展批（M5）：12 启动时间、13 内存泄漏趋势、14 卡顿分级——三者相互独立，均无阻塞。
06 为**软依赖**（blocked by 04）：等 M1 组件稳定后再动 SoloPi App 与真机 APK 单活线切换（ADR-0003，不可逆）——已完成。

## M6 · GPU / 弱网 / 整页与接口耗时（工单 15–20）

方案全文：[docs/设计方案-M6-GPU与弱网.md](../../docs/设计方案-M6-GPU与弱网.md)（含真机实测可行性证据）。

- 15 逐帧采集与帧耗时指标（`gfxinfo framestats`，补 M5 窗口级口径的粒度短板）— 无阻塞
- 16 GPU 双源双指标（`kgsl gpubusy` 实测 shell 可读，修掉 M3 遗留的永久 `not_tested`）— blocked by 15
- 17 弱网编排直通档（mitmproxy 子进程 + adb reverse + 系统代理，退出必恢复）— 无阻塞
- 18 解密档与接口级指标（**先探测再决定**：App 不信任代理 CA 时如实降级）— blocked by 17
- 19 整页加载耗时（末帧稳定口径，不依赖解密）— blocked by 15, 17
- 20 M6 集成、档位可比性与文档 — blocked by 15–19

前端 15/17 可并行；16、19 分属两条链；20 收口。

## 关联资产

- 采集内核：本仓库 SoloPi 全链路（Harness 纯标准库 CLI + 真机 App），Windows 实测通过
- 基线数据：`perf-output/`（真实 CSV，指标字典校准用，已 gitignore）
- 宿主参照：`D:\workspace\claudecode-test\moblie-auto`（Python 3.12 包形态、测试三段式、租约协议）
- 真机：serial `749622c9`（SoloPi 内部 adb 走手机 tcpip 5555）
