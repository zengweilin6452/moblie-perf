# 16: GPU 双源补采与显存指标

**What to build:** 修掉 M3 遗留的 GPU 空缺（工单 10 记录"本机型实测不可用 → 永久 not_tested"）。实测发现 `cat /sys/class/kgsl/kgsl-3d0/gpubusy` 在 shell 用户下**可读**（无需 root），输出 `busy total` 二元组，`total==0` 表示该窗口 GPU 空闲，比值即窗口平均占用率。据此：

①`gpu_util_pct` 改为三级降级链——kgsl `gpubusy` 比值 → `dumpsys gpu` 文本解析（原实现）→ `not_tested`，每级降级原因如实记录；
②新增 `gpu_mem_mb`（应用 GPU 显存）：`dumpsys gpu` 输出中 `Proc <pid> total:` 的字节数换算，pid 由目标包名解析；
③两者并入 `supplement` 现有轮询线程（一次 adb 事务多条命令），不新增采集通道。

口径约束：`gpubusy` 是驱动侧约 1 s 滚动窗口的累计值（实测 total 在 1.00e6–1.04e6 间浮动且会回落），**只报聚合（median/p90/max），不承诺逐点序列对齐**，模块文档写明。

**Blocked by:** 15（复用其轮询线程与降级记录扩展点）

**Status:** done（2026-10-26，两台真机验收通过）

- [x] `gpubusy` 解析器纯函数：正常值、`0 0`（空闲）、`total=0`、格式异常四种分支 unit 覆盖
- [x] 三级降级链逐级可测：kgsl 不可读 → 落 dumpsys gpu → 仍不可得 → `not_tested` + 具体原因
- [x] `gpu_mem_mb` 按 pid 取到目标进程显存；pid 解析失败时 `not_tested` 而非取错进程
- [x] 真机验证：滑动负载下 `gpu_util_pct` 有非零值，与 M5 的"永远 not_tested"形成对比
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

### 2026-10-26 真机验收（Redmi 25102RKBEC / Adreno840v2 / Android 17，`84cb2c4e`）

前一轮"设备离线"的缺口已补。`tests/device/test_supplement_device.py` 的两个纯补采用例
在真机上通过（`test_supplement_poller_round_on_real_device`、
`test_gpu_mem_tracks_target_process_on_real_device`）：

| 项 | 实测值 |
|---|---|
| `gpu_source` | `kgsl_gpubusy`（第一级源命中，未降级） |
| `degraded` | `[]`（三级降级链一级都没触发） |
| `gpu_util_pct` | 有值：中位 ~23%、P90 23.63%、最大 33.76%（另一轮 4 样本 7.14 / 23.11 / 30.32 / 29.65） |
| `gpu_mem_mb` | 有值：8.33 MB（另一轮 77.91 MB；随目标进程当前占用变化） |
| `gpu_mem_mb` 独立核对 | `dumpsys gpu` 中 `Proc 15547 total: 8732672` 与目标 pid 一致，非 `Global total` |
| `temp_battery_c` | 37.5 °C |

**同时发现一个必须记录的跨机型差异**（详见 M6 方案 §2.1a）：本机型上
`gpubusy` 比值**不随负载单调变化**——静止桌面稳定 ~49.3%（3 轮 55 次采样，0 次
`0 0`），App 内持续滑动反而 ~26%（4 轮 75 次采样，17 次 `0 0`）。与 749622c9
（Adreno 730 / Android 14：静止 `0 0`、滑动 7%–35%）语义相反。因此：

- 比值口径（窗口内平均占用）仍成立，**绝对值不可跨机型比较**，阈值必须按机型建档；
- `0 0` 不是跨机型的"空闲"证据，在重负载中也会瞬时读到；保留 `0 0 → 0%`
  （工单 16 既定语义）记为**已知低估偏差**，靠聚合与样本量吸收；
- 本机型 `ls /sys/class/kgsl/kgsl-3d0/` 也 Permission denied（只能按固定路径 `cat`），
  降级链不能靠列目录发现新节点。

已同步写入 `docs/设计方案-M6-GPU与弱网.md` §2.1a / §10 风险表、`supplement.py`
模块文档与 `parse_gpubusy` docstring、报告 GPU 小节提示文案、`GLOSSARY.md`
「GPU 窗口占用率」。

**本轮未覆盖**：`test_supplement_ingestion_full_loop_on_real_device`（补采 + SoloPi
入库全链路）在本机 `84cb2c4e` 上失败于 `perf-start 失败: 所选设备未安装 SoloPi`——
该机型没有装自编译 SoloPi（749622c9 上装过）。这是**设备环境缺口，不是代码缺陷**：
补采本身的两条真机用例都通过，入库链路由 unit/contract 的合成链路用例覆盖。
