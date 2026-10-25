# 17: 弱网编排（直通整形档）

**What to build:** 新增 `netprof` 模块——网络剖面编排：起停代理子进程、建/拆 `adb reverse`、写/清设备系统代理，把被测 App 的网络条件变成可配置、可复现、**退出必恢复**的采集前置。与 `gate` 同构：短事务、幂等、失败 fast-fail、try/finally 保证恢复。

范围（直通整形档，无需任何前置条件）：代理以 TLS 直通模式运行，不解密；addon 在连接级注入固定延迟、带宽限速、丢包。产出 `net_conn_duration_ms`（连接建立→首字节）。内置档位预设 `none/4g/3g/weak/custom`（参数见 M6 方案 §6），宿主可覆盖。

三条实测约束必须体现在实现里：

1. mitmproxy 要求 **Python ≥3.12**（项目运行时 ≥3.10）→ 作**可选依赖**，跑在独立 venv 的 Python 上，通过 subprocess + addon 脚本交互（JSON 行输出），不在本进程 import；未安装时 `not_tested` 并给出安装提示，**不阻断**非弱网采集。
2. `adb reverse` + `settings put global http_proxy` 实测可用（`:0` 清除）；但**不能用"页面能否打开"判断代理是否生效**——BYD academy 在代理指向死端口时仍完整加载首屏（本地缓存/多域名兜底）。生效证据只看**代理侧连接日志**。
3. 弱网会话异常退出会把设备留在坏网络 → 启动时 `netprof doctor` 检查并清理残留代理/端口转发。

**Blocked by:** None (can start immediately)

**Status:** done（2026-10-25）

- [x] 档位预设纯函数可测（参数校验、未知档位报错、custom 必填项）
- [x] 生命周期幂等：重复 start/stop 安全；异常路径（采集抛错、Ctrl-C）后设备代理与 reverse 必被恢复
- [x] 代理生效判定基于代理侧日志而非页面表现；探测不到连接时如实标记（不假设成功）
- [x] mitmproxy 缺失/版本不满足时 `not_tested` + 安装提示，其余采集照常完成
- [x] 真机验证：`weak` 档下 `net_conn_duration_ms` 明显高于 `none` 档，且设备状态可恢复
- [x] `netprof doctor` 能发现并清理残留代理设置

## Comments

### 2026-10-25 完成记录

**实现**：`netprof.py`（档位纯函数 + 代理侧日志证据 + 生命周期编排 + doctor）、
`netproxy_addon.py`（mitmproxy 直通整形 addon，**只由子进程加载**）、
`metrics.net_conn_duration_ms`、kit/report/CLI 接线（`collect --net-profile`、
`netprof doctor|probe`）。

**设计要点（含两条实测修正）**：

1. **对照组也走代理**。最初按"none 档不开代理"实现，但那样两档走的是不同测量
   路径（一个经代理一个直连），`net_conn_duration_ms` 不可比。改为 none 档同样
   起代理、整形参数全 0；只在 mitmproxy 缺失时允许 none 档跳过代理（记 note）。
2. **代理侧日志是唯一证据**。addon 逐事件写 JSON Lines（`net_proxy.jsonl`），
   `analyze_proxy_log` 纯函数解析：连接数、`tcp_start`→首个客户端字节的连接耗时、
   域名、错误数、字节数、整形参数。零连接 → `evidence=none` → 指标 `not_tested`
   并写明原因，**不产出 0 值**。
3. **整形口径**：延迟每条消息各加一次；带宽按 `size/rate` 追加排队延迟；
   TCP 流级别无法真丢包（丢字节只会破坏流完整性），故按 `loss_pct` 概率追加
   2× 延迟停顿近似重传退避，日志首行 `lossMode="stall_emulation"` 如实标注。
4. **CONNECT 后强制裸 TCP 层**（`next_layer` + `TCPLayer`）：不解密 TLS，只做连接级
   整形。实测 mitmproxy 12.3.0 的 `ignore_hosts` 会让隧道直接透传、拿不到 `tcp_*`
   钩子，故用 `next_layer` 强制；`server_connect/connected` 钩子的字段是
   `data.client`/`data.server`（不是 `data.client_conn`）。
5. **doctor 复验**：清理命令返回 0 ≠ 设备干净，清理后再读一次，只有读回干净才置
   `proxyClean/reverseClean`；CLI 在复验不干净时非零退出。
6. **端口探测用 connect 而非 bind**：Windows 的 SO_REUSEADDR 允许抢占式 bind，
   bind 成功不代表端口空闲。

**真机实测**（749622c9 / com.sinosoft.trainapp，冷启 + 滚动，每档同操作）：

| 档位 | 连接数 | 命中域名 | 连接耗时中位 | P90 | 恢复 |
|---|---|---|---|---|---|
| none | 23 | 3 | 224.1 ms | 418.9 ms | 代理/reverse/进程全 True |
| weak | 16 | 2 | **636.2 ms** | 808.4 ms | 代理/reverse/进程全 True |

中位比值 **2.84×**（验收要求明显高于，device 测试阈值 1.5×）。doctor 事后复验
`proxy=':0'`、`reverse=[]`、`cleaned=False`（无残留）。

**验证**：unit+contract 522 passed；device 4 passed（两档对比、设备恢复、doctor
真机清理、代理侧域名与参数）。ruff 全绿。

**端到端复验**（`python -m mobile_perf collect ... --net-profile weak`，
run `m6-net-e2e-4`，25s 采集）：`network.effective=true`、
`evidence=observed`、`tier=passthrough`、`restored` 三项全 True；
代理侧 17 条连接、2 个域名（`d-lms-sg-uat.byd.com`、
`firebaseinstallations.googleapis.com`）、0 错误、下行 168321 B；
`net_conn_duration_ms` median 626.1 ms / P90 643.7 ms / 16 样本；
报告 HTML 含「网络剖面」段与「只看代理侧日志」说明；事后 `netprof doctor`
复验 `proxy=':0'`、`reverse=[]`、`proxyClean/reverseClean=true`。

> 入口坑：`python -m mobile_perf.cli` **不会**执行 main（`cli.py` 无
> `__main__` 守卫），会静默退出 0 且不产出任何东西。端到端必须用
> `python -m mobile_perf`（或 console script `mobile-perf`）。

**未覆盖 / 已知限制**：
- 只在 Windows 主机 + 单台 Android 14 设备上验证；macOS/Linux 未测。
- `stall_emulation` 是近似丢包，不是真丢包；弱网下的重传行为与真实链路有差异。
- 接口级明细（URL/状态码/响应体）属工单 18 的解密档，本工单不做。
- `net_profile` 尚未进 `sessions` 表与 trend 可比性过滤（工单 20）。
