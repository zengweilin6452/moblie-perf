# 01: 包骨架 + tracer——mobile-perf collect 真机采集闭环

**What to build:** 新建 src 布局 Python 包 `mobile_perf`（≥3.10，pyproject + ruff + pytest 三段式 tests/unit|contract|device，形态对齐 moblie-auto）。交付一条端到端命令：`mobile-perf collect <pkg> --items CPU,FPS,Memory --duration N` 在真机完成 启动 SoloPi 采集 → 计时 → 停止 → 自动 pull → 产物落盘 `runs/<runId>/`。solo.py 是唯一知道 Harness 的模块（subprocess + JSON stdout 契约，ADR-0002）；ADB 三级解析（显式 > 环境变量 > 随包 platform-tools 37.0.1）。

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] `pip install -e .` 后，`mobile-perf collect tv.danmaku.bili --duration 60` 对真机 749622c9 跑通采集→pull→runs/ 产物落盘（device 测试，`-m device` 门控；无设备时如实报告，不计为通过）
- [ ] solo.py 以子进程调用 Harness，宿主进程 sys.path 未被注入、`timing` 等顶级模块名未被劫持（contract 测试证明）
- [ ] tests/unit 全绿 + ruff 通过
- [ ] SoloPi 基线 tag `solopi-baseline` 已打在初始提交（上游同步锚点，§10）

## Comments
