# 16: GPU 双源补采与显存指标

**What to build:** 修掉 M3 遗留的 GPU 空缺（工单 10 记录"本机型实测不可用 → 永久 not_tested"）。实测发现 `cat /sys/class/kgsl/kgsl-3d0/gpubusy` 在 shell 用户下**可读**（无需 root），输出 `busy total` 二元组，`total==0` 表示该窗口 GPU 空闲，比值即窗口平均占用率。据此：

①`gpu_util_pct` 改为三级降级链——kgsl `gpubusy` 比值 → `dumpsys gpu` 文本解析（原实现）→ `not_tested`，每级降级原因如实记录；
②新增 `gpu_mem_mb`（应用 GPU 显存）：`dumpsys gpu` 输出中 `Proc <pid> total:` 的字节数换算，pid 由目标包名解析；
③两者并入 `supplement` 现有轮询线程（一次 adb 事务多条命令），不新增采集通道。

口径约束：`gpubusy` 是驱动侧约 1 s 滚动窗口的累计值（实测 total 在 1.00e6–1.04e6 间浮动且会回落），**只报聚合（median/p90/max），不承诺逐点序列对齐**，模块文档写明。

**Blocked by:** 15（复用其轮询线程与降级记录扩展点）

**Status:** done（2026-10-26，真机验收待设备回连）

- [x] `gpubusy` 解析器纯函数：正常值、`0 0`（空闲）、`total=0`、格式异常四种分支 unit 覆盖
- [x] 三级降级链逐级可测：kgsl 不可读 → 落 dumpsys gpu → 仍不可得 → `not_tested` + 具体原因
- [x] `gpu_mem_mb` 按 pid 取到目标进程显存；pid 解析失败时 `not_tested` 而非取错进程
- [ ] 真机验证：滑动负载下 `gpu_util_pct` 有非零值，与 M5 的"永远 not_tested"形成对比
- [x] 指标字典 + 双语标签 + 报告呈现（与 CPU/内存同图）

## Comments

### 2026-10-26 完成记录

**实现**：`supplement.py` 扩展（三级降级链 + `gpu_mem_mb` + 段标记解析）、
`metrics.gpu_mem_mb`、kit 接线（`target_pkg` → pid）、report 新增 GPU 小节与
同图第三条轴。

**五个设计要点**：

1. **一次 adb 事务取全部补采项，靠段标记切分**。原实现直接把 `dumpsys battery`
   与 `dumpsys gpu` 的输出拼在一起解析——加了 kgsl 的 `cat` 之后这个做法开始
   出问题（`cat` 的错误信息、`dumpsys` 里偶然出现的 `temperature:` 会串味）。
   改为 `echo __mp_section__:<name>` 分段，`split_sections` 纯函数切分，标记
   之前的噪声丢弃。
2. **`0 0` 是有效读数，不是"读不到"**。`total==0 且 busy==0` 表示该窗口 GPU
   空闲 → 报 0%；但 `busy!=0 且 total==0`（驱动窗口刚重置）比值无意义 → 降级。
   这条区分直接决定空闲期会不会被误记成 not_tested。
3. **降级链逐级留痕**。`degraded[].stages` 记录 `kgsl:kgsl_not_readable` /
   `kgsl:kgsl_unparseable` / `dumpsys:unparseable`，区分"这台机器根本没有这个
   源"（非 Adreno）与"有这个源但这次读失败"（权限/瞬时态）——两者的处置完全不同。
   原因在整个会话内累计，避免最后一轮偶然成功掩盖前几轮的失败。
4. **pid 必须唯一才采用**。多进程应用（`:push`、`:remote`）`pidof` 返回多个 pid，
   取错进程的显存比不取更糟 → 一律降级（`pid_ambiguous`）。同理只认
   `Proc <pid> total:`，绝不取 `Global total`（那是全机之和）。
5. **报告必须写出实际数据源**。`kgsl_gpubusy` 是驱动 ~1 s 窗口的累计值（窗口
   平均），`dumpsys_gpu` 是瞬时读数，口径不同、混看会误判。报告 GPU 小节显式
   标注数据源与"只报聚合"的限定；同图按**单位**分组各占一条轴（°C / % / MB
   挤在一条轴上会误读量级）。

**验证**：unit+contract 549 passed（含新增 20 项：`parse_gpubusy` 四分支、
`parse_gpu_proc_mem_mb`、`parse_pidof`、`split_sections`、降级链三级、
`gpu_mem_mb` 四种降级、报告 GPU 小节三例）。ruff 全绿。

**未覆盖 / 已知限制**：
- **真机验收未做**：2026-10-26 本机设备已断开（USB 与 `192.168.0.101:5555`
  均不可见），`tests/device/test_supplement_device.py` 已按 kgsl 可读/不可读
  两条分支改写（可读则断言有非零值且 `gpuSource=kgsl_gpubusy`），设备回连后
  跑 `pytest -m device` 即可补齐这一项。
- 合成链路验证（FakeSupplementAdb 走完整 `run_collect`）：`gpuSource=kgsl_gpubusy`、
  `gpu_util_pct` median 25.0、`gpu_mem_mb` median 125.37MB、报告 GPU 小节渲染
  含"窗口平均"限定，均符合预期。
- `gpubusy` 的窗口与采样时刻存在错位（采样间隔 2s、驱动窗口约 1s），只报聚合
  是对这一错位的如实处理，不是精度提升。
