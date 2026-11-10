# APP 抓包（HAR）真机验收与 HTML 报告

本文补充 [README](../README.md)、[嵌入指南](嵌入指南.md) §3.3 与
[ADR-0005](adr/0005-app-har-capture.md) 的验收操作。范围是 `mobile_perf`
的 HAR 抓包，不是新的性能指标口径；报告中的 `network.har` 是证据摘要，
`net_proxy.jsonl` 仍是代理是否生效的唯一证据。

## 1. 前置条件

- Python ≥3.10；抓包依赖独立的 mitmproxy（Python ≥3.12）：
  `MOBILE_PERF_MITMDUMP` 指向 `mitmdump.exe`，或使用 `--net-mitmdump`。
- 真机在线，`adb` 可由组件的三级解析找到；设备系统代理与 `adb reverse`
  必须可写、可恢复。
- **不需要 root、不需要安装 CA**。本文的 HTTPS 路径只验收“证书拒绝时的
  如实降级”，不把明文 HTTP 结果冒充 HTTPS 解密结果。

## 2. 正式采集路径

```bash
# 1) 采集窗口内开启抓包；不指定 net_profile 时内部用 none + decrypt
mobile-perf collect <pkg> --items CPU,Memory --duration 30 \
  --capture-har --net-tier decrypt \
  --net-mitmdump <mitmdump 路径>

# 2) 生成含 network.har 摘要的离线 HTML + JSON
mobile-perf report <runId>
```

采集窗口内必须实际产生流量：

- 明文 HTTP：设备侧请求需显式经过代理，例如
  `curl --proxy http://127.0.0.1:8080 http://127.0.0.1:<port>/probe?token=...`；
  Android 可能对 `127.0.0.1` 走本地直连，不能只看“页面能打开”。
- HTTPS：冷启动被测 App，让首个 CONNECT 在采集窗口内发生；targetSdk ≥24
  的正式包通常不信任用户 CA，应预期 `trust_probe` 为 rejected。
- 首屏验收必须在采集窗口内启动 App，否则首屏请求在代理施加前已直连完成。

## 3. 本机双路径验收脚本

本仓库保留了一个不安装证书的真机验收脚本：

```bash
MOBILE_PERF_MITMDUMP=<repo>/.scratch/venv-mitm/Scripts/mitmdump.exe \
  python .scratch/app-capture/verification/accept_real_device.py
```

脚本只写 `.scratch/app-capture/verification/acceptance-*/` 下的证据，不修改
产品源码；两条路径：

1. **明文 HTTP 完整 HAR**：本机临时 HTTP 服务 + `adb reverse` + 设备侧
   `curl --proxy`；检查 `app.har` 为 HAR 1.2、至少 1 条 200 条目、无 CONNECT
   伪请求、`token`/`Set-Cookie` 已脱敏、正文默认仅元数据。
2. **HTTPS 证书拒绝**：正式包在 decrypt 档冷启动；检查代理侧有 CONNECT 与
   `trust_probe`，HAR 为 `not_tested` 或仅真实明文的 `partial`，不创建空 HAR。

脚本结束会恢复系统代理、拆除 `adb reverse`，并用 `netprof doctor` 复验。
当前脚本的 `result.json` 还记录 17 项检查；2026-11-07 真机
`749622c9`（Xiaomi 2206123SC / Android 14）实测 17/17 通过。

## 4. HTML 报告检查点

`mobile-perf report <runId>` 的 `report.html` 应满足：

- 网络剖面表出现 `HAR / HAR`、`HAR 条目`、`HAR 产物`、`正文`、`脱敏`、
  `截断` 与 `HTTPS 覆盖` 行；
- 有有效条目时 `HAR 产物` 链接指向 `network/app.har`；无有效条目时显示
  “无（未创建空 HAR）”，并给出 `notTestedReason`；
- 报告 JSON/HTML 不内嵌 HAR 正文或敏感原值；`report.json` 的
  `network.har` 只保留状态、条目数、大小、脱敏/截断/降级原因；
- 报告不包含性能曲线时，不要把它误读为性能采集完成：`items=HAR` 的验收
  报告只证明抓包链路，不产生 CPU/内存/FPS 指标。

## 5. 已知边界

- HTTPS 请求/响应内容需要 App 信任代理 CA；本机正式包无 CA 时只能得到
  连接级证据和明文 HTTP 条目。
- QUIC/UDP、非 HTTP TCP、证书 pinning、未关闭的 WebSocket 长连接不保证覆盖。
- 自定义敏感头、路径段中的 token 需宿主按业务补充脱敏策略；默认名单只覆盖
  常见认证头、Cookie、URL 查询参数与 URL 型头。
- 真机验收使用 `.scratch` 本地证据，不写入产品数据库；不要把本地报告路径
  当作跨机器的稳定链接。
