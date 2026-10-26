# M6 设计方案：GPU 补采 · 弱网编排 · 整页与接口加载耗时

> 前置：M1–M5 已完成（14 张工单，426 unit/contract 全绿）。本文只描述 **M6 增量**。
> 术语以 [GLOSSARY.md](../GLOSSARY.md) 为准；新术语在 §11 登记。
> 实测环境：真机 `749622c9`（Xiaomi 2206123SC / Adreno 730 / Android 14），被测 `com.sinosoft.trainapp` v1.9.77（targetSdk 36）。
> 定稿日期：2026-10-25；实施收口（工单 15–20 全部落地）：2026-10-27。

---

## 1. 目标与不做什么

### 1.1 目标

M5 之后仍有三处空缺，本里程碑一次补齐：

| 空缺 | M5 现状 | M6 目标 |
|---|---|---|
| GPU | `gpu_util_pct` 走 `dumpsys gpu` 文本解析，本机型解析不到 → 永久 `not_tested` | 换可读数据源拿到 GPU 利用率；另补 **每进程显存**（新指标） |
| 卡顿 | 只有窗口级口径（SoloPi CSV 无逐帧分布，工单 14 已记录偏差） | 用 `dumpsys gfxinfo framestats` 拿**真逐帧**分布，两套口径并存 |
| 网络 | 只有全局上下行速率与累计流量（SoloPi 端侧） | **弱网可编排** + **整页加载耗时** + **接口级明细**（解密档） |

### 1.2 不做

- 不做弱网下的协议/内容断言（那是接口测试的活）；本里程碑只产出**耗时与流量**指标。
- 不引入常驻 daemon；弱网代理随采集会话起停。
- 不改 SoloPi（本里程碑零 App 二开）。

---

## 2. 实测可行性（先证后设计）

以下全部为本机真机实测结论，不是文档推测。

### 2.1 GPU 利用率：`kgsl` 窗口计数器可读（无 root）

```
$ adb shell cat /sys/class/kgsl/kgsl-3d0/gpubusy
  280749 1008311      ← (busy, total)
```

- shell 用户（uid 2000）**可读**，无需 root；`gpu_busy_percentage` / `devfreq/*` 则 Permission denied。
- `total` 在 1.00e6–1.04e6 之间浮动且**会回落**，说明它是驱动侧的**滚动窗口累计值**（约 1 s 窗），不是单调累加器。
- 结论：`gpu_util_pct = busy / total × 100`。口径是"驱动窗口内的平均占用率"，**不是瞬时值**，因此只报聚合（median/p90/max），不承诺逐点序列对齐。

#### 2.1a 按机型实测的差异（2026-10-26 两台真机对照）

同一套 `cat` 命令在两台机器上给出的**不是同一套语义**，必须按机型记录：

| 机型 / GPU / 系统 | 静止桌面（Awake） | App 内持续滑动 | 熄屏 | `0 0` 的含义 |
|---|---|---|---|---|
| Xiaomi 2206123SC · Adreno 730 · Android 14（`749622c9`） | `0 0` | 7%–35% | — | 该窗口 GPU 空闲 |
| Redmi 25102RKBEC · Adreno840v2 · Android 17（SDK 37，`84cb2c4e`） | 49.3%（3 轮 n=55，48.5–52.7，**0 次** `0 0`） | 26%（4 轮 n=75，单轮中位 25.9–27.1，**17 次** `0 0`） | 基本 `0 0`（33/35） | **既可能真空闲，也可能在重负载中瞬时读到** |

（84cb2c4e 的采样方法：固定 1.2 s 间隔、每轮 15–20 次，滑动轮每次采样前发一条
`input swipe`，并同时读 `dumpsys power` 的 `mWakefulness` 确认屏幕状态——有一轮
"桌面"读数 4.9%/17 次 `0 0` 就是唤醒未完成导致的，已剔除，不进入上表。）

由此得到三条必须在实现与文档里同时写明的结论：

1. **比值本身仍成立，绝对值不能跨机型比较。** 两台机器窗口都稳定在 ~1.00e6，
   `busy/total` 确实是窗口内平均占用；但同一种"重负载滑动"在 749622c9 上是
   7%–35%、在 84cb2c4e 上是 26% 左右，而 84cb2c4e 的**静止桌面（49.3%）反而高于
   滑动（26%）**——占用率由机型、刷新率、合成负载共同决定，**不能拿一台机器的
   阈值去套另一台**，也不能把"数值变大"直接读成"负载变重"。
2. **"静止时读回 `0 0`"不是跨机型规律。** 749622c9 静止读 `0 0`；84cb2c4e 静止桌面
   连续 55 次采样一次 `0 0` 都没有，反倒是滑动中偶发 `0 0`。因此 `0 0` 不能当作
   "GPU 空闲"的跨机型证据。
3. **`0 0` 在负载中也会出现 → 映射成 0% 会低估。** 84cb2c4e 滑动期间 75 次采样有
   17 次读到 `0 0`（`parse_gpubusy` 记为 0%）。当前实现保留 `0 0 → 0%`
   （工单 16 的既定语义，在 749622c9 上正确），把这条记为**已知口径偏差**：
   样本量大时中位数影响有限（剔除这 17 个样本后单轮中位 27.3% vs 含它们 26.5%），
   但短会话若连续读到 `0 0` 会明显偏低。要收敛需改成"`0 0` 不计入分布"，
   那会改动已验收语义、需要更多机型证据，不在本里程碑擅改。

**可读节点也按机型不同**（`84cb2c4e` 上实测）：

| 节点 | `749622c9` | `84cb2c4e` |
|---|---|---|
| `gpubusy` | 可读 | 可读 |
| `gpu_model` | 可读 | 可读（`Adreno840v2`） |
| `gpu_busy_percentage` | Permission denied | Permission denied |
| `gpuclk` | Permission denied | Permission denied |
| `gpu_available_frequencies` | Permission denied | Permission denied |
| `devfreq/*` | Permission denied | 路径不存在 |
| `ls /sys/class/kgsl/kgsl-3d0/` | 可列 | **Permission denied**（只能按已知路径 `cat`） |

最后一行意味着**不能靠列目录去发现新节点**——降级链只能用固定路径探测，
新增数据源必须先确认该机型路径可读。

### 2.2 GPU 显存：`dumpsys gpu` 按 pid 可读

```
$ adb shell dumpsys gpu | grep 'Proc 23286'
Proc 23286 total: 131457024        ← 目标进程 125.4 MB
```

`Global total` 与逐 `Proc <pid>` 均可读。可按目标包名 → pid 取到**应用级显存**，这是 M5 完全没有的维度。

### 2.3 逐帧数据：`gfxinfo framestats` 可用

```
$ adb shell dumpsys gfxinfo com.sinosoft.trainapp reset   # 清零窗口
$ adb shell dumpsys gfxinfo com.sinosoft.trainapp framestats
Flags,FrameTimelineVsyncId,IntendedVsync,Vsync,InputEventId,HandleInputStart,...
1,1092866,18612958382881,18613090241969,...,18613210186856,...
```

- 21 列逐帧时间戳（纳秒），`FrameCompleted - IntendedVsync` 即该帧总耗时；`reset` 后读回只含本窗口帧（实测 reset → 81 帧，符合预期）。
- 目标 App targetSdk 36，framestats 未被裁剪。
- `dumpsys SurfaceFlinger --latency <layer>` 在本机型返回**全 0 帧**（无有效时间戳），**不作为数据源**。

### 2.4 弱网：系统代理生效，但 TLS 解密被证书挡住

**（a）代理链路成立**——`adb reverse` + `settings put global http_proxy` 后，目标 App 的流量确实进了代理：

```
[20:36:51.054] server connect d-lms-sg-uat.byd.com:443 (47.84.92.63:443)   ← 十余条并发
[20:36:51.317] Client TLS handshake failed. The client does not trust
              the proxy's certificate for d-lms-sg-uat.byd.com
```

- `adb reverse tcp:8080 tcp:8080` 可用；`settings put global http_proxy` 可写可清（`:0` 恢复）。
- 反向验证：把代理指向**不存在的端口** 9999，App 首屏仍完整加载（截图确认），说明该 App 有本地缓存/多域名兜底，**不能用"页面能否打开"判断代理是否生效**——必须看代理侧连接日志。

**（b）解密档被证书信任链挡住**——App targetSdk 36：

- Android 7+ 起 `targetSdk ≥ 24` 的 App **默认不信任用户 CA**，只信任系统 CA；
- 装系统 CA 需 root 或可写 system 分区，本机 `ro.build.type=user`、无 Magisk；
- 因此**本机型 + 本 App 无法解密 TLS**，接口级明细（URL/状态码/响应体）在当前条件下拿不到。

这不是实现缺陷，是平台约束。方案据此分成**两档**（§6），并让"拿不到"如实降级为 `not_tested` 而不是假装有数据。

### 2.5 其他实测结论

| 项 | 结论 |
|---|---|
| `adb exec-out screencap -p` | 0.45 s/次，可用但太重，**不用于**逐帧或整页判定 |
| `dumpsys netstats detail` | 可按 uid 取字节数与 7200 s 分桶，粒度太粗，仅作交叉校验 |
| `tcpdump` | 设备无该二进制，排除 |
| mitmproxy | PyPI 12.3.0 / 11.1.3 均要求 **Python ≥3.12**（项目运行时 ≥3.10）→ 只能作**可选依赖**，独立 venv 安装 |
| 设备 udp | 全量 `ss -uan` 中除 DHCP 外无对外 UDP 连接，未见 QUIC；App 的对外连接均为 TCP/443，代理路径暂无 UDP 绕过问题（后续换 App 需重测） |

---

## 3. 设计决策

### D1 · GPU 走"双源 + 双指标"

- `gpu_util_pct`（已有 key）：数据源优先级 `kgsl gpubusy` 比值 → `dumpsys gpu` 文本 → `not_tested`。降级链逐级记录原因。
- 新增 `gpu_mem_mb`（每进程显存）：`dumpsys gpu` 的 `Proc <pid> total` 字节数换算。
- 两者随 `supplement` 同一轮询线程采样（一次 adb 事务多条命令），**不新增采集通道**。

### D2 · 卡顿"两套口径并存"，不替换旧口径

- 旧：`app_jank_light/heavy/severe_count`（窗口级，源自 SoloPi CSV，**保留**，历史数据可比）。
- 新：逐帧口径（源自 `gfxinfo framestats`，source=`pc_supplement`）：
  `app_frame_p50_ms` / `app_frame_p90_ms` / `app_frame_p99_ms` /
  `app_frame_jank_ratio_pct`（帧时长 > 刷新周期×1.5 的帧占比）/
  `app_frame_count`。
- 报告并列展示并**显式标注两者粒度不同、不可互算**。judge 可对任一体系设阈值。

### D3 · 弱网是"环境编排"，独立模块

新增 `netprof.py`（Network Profile）：起停 mitmproxy 子进程、建/拆 `adb reverse`、写/清系统代理，**不碰采集链路**。
`kit.collect(..., net_profile=...)` 只是把它的生命周期挂在会话前后；采集本身仍由 SoloPi 负责。
与 `gate.py` 同构：短事务、幂等、失败 fast-fail、退出必恢复。

### D4 · 解密档"先探测再决定"，不硬试

会话开始前做一次**信任探测**：代理侧观察首个 `CONNECT` 的 TLS 握手结果。

- 握手成功 → 解密档可用，产出接口级指标；
- 握手失败且错误为 `certificate unknown` → 判定**App 不信任代理 CA**，本 run 的接口级指标一律 `not_tested`，**不重试、不降级成猜测数据**；
- 探测结论写入 run 事件与 `meta.json`，供宿主决策（换 debug 包 / 换 root 设备）。

### D5 · 整页加载耗时用"末帧稳定"主口径，网络静默作佐证

不依赖解密，两档通用：

```
page_load_ms = (首个「连续 ≥ QUIET_GAP_MS 无新帧」窗口的起点) − 页面起点
```

- 页面起点由宿主打标给出：`s.mark_page("home")`（新增语义化打标，落 `events.kind=page`）；
- `QUIET_GAP_MS` 默认 300 ms，可配；超过 `PAGE_TIMEOUT_MS`（默认 15 s）仍未稳定 → `not_tested`（如无限轮播）；
- 网络侧"最后一条连接关闭时刻"作为**佐证字段**写入事件 detail，不参与主值（直通档下 HTTP/2 多路复用会让连接存活远超请求完成，不可靠）。

### D6 · 网络档位进入"可比性维度"

弱网档位（`none` / `3g` / `4g` / 自定义）与设备档案同属**可比性维度**：
不同档位的 run 不可直接比趋势、不可共用基线。落法：

- `sessions` 增列 `net_profile`（JSON：档位名 + 延迟/带宽/丢包参数）；
- `kit.trend` / `kit.leak_trend` 默认按 `net_profile` 过滤，跨档需显式覆盖并标记口径不一致（与 `device_profile` 同构语义）；
- baseline 记录来源 run 的档位，judge 档位不匹配 → `not_tested`。

---

## 4. 模块与边界

```
mobile_perf
├─ netprof.py    新增：网络剖面编排（mitmproxy 子进程 + adb reverse + 系统代理）
│                 唯一知道 mitmproxy 的地方（与 solo.py 对 harness 的地位同构）
├─ frameinfo.py  新增：gfxinfo framestats 解析与逐帧统计（纯函数 + 轮询器）
├─ pageflow.py   新增：页面稳定判定与整页耗时（消费 frameinfo 的帧时刻 + 打标）
├─ supplement.py 扩展：GPU 双源采样（kgsl / dumpsys gpu）+ gpu_mem_mb
├─ metrics.py    扩展：新增 canonical key 与双语标签
├─ kit.py        扩展：collect(net_profile=…, frame_poll=True) 组装；judge/trend 档位过滤
├─ report.py     扩展：逐帧分布图、GPU 双指标、接口瀑布图（Top N）
└─ cli.py        扩展：collect --net-profile / --frame-poll；netprof 子命令（doctor/ls）
```

依赖方向仍单向：`netprof` 不 import `collector`；`frameinfo`/`pageflow` 只依赖标准库与 `metrics`。

**进程边界**：mitmproxy 跑在**独立 venv 的 Python**（≥3.12），通过 subprocess + addon 脚本交互，JSON 行输出；不在本进程 import（同 ADR-0002 理由：避免依赖污染与 sys.path 副作用）。→ 建议落 **ADR-0004**。

---

## 5. 指标扩展（canonical key）

命名法沿用 `<对象>_<指标>_<单位>`。全部新增 key 必须先进 `metrics.METRICS` 才能入库。

| canonical key | 单位 | source | 中文标签 | 口径 |
|---|---|---|---|---|
| `gpu_util_pct` | % | pc_supplement | GPU占用 | **改源**：kgsl 窗口 busy/total |
| `gpu_mem_mb` | MB | pc_supplement | 应用GPU显存 | `dumpsys gpu` 的 `Proc <pid> total` |
| `app_frame_p50_ms` | ms | pc_supplement | 帧耗时P50 | 逐帧 `FrameCompleted − IntendedVsync` |
| `app_frame_p90_ms` | ms | pc_supplement | 帧耗时P90 | 同上 |
| `app_frame_p99_ms` | ms | pc_supplement | 帧耗时P99 | 同上 |
| `app_frame_jank_ratio_pct` | % | pc_supplement | 超时帧占比 | 帧时长 > 刷新周期×1.5 |
| `app_frame_count` | 帧 | pc_supplement | 采样帧数 | 本窗口帧数（样本下限依据） |
| `page_load_ms` | ms | pc_orchestration | 整页加载耗时 | 末帧稳定口径（D5） |
| `net_conn_duration_ms` | ms | pc_supplement | 连接建立耗时 | 直通档：TCP/CONNECT 建立→首字节 |
| `api_req_duration_ms` | ms | pc_supplement | 接口耗时 | 解密档：按 URL 聚合 |
| `api_req_count` | 次 | pc_supplement | 接口请求数 | 解密档 |
| `api_error_rate_pct` | % | pc_supplement | 接口错误率 | 解密档：≥400 或连接失败 |
| `api_payload_kb` | KB | pc_supplement | 接口响应大小 | 解密档 |

**不新增**：`app_jank_*_count` 保持原样（历史可比）；`net_rx_kbps` 等 SoloPi 端侧指标不动。

---

## 6. 弱网两档

### 档 A · 直通整形（默认，无需任何前置条件）

- mitmproxy 以 **TLS 直通**模式运行（`ignore_hosts` 全量 + 只做连接级处理），不解密；
- addon 在连接级注入：**固定延迟**、**带宽限速**、**丢包/超时**；
- 可得：连接建立耗时、连接级传输耗时、字节数、连接失败率；
- **不可得**：URL、方法、状态码、响应体、请求头（TLS 内）。
- 指标：`net_conn_duration_ms` + 既有 SoloPi 网络速率。

### 档 B · 解密（需前置条件，逐 run 探测）

前置条件（满足任一即可）：

1. App 出 **debug/profileable 包**且 `networkSecurityConfig` 信任用户 CA；
2. 设备 **root** 或可写 system 分区，安装系统 CA；
3. 使用 **模拟器**（可 `-writable-system`）。

- 满足 → 解密成功，产出 §5 的 `api_*` 四项 + 报告接口瀑布图；
- 不满足 → 探测判定后 `api_*` 全部 `not_tested`，事件记录 `net_profile.decrypt_unavailable` + 原因。
- **本机型 + BYD academy 属"不满足"**（§2.4b），故档 B 的验收在 M6 用**可满足的设备/App 组合**完成，BYD academy 上如实跑成 `not_tested`。

### 档位参数（内置预设，宿主可覆盖）

| 档位 | 延迟 | 下行 | 上行 | 丢包 |
|---|---|---|---|---|
| `none` | 0 | 不限 | 不限 | 0 |
| `4g` | 50 ms | 4 Mbps | 1 Mbps | 0 |
| `3g` | 200 ms | 780 kbps | 330 kbps | 0 |
| `weak` | 400 ms | 400 kbps | 200 kbps | 2% |
| `custom` | 宿主给 | 宿主给 | 宿主给 | 宿主给 |

---

## 7. 整页加载耗时

### 7.1 口径

```
宿主: s.mark_page("home")            → 记录页面起点 t0（统一时间轴）
采样: frameinfo 轮询 framestats（reset 后增量读）→ 帧完成时刻序列
判定: 从 t0 起，找到首个「相邻帧间隔 ≥ QUIET_GAP_MS」的位置 t1
      page_load_ms = t1 − t0
保护: t1 − t0 > PAGE_TIMEOUT_MS → not_tested（页面持续动画，无法判定稳定）
```

### 7.2 为什么不用截图差分

`screencap` 0.45 s/次，采样率不足且会显著干扰被测 App（每次拉起渲染管线）。帧时刻是零成本旁路数据，精度更高。

### 7.3 已知局限（如实记录）

- 页面若含**无限轮播/持续动画**，永不进入静默 → `not_tested`（不猜测）；
- "稳定"是**视觉稳定**，不保证数据已全部就绪（懒加载图片可能在稳定后才到）；
- 首帧前的空白期计入耗时，这是"用户感知等待"的真实组成。

---

## 8. 门禁与趋势的可比性

| 维度 | 落库位置 | 不匹配时 |
|---|---|---|
| 设备档案（已有） | `sessions.device_profile` | judge → `not_tested` |
| **网络档位（新增）** | `sessions.net_profile` | judge → `not_tested`；trend/leak 默认过滤，跨档需显式覆盖并标记 |
| 卡顿口径（新增） | 指标 key 本身区分（`app_jank_*` vs `app_frame_*`） | 不可互算，报告标注 |

`JudgeSpec` 不新增字段：档位由 `kit.judge` 从 run 报告与 baseline 来源 run 解析后注入，保持"纯函数只读报告"。

---

## 9. 里程碑拆分（工单 15–20）

| # | 工单 | Blocked by | 交付 |
|---|---|---|---|
| 15 | 逐帧采集与帧耗时指标 | — | `frameinfo.py` + 5 个 `app_frame_*` key + 报告分布图 |
| 16 | GPU 双源双指标 | 15（共用轮询线程扩展点） | `gpu_util_pct` 改源 + `gpu_mem_mb` |
| 17 | 弱网编排（档 A） | — | `netprof.py` + 档位预设 + 生命周期与恢复 |
| 18 | 解密档与接口级指标 | 17 | 信任探测 + 4 个 `api_*` key + 瀑布图 |
| 19 | 整页加载耗时 | 15, 17 | `pageflow.py` + `mark_page` + `page_load_ms` |
| 20 | M6 集成、档位可比性与文档 | 15–19 | 一条命令全流程 + judge/trend 档位 + 文档 + ADR-0004 |

前端（15、17）可并行；16 与 19 分属两条链；20 收口。

---

## 10. 风险与降级矩阵

| 风险 | 概率 | 影响 | 对策 |
|---|---|---|---|
| 非 Adreno 机型无 kgsl | 高 | GPU 利用率缺失 | 三级降级链 → `not_tested` + 原因 |
| `gpubusy` 窗口与采样错位 | 中 | 利用率偏差 | 只报聚合不报序列；文档写明口径 |
| `gpubusy` 语义按机型不同（§2.1a） | 高（已实测两机型相反） | 跨机型阈值误判；负载中偶发 `0 0` 低估 | 阈值按机型建档、不做跨机型绝对值比较；`0 0 → 0%` 记为已知偏差，样本量足够时用中位/P90 看 |
| App 有本地缓存，弱网下"看起来正常" | 高 | 误判代理未生效 | 以**代理侧连接日志**为生效证据，不以页面表现判断 |
| mitmproxy 需 Python ≥3.12 | 确定 | 与项目 ≥3.10 冲突 | 可选依赖 + 独立 venv + 未装时 `not_tested` 并给安装提示 |
| 解密档前置条件不满足 | 高（本机确定） | 无接口明细 | D4 探测后降级，不猜；文档给出三条前置路径 |
| 无限动画页面 | 中 | 整页耗时不可判定 | `not_tested` + 超时保护 |
| 弱网会话崩溃未恢复代理 | 中 | 设备被留在坏网络 | try/finally + 启动时 `netprof doctor` 检查残留代理 |
| 帧采样轮询干扰被测 App | 低 | 数据失真 | 轮询间隔 ≥1 s，`dumpsys` 不触发渲染 |

---

## 11. 术语登记（同步 GLOSSARY.md）

- **网络剖面（Network Profile）**：一次采集会话施加的网络条件（档位 + 延迟/带宽/丢包），是与设备档案并列的可比性维度。
- **直通档 / 解密档（Passthrough / Decrypt Tier）**：弱网代理是否解密 TLS 的两种工作模式；决定能拿到连接级还是接口级指标。
- **逐帧口径（Per-frame Scope）**：以 `gfxinfo framestats` 的单帧耗时为基础统计的卡顿口径，区别于窗口级口径。
- **整页加载耗时（Page Load Time）**：从页面打标到画面进入稳定（连续无新帧）的耗时。
- **信任探测（Trust Probe）**：会话前对 App 是否信任代理 CA 的一次性判定，失败即降级不重试。

---

## 12. 验收标准

1. `mobile-perf collect <pkg> --net-profile weak --frame-poll --page home` 一条命令产出：整页耗时、逐帧卡顿、GPU 双指标、弱网连接耗时；
2. BYD academy 实测：整页耗时与逐帧指标有值；GPU 双指标有值；**接口级指标如实 `not_tested`**（原因写明）；
3. 换满足前置条件的组合（模拟器可写 system / debug 包）时接口级指标有值且瀑布图可看；
4. 弱网会话结束后设备代理必被恢复（含异常退出路径，`netprof doctor` 可查残留）；
5. 跨网络档位的 trend 默认拒绝、显式覆盖可查并标记口径不一致；
6. 新增指标全部经指标字典入库，unit/contract 覆盖解析与降级分支；真机项走 `-m device` 门控。
