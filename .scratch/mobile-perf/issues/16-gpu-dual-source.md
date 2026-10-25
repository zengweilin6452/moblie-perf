# 16: GPU 双源补采与显存指标

**What to build:** 修掉 M3 遗留的 GPU 空缺（工单 10 记录"本机型实测不可用 → 永久 not_tested"）。实测发现 `cat /sys/class/kgsl/kgsl-3d0/gpubusy` 在 shell 用户下**可读**（无需 root），输出 `busy total` 二元组，`total==0` 表示该窗口 GPU 空闲，比值即窗口平均占用率。据此：

①`gpu_util_pct` 改为三级降级链——kgsl `gpubusy` 比值 → `dumpsys gpu` 文本解析（原实现）→ `not_tested`，每级降级原因如实记录；
②新增 `gpu_mem_mb`（应用 GPU 显存）：`dumpsys gpu` 输出中 `Proc <pid> total:` 的字节数换算，pid 由目标包名解析；
③两者并入 `supplement` 现有轮询线程（一次 adb 事务多条命令），不新增采集通道。

口径约束：`gpubusy` 是驱动侧约 1 s 滚动窗口的累计值（实测 total 在 1.00e6–1.04e6 间浮动且会回落），**只报聚合（median/p90/max），不承诺逐点序列对齐**，模块文档写明。

**Blocked by:** 15（复用其轮询线程与降级记录扩展点）

**Status:** ready-for-agent

- [ ] `gpubusy` 解析器纯函数：正常值、`0 0`（空闲）、`total=0`、格式异常四种分支 unit 覆盖
- [ ] 三级降级链逐级可测：kgsl 不可读 → 落 dumpsys gpu → 仍不可得 → `not_tested` + 具体原因
- [ ] `gpu_mem_mb` 按 pid 取到目标进程显存；pid 解析失败时 `not_tested` 而非取错进程
- [ ] 真机验证：滑动负载下 `gpu_util_pct` 有非零值，与 M5 的"永远 not_tested"形成对比
- [ ] 指标字典 + 双语标签 + 报告呈现（与 CPU/内存同图）

## Comments
