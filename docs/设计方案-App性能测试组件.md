# App 性能测试组件设计方案（v0.3）

> 目标：基于本仓库 SoloPi 完整版二开，交付**可嵌入测试平台的 App 性能测试组件 `mobile_perf`**。
> 范围变更：v0.1 的"UI 自动化 + 投屏 + 平台"整体移交 `D:\workspace\claudecode-test\moblie-auto`（已实现），本项目只做性能。
> 定稿日期：2026-10-25 ｜ 状态：v0.3，经两轮盘问（20 项决策）全部确认的共识版
> 术语以 [GLOSSARY.md](../GLOSSARY.md) 为准；关键架构决策见 `docs/adr/0001~0003`。

---

## 1. 背景与范围

### 1.1 两个项目的分工（关键边界）

| 能力 | 归属 | 现状 |
|---|---|---|
| UI 自动化（uiautomator2 驱动、15 异步方法） | **moblie-auto** | 已实现（`src/mobile_auto/core/`） |
| scrcpy 投屏/网页控制 | **moblie-auto** | 已实现（`stream/` + `viewer/`） |
| 设备并发/租约（跨进程文件锁） | **moblie-auto** | 已实现；本组件以 **DeviceGate 协议对等参与**，不 import（ADR-0001） |
| 失败证据、pytest 钩子 | **moblie-auto** | 已实现，本组件提供对称钩子形态 |
| **App 性能采集（SoloPi 五项指标）** | **本项目（moblie-xn）** | SoloPi 全链路已在 Windows 实测 |
| **采集服务化、归档、基线门禁、趋势** | **本项目** | 本方案范围 |
| **SoloPi App 二开（无人值守改造）** | **本项目** | 唯一硬二开点：FPS 告知弹窗（ADR-0003） |

### 1.2 本项目目标

交付 Python 组件包 `mobile_perf`（工程形态对齐 moblie-auto）：

1. **库 API**：宿主同进程 import，发起/停止/查询采集会话；
2. **CLI**：人工排查与脚本化（现有 `solopi-ai.cmd` 保留为薄入口）；
3. **无人值守**：弹窗免重复（App 二开）+ 连接/无障碍自愈巡检；
4. **数据闭环**：自动归档 + 元数据 + 基线对比 + 阈值门禁（三态）+ 趋势；
5. **联动协议**：宿主 UI 操作与性能采集同 run 对齐，产出联合报告。

### 1.3 非目标

UI 自动化、投屏、设备管理平台、Web 前端（由 moblie-auto / 未来平台承载）；iOS；独立 daemon 部署。

### 1.4 已锁定的边界决策（盘问 Q1–Q20）

| # | 决策 | 落点 |
|---|---|---|
| Q1 | 零依赖宿主，DeviceGate 依赖反转，Python ≥3.10 | ADR-0001 |
| Q2 | ADB 三级解析：显式参数 > 环境变量 > 随包默认（本仓库 platform-tools 37.0.1） | §10 |
| Q3 | SQLite 默认 `<repo>/data/`，WAL + busy_timeout=5s；xdist 推迟 M4 | §10 |
| Q4 | 自动清理：pull 校验后删设备端目录；本地保留 200 run / 30 天 | §10 |
| Q5 | `metrics.py` 指标字典为唯一中文列名翻译层 | §6.4 |
| Q6 | 基线绑定设备档案（机型+OS 大版本），不匹配 → not_tested | §5 |
| Q7 | Judge 纯函数化：阈值归宿主，口径归组件 | §6.3 |
| Q8 | 报告 = 单文件 HTML（离线 ECharts）+ 同内容 JSON | §6.3 |
| Q9 | 自编译 APK 单活线，一次性切换 | ADR-0003 |
| Q10 | 弹窗免重复开关 adb 可读写（scheme/broadcast 通道） | §4 #1 |
| Q11 | keeper 内聚设备可用性；弹窗自动点独立模块、默认关 | §3 |
| Q12 | 充电时 Battery/温度项 not_tested 降级；每会话校准时钟偏移 | §6.2 |
| Q13 | Harness 子进程进程边界集成 | ADR-0002 |
| Q14 | 租约按文件协议自实现 + 契约测试防漂移 | ADR-0001 |
| Q15 | DeviceGate 短事务语义（三模式） | §6.1 |
| Q16 | "采集命令不改变被测应用前台"为 M4 专项验收 | §8 M4 |
| Q17 | 指标命名法 `<对象>_<指标>_<单位>` + 初版字典 | §7 |
| Q18 | 上游基线 tag + 最小 diff + 季度评估同步 | §10 |
| Q19 | 测试三段式：unit / contract / device（镜像 moblie-auto） | §10 |
| Q20 | ADR×3 | `docs/adr/` |

---

## 2. 开源调研结论（性能相关收敛版）

| 项目 | 与本组件的关系 |
|---|---|
| **SoloPi**（蚂蚁，本仓库） | 采集内核：真机端侧五项指标（CPU/FPS/内存/网络/电池）、无需 root、AI Harness 纯标准库 |
| **Perfara**（github.com/codinghiker/perfara） | 数据层/可视化对标：私有化性能数据平台的指标口径与归档设计参考 |
| **perfcat**（github.com/UncleNeil/perfcat） | 补采参考：温度/GPU 等 SoloPi 未覆盖指标走 PC 侧 `dumpsys` 补采 |
| **Sonic 性能监控** | 口径对标（系统/进程 CPU、内存、FPS）；验证了 agent 侧 dumpsys 轮询路线可行 |
| **Fastbot**（字节） | 生态位预留：稳定性遍历产出 crash 率，可消费本组件的 crash/ANR 采集（后期） |
| **mitmproxy** | 弱网位预留（后期，不进当前里程碑） |

**结论**：端侧采集用 SoloPi（差异化资产），平台粘合不重造。

---

## 3. 总体架构

```
┌─────────────────────── 宿主进程（三选一）───────────────────────┐
│ ① moblie-auto 用例（pytest 同进程）  ② 未来测试平台  ③ CLI/脚本  │
│        │  import mobile_perf（库 API / pytest 钩子）             │
│        │  宿主注入：adb_path、serial、host_run_id、DeviceGate     │
├────────▼────────────────────────────────────────────────────────┤
│                mobile_perf 组件包（本项目的全部产出）              │
│  ├─ kit.py        PerfKit：门面，组装下列模块                     │
│  ├─ gate.py       DeviceGate 默认实现：moblie-auto 锁文件协议     │
│  │                对等参与（短事务；非 Windows passthrough+警告） │
│  ├─ solo.py       Harness 适配层：唯一知道 harness 的地方         │
│  │                （subprocess 调 CLI，JSON stdout 契约，ADR-0002）│
│  ├─ session.py    采集会话生命周期（含打标、时钟偏移校准）          │
│  ├─ keeper.py     设备可用性：doctor 巡检、tcpip 自愈、            │
│  │                无障碍检查、充电/前台检查（按需触发，不持设备权）  │
│  ├─ dialog.py     弹窗自动点（备选路径）：独立模块、默认关、显式开关 │
│  ├─ collector.py  自动 pull、归档、元数据；pull 校验后清理设备端   │
│  ├─ supplement.py PC 侧补采：温度/GPU（dumpsys 轮询，对时）        │
│  ├─ logwatch.py   crash/ANR 监听，与会话时间轴对齐                 │
│  ├─ metrics.py    指标字典：canonical key ↔ 中文列名 唯一映射      │
│  ├─ judge.py      门禁：纯函数，JudgeSpec → 三态 Verdict + 证据    │
│  ├─ report.py     报告聚合：单文件 HTML（离线 ECharts）+ JSON      │
│  ├─ store.py      SQLite（WAL）聚合指标 + runs/ 文件归档 + 清理    │
│  └─ cli.py        命令行：doctor / collect / report / trend       │
├──────────────────────┬───────────────────────────────────────────┤
│  SoloPi Harness      │  SoloPi App（真机常驻，本项目二开对象）      │
│  （子进程调用，零修改）│  PermissionDialogActivity 弹窗免重复改造    │
└──────────────────────┴───────────────────────────────────────────┘
```

要点：

- **依赖方向单向**：mobile_perf 不知道 moblie-auto 的存在；耦合点 = moblie-auto 侧一个 DeviceGate 适配文件 + 两边共守的锁文件协议（契约测试锁死）；
- **Harness 子进程边界**：适配集中在 `solo.py`，锁定快照版本，上游升级只回归它；
- **采集进行中不持有设备权**：会话 recording 期间 PC 侧空闲，UI 自动化可同时工作；
- **SoloPi 内部 adb 走手机 tcpip 5555**，与 PC adb server 相互独立。

---

## 4. SoloPi 二开与工程清单

原则：**能放 mobile_perf 层的不动 SoloPi 源码**。

| # | 事项 | 层 | 方案 | 涉及位置 | 风险 |
|---|---|---|---|---|---|
| 1 | FPS 告知弹窗每次必弹 | **App（唯一硬二开）** | 告知型权限加"本机不再提示"（SP 持久化）；**提供 adb 可读写通道**（既有 scheme 加动作或 broadcast），keeper 在 ensure_ready 时确认开关已开 | `PermissionDialogActivity.java`、`PermissionUtil.java`、scheme Resolver | 中 |
| 2 | CSV 中文文件名 | App（顺手项） | 落盘英文名开关（列名不动，列名由指标字典翻译） | `PerformanceSchemeResolver.java` | 低 |
| 3 | `adb tcpip 5555` 重启失效 | mobile_perf | keeper：doctor 失败 → 自动补 tcpip → 重连 → 重试 | `keeper.py` | 低 |
| 4 | MIUI 无障碍"假开启" | mobile_perf | `dumpsys accessibility` 检测 Bound 空 → 设备标 `needs_attention` + 事件回调（无法代开，如实暴露） | `keeper.py` | 低 |
| 5 | 手动 pull、无元数据 | mobile_perf | collector：stop 后自动 pull → 校验 → 归档 → 清理设备端 | `collector.py` | 低 |
| 6 | 无基线/门禁 | mobile_perf | judge：纯函数三态 + 设备档案严格匹配 | `judge.py` | 中 |
| 7 | crash/ANR | mobile_perf | logcat 监听入库，时间轴对齐 | `logwatch.py` | 中 |
| 8 | 温度/GPU 缺位 | mobile_perf | PC 侧 dumpsys 补采（对时后合并） | `supplement.py` | 中 |
| 9 | App 构建 | 工程 | JDK 11（**已装**：D:\Java，JAVA_HOME 已配）+ 一键 `build.cmd`；自编译单活线切换脚本（ADR-0003） | `solopi-app/` + 新增脚本 | 中（老构建链） |
| 10 | 联动 | mobile_perf | pytest 钩子 `--mobile-perf` + 联合报告 + gate 契约互操作测试 | 新增 | 中 |

### #1 的备选路径（App 改造受阻时）

`dialog.py` 检测 `PermissionDialogActivity` 抢前台 → `adb shell input tap` 自动点"我知道了"（坐标一次标定）。缺点：分辨率相关、代点人工确认与 Harness 哲学略冲突，独立模块 + 默认关，仅降级用。

---

## 5. 数据模型（SQLite，`<repo>/data/mobile_perf.db`，WAL）

```
devices        serial, model, os_version, device_profile, status(online/needs_attention/offline), health_json, updated_at
sessions       id, solo_session_id, serial, device_profile, target_pkg, target_version, items[],
               started_at, stopped_at, duration_s, sample_count, host_run_id, clock_offset_ms, status
metrics        session_id, metric_key, unit, min, median, mean, max, p90, source(solo_pi/pc_supplement)
events         session_id, ts(统一时间轴), kind(crash/anr/thermal_warning/dialog/gate), detail_json
baselines      name, target_pkg, device_profile, metric_key, value(median), source_session_id, created_at
thresholds     —— 不入库（宿主 JudgeSpec 显式传入，组件不持有业务阈值）
runs_artifacts run_id, dir_uri
```

- **baseline 绑定 device_profile**（机型+OS 大版本）：judge 严格匹配，不匹配 → `not_tested`；跨 profile 趋势查询默认拒绝、显式传参覆盖并标记口径不一致；
- 原始 CSV 永不入库，聚合行入库（min/median/mean/max/p90）。

---

## 6. 接口契约

### 6.1 DeviceGate 协议（ADR-0001）

```python
class DeviceGate(Protocol):
    def borrow(self, serial: str, *, purpose: str, timeout_s: float = 10) -> ContextManager[None]: ...
    # 语义：短事务借用——只在发起设备命令的短窗口持有，采集进行中不持有设备权
```

三种工作模式：

1. **独立 CLI 模式**（默认实现 `gate.py`）：按 moblie-auto 锁文件协议对等参与——`~/.mobile-auto/leases/<sha256(serial)[:24]>.lock` 的 msvcrt 1 字节区域锁 + holder.json 写 pid/hostname/purpose；ensure_ready / start / stop / pull 各为一个短事务，完成即还；
2. **嵌入模式**：宿主注入 gate；moblie-auto 侧适配器发现 holder.json 的 pid 为本进程则重入放行（宿主自身已保证序列化），否则短取；
3. **非 Windows**：文件锁不可用 → passthrough + warning 事件（平台 Windows 优先）。

获取失败 → `GateBusyError` fast-fail，不内置等待重试（重试是宿主策略）。

### 6.2 库 API（主形态）

```python
from mobile_perf import PerfKit, SessionSpec, JudgeSpec

kit = PerfKit(adb_path=None, serial="749622c9", gate=None)   # adb/gate 未传则走默认解析/默认 gate
kit.ensure_ready()                    # keeper：doctor + tcpip 自愈 + 无障碍/充电/前台检查 + 弹窗开关确认（幂等）

spec = SessionSpec(target="tv.danmaku.bili", items=["CPU","FPS","Memory","Network","Battery"])
with kit.collect(spec, host_run_id="pytest-20260101-001") as s:
    s.mark("scroll_home_done")        # 打标与事件共用统一时间轴（会话开始校准 device-host 偏移，>2s 记 warning）
    ...
report = s.finalize()                 # 自动 pull/校验/归档/聚合/补采合并/清理设备端
verdict = kit.judge(report, JudgeSpec(baseline="bili-home-v3",
               thresholds={"mem_pss_mb": ("lt", 220, "fail"), "fps_frame": ("gt", 50, "fail")}))
```

- 充电状态：Battery/温度项标记 `not_tested` 降级，其余照采；
- 判定：样本数不足 / profile 不匹配 / 前置不满足 → `not_tested`；阈值效果 fail/warn 分级。

### 6.3 报告与产物

每个 run 双产物：单文件 HTML（内嵌离线 ECharts，无 CDN 依赖）+ 同内容 JSON；趋势接口 `kit.trend(pkg, metric, profile=...)` 供宿主绘图。三源数据（SoloPi / 补采 / 事件）在同一时间轴上呈现。

### 6.4 指标字典（metrics.py）

canonical key 为全组件唯一词汇（judge/report/store/CLI/宿主只见 canonical key）；中文原始列名的映射只存在于此。报告展示用字典的双语标签。

**派生指标（同源统计）**：不映射 SoloPi 原始列、由同源数据二次统计得到的指标，source 沿用来源（如 `solo_pi`），在字典中登记 canonical key、双语标签与单位，并在模块文档写明口径。卡顿分级即此类（见 §7 附注）。

### 6.5 pytest 钩子（对称 moblie-auto 的 `--mobile-evidence`）

```python
def test_home_scroll(device, perf):          # perf fixture 由 --mobile-perf 注入
    with perf.session("tv.danmaku.bili", items=[...]) as s:
        device.launch_app(...); device.swipe_up(5)
        s.mark("scroll_done")
    # 用例结束自动 finalize + judge（按用例标记的基线），结论进报告与 JUnit 附加
```

### 6.6 CLI

```
mobile-perf doctor [--serial]                 # 巡检+自愈（短事务 gate）
mobile-perf collect <pkg> --items CPU,FPS,Memory --duration 180
mobile-perf report <runId> [--baseline name]
mobile-perf trend <pkg> --metric mem_pss_mb
```

`serve` HTTP 模式仅预留接口不实现（回环 + gate 门控，形态对齐 moblie-auto viewer）。

安全基线沿用 Harness 哲学：无任意 shell 透传；高危动作显式确认；三态结论。

---

## 7. 指标字典初版（M1 对真实 CSV 校准后定稿）

命名法：`<对象>_<指标>_<单位>`；聚合值（min/median/mean/max/p90）是字段不是 key。

| canonical key | SoloPi 原始列（示例） | 来源 | 单位 |
|---|---|---|---|
| `cpu_process_pct` | 应用进程-main-\<pid\>_CPU | solo_pi | % |
| `cpu_global_pct` | 全局占用_CPU | solo_pi | % |
| `mem_pss_mb` | PSS-main | solo_pi | MB |
| `fps_frame` | 帧率_FPS | solo_pi | fps |
| `frame_delay_max_ms` | 最长延迟时间 | solo_pi | ms |
| `net_rx_kbps` / `net_tx_kbps` | 全局下行/上行速率 | solo_pi | kbps |
| `battery_pct` | 电量列（校准） | solo_pi | % |
| `battery_current_ma` | 电流列（校准） | solo_pi | mA |
| `temp_battery_c` | — | pc_supplement | °C |
| `gpu_util_pct` | — | pc_supplement | % |
| `app_jank_light_count` | 派生自 最长延迟时间(ms) | solo_pi | 次 |
| `app_jank_heavy_count` | 派生自 最长延迟时间(ms) | solo_pi | 次 |
| `app_jank_severe_count` | 派生自 最长延迟时间(ms) | solo_pi | 次 |

**附注：卡顿分级口径与设计偏差（ticket 14，对真实 CSV 校准）**

- 真实 SoloPi FPS 采集只有 `帧率(帧)` / `延迟次数(次)` / `延迟占比(%)` / `最长延迟时间(ms)` 四列；`最长延迟时间` 是每个采样窗口（约 1 s）的单值，**不存在逐帧延迟分布**。
- 因此分级实现为"会话内超阈值窗口次数"：每个窗口的最长延迟严格大于阈值即计该档一次，分桶互斥（只计最高档）；默认 >100 ms 轻、>300 ms 重、>700 ms 严重，阈值归宿主（`collect --jank-threshold` 或 `jank_thresholds_ms` 参数，不入库，随 meta.json 记录）。
- 与"逐帧卡顿分级"的偏差如实保留：窗口级计数会低估同一窗口内的多次卡顿；若上游将来在 SoloPi 端侧增加逐帧分布列（低成本二开位），再升级口径。

---

## 8. 里程碑

### M1 · `mobile_perf` 组件包 MVP（约 2 周）
- 范围：包骨架（src 布局 + pytest + ruff）；`gate / solo / session / collector / metrics / store / report / cli`；指标字典对真实 CSV 校准；数据保留策略。
- 验收：`mobile-perf collect tv.danmaku.bili --duration 180` 一条命令完成 采集→校验→归档→报告（HTML+JSON）；连续两次运行可见趋势；真机 749622c9 实测；gate 与真实 DeviceLease 互抢的契约测试通过。
- SoloPi 改动：零。

### M2 · 无人值守加固（约 2-3 周；JDK 11 已就绪）
- 范围：App 二开 #1（弹窗免重复 + adb 读写通道）+ #2；一键构建与单活线切换脚本；`keeper`（tcpip 自愈/无障碍巡检/充电/前台/开关确认）；`logwatch` crash/ANR；时钟偏移校准。
- 验收：含 FPS 的采集无人交互完成；手机重启后 `ensure_ready()` 自动恢复；人为制造 crash 被记录且与时间轴对齐；自编译 APK 在真机稳定替换官方版。

### M3 · 门禁与补采（约 2 周）
- 范围：`judge`（三态 + 设备档案匹配）；`supplement`（温度/GPU）；三源同时间轴报告。
- 验收：B 站设阈值后产出 pass/fail 结论与证据引用；温度曲线与帧率曲线同图对齐。

### M4 · 嵌入联调（约 1-2 周）
- 范围：pytest 钩子；与 moblie-auto 联动实测（UI 驱动 + 采集同 run）；**前台干扰专项**（HarnessSession 默认可能拉起 SoloPi 抢前台——探明规避手段）；pytest-xdist 并发验证；接入文档。
- 验收：moblie-auto 仓库内跑通"UI 滑动 + 性能采集 + 门禁结论"用例，全程无人工干预；**采集命令执行前后被测应用前台状态不变**；文档齐。

---

## 9. 风险与对策

| 风险 | 对策 |
|---|---|
| Harness import 副作用（sys.path 注入、顶级模块名冲突） | 已规避：子进程边界（ADR-0002） |
| HarnessSession 默认拉起 SoloPi 抢前台 | M4 专项验收 + 探明免拉起手段（§8 M4） |
| 锁文件协议随 moblie-auto 演进漂移 | tests/contract 对真实 DeviceLease 互抢测试 |
| SoloPi 无障碍服务与 uiautomator2 共存性 | M4 联调专项；必要时采集命令窗口内协调 |
| App 二开老构建链（Gradle 6.1.1） | JDK 11 已装；一键脚本 + 最小 diff（§10） |
| 阈值口径误报 | judge 只认 median + 显式窗口 + 样本下限，口径入 baseline 元数据 |
| 联动时 SoloPi 拉起导致数据脏 | 前台不变验收 + 打标/事件时间轴可事后剪裁 |

---

## 10. 工程约定

- **Python**：≥3.10（在宿主 3.12 下测试）；包管理 `pip install -e`（源码交付，对齐 moblie-auto）。
- **ADB 解析**：显式参数 > `MOBILE_PERF_ADB`/`ADB` 环境变量 > 随包默认（`SoloPi/platform-tools`，37.0.1）；宿主换 adb 版本需自行保证兼容。
- **存储**：SQLite 默认 `<repo>/data/mobile_perf.db`，WAL + `busy_timeout=5s`；MVP 同进程会话串行，xdist 到 M4 验证。
- **数据保留**：设备端 pull 校验（行数一致）后删该会话目录，不一致保留并告警；本地保留最近 200 run 或 30 天（可配置）。自动清理仅限本组件产生的数据。
- **测试三段式**（镜像 moblie-auto）：`tests/unit`（不碰 adb）/ `tests/contract`（gate 协议、harness CLI 输出形状、指标字典映射）/ `tests/device`（`-m device` 门控，无设备不计为通过）。
- **上游同步**：首次 commit 给 SoloPi 基线打 tag；二开全在本仓库分支、保持最小 diff 便于 rebase；季度评估同步 master（harness 侧因进程边界只剩契约测试成本）。
- **统计口径**：median + 显式窗口 + 样本数下限，归组件；阈值归宿主。

---

## 11. 参考

- 决策记录：`docs/adr/0001-component-boundary-device-gate.md`、`0002-harness-subprocess-boundary.md`、`0003-self-built-apk-single-line.md`；术语：`GLOSSARY.md`
- 关联项目：`D:\workspace\claudecode-test\moblie-auto`（UI 自动化/投屏/租约/证据，Python 3.12）
- 调研来源：Perfara、perfcat、Sonic 性能监控（testerhome.com/topics/35387）、Fastbot、mitmproxy
- 本仓库实测资产：`SoloPi/README.md`（全链路工作流与踩坑实录）、`perf-output/`（首份基线数据）
- 版本历史：v0.1（平台版，作废）→ v0.2（性能收敛版）→ **v0.3（盘问共识版：20 项决策、ADR×3、指标字典、gate 协议、时钟/充电/清理/前台治理）**
