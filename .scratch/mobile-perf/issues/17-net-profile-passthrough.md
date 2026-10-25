# 17: 弱网编排（直通整形档）

**What to build:** 新增 `netprof` 模块——网络剖面编排：起停代理子进程、建/拆 `adb reverse`、写/清设备系统代理，把被测 App 的网络条件变成可配置、可复现、**退出必恢复**的采集前置。与 `gate` 同构：短事务、幂等、失败 fast-fail、try/finally 保证恢复。

范围（直通整形档，无需任何前置条件）：代理以 TLS 直通模式运行，不解密；addon 在连接级注入固定延迟、带宽限速、丢包。产出 `net_conn_duration_ms`（连接建立→首字节）。内置档位预设 `none/4g/3g/weak/custom`（参数见 M6 方案 §6），宿主可覆盖。

三条实测约束必须体现在实现里：

1. mitmproxy 要求 **Python ≥3.12**（项目运行时 ≥3.10）→ 作**可选依赖**，跑在独立 venv 的 Python 上，通过 subprocess + addon 脚本交互（JSON 行输出），不在本进程 import；未安装时 `not_tested` 并给出安装提示，**不阻断**非弱网采集。
2. `adb reverse` + `settings put global http_proxy` 实测可用（`:0` 清除）；但**不能用"页面能否打开"判断代理是否生效**——BYD academy 在代理指向死端口时仍完整加载首屏（本地缓存/多域名兜底）。生效证据只看**代理侧连接日志**。
3. 弱网会话异常退出会把设备留在坏网络 → 启动时 `netprof doctor` 检查并清理残留代理/端口转发。

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] 档位预设纯函数可测（参数校验、未知档位报错、custom 必填项）
- [ ] 生命周期幂等：重复 start/stop 安全；异常路径（采集抛错、Ctrl-C）后设备代理与 reverse 必被恢复
- [ ] 代理生效判定基于代理侧日志而非页面表现；探测不到连接时如实标记（不假设成功）
- [ ] mitmproxy 缺失/版本不满足时 `not_tested` + 安装提示，其余采集照常完成
- [ ] 真机验证：`weak` 档下 `net_conn_duration_ms` 明显高于 `none` 档，且设备状态可恢复
- [ ] `netprof doctor` 能发现并清理残留代理设置

## Comments
