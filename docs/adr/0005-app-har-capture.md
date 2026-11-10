# ADR-0005：APP 抓包采用流式 JSONL + 原子合成 HAR 1.2

- 状态：已接受
- 日期：2026-11-07
- 关联：ADR-0002（Harness 子进程边界）、ADR-0004（mitmproxy 可选子进程）
- 验收：[真机验收与 HTML 报告](../app-har-capture-acceptance.md)

## 背景

需求是在现有 `mobile_perf` 采集窗口内增加 APP 抓包，产物为 HAR 请求/响应，
并与既有弱网档位、连接级/接口级指标、设备恢复与 `net_proxy.jsonl` 唯一证据
语义严格共存。抓包默认关闭，且 HAR 天然含业务敏感信息。

候选实现里，mitmproxy 内置 `hardump` 看似最省事，但在本项目的运行方式下不可行：

- addon 跑在独立 mitmproxy 子进程；Windows 上 `Popen.terminate()` 不会触发
  mitmproxy 的 `done()`，`hardump` 没有机会把内存中的流写盘，硬终止会丢文件；
- `hardump` 需要把全部 flow 留在内存，长会话占用不可控；
- 内置导出不做本项目要求的默认脱敏与正文元数据口径。

## 决策

采用「**自定义流式 HAR JSONL + 停止时原子合成 HAR 1.2**」：

1. **写入链路**：addon 在 `response()`、`error()`、`websocket_end()` 中把已脱敏
   的 entry 逐行 append 到 `app.har.entries.jsonl` 并立即 `flush()`；不依赖
   `done()`/`hardump`。`http_connect()` 不写条目，CONNECT 绝不伪装成请求。
2. **合成链路**：主进程在设备恢复与代理进程终止之后，流式读取 JSONL、跳过坏行，
   写同目录 `app.har.tmp`，最后 `os.replace` 原子落盘。无有效条目不创建
   `app.har`；`stop()` 幂等，不重复合成、不残留 `.tmp`。
3. **默认安全**：`Authorization/Cookie/Set-Cookie/Proxy-Authorization/*Token/*Key/
   Signature` 与 URL 常见敏感查询参数、userinfo 默认替换为 `[REDACTED]`；默认
   不写正文，只记 size + MIME + sha256（大 body 不计算摘要并标记，阈值口径是
   `max_hash_bytes`，默认 `8 * 1024 * 1024` 字节，不是 MB 参数）；显式开启
   正文时按单条上限截断并标记，二进制用 base64。
4. **抓包与弱网正交**：`capture_har=True` 且无 `net_profile` 时内部用
   `none + decrypt` 起代理，但不写 `sessions.net_profile`（报告
   `captureOnly=true`），趋势/基线的可比性维度零改动；显式 `net_tier` 不是
   `decrypt` 时早失败，不自动改档。
5. **诚实降级**：mitmproxy 缺失、端口占用、代理未起、零 HTTP 请求都只让 HAR
   标 `not_tested` 并给原因，不阻断采集、不创建空文件；证书拒绝但存在明文
   HTTP 时产 `partial`（`httpsCoverage="none"`）并明写 HTTPS 未解密。
6. **证据边界**：`net_proxy.jsonl` 仍是代理生效的唯一证据；HAR 不参与
   `net_conn_duration_ms` 或 `api_*` 计算，摘要只随 `meta.network.har` 进报告。

## 后果

- 优点：Windows 硬终止不丢已 flush 的条目；内存占用与会话长度解耦；默认脱敏
  与正文口径可控；HAR 合成失败不影响采集闭环，失败原因可追溯。
- 代价：多一个 JSONL 中间文件（与最终 HAR 同在 `network/` 下）；entry 结构需
  自己维护 HAR 1.2 兼容性；限额统计需从代理日志的 `har_dropped`/`har_error`
  事件还原（addon 被终止后其内存计数器不可读）。
- 兼容性：默认关闭时无 HAR 环境变量、无新文件、`network` 段形状与旧 run 一致；
  旧 run 无 `har` 键时报告输出零变化。

## 备选方案

- **mitmproxy `hardump`**：Windows terminate 丢文件、全量内存、无脱敏，拒绝。
- **HTTP Toolkit / PCAPdroid / VpnService**：引入新运行时或许可证约束（AGPL/GPL），
  与现有 mitmproxy 子进程边界重复，拒绝。
- **边抓边写完整 HAR（无法原子收尾）**：终止时文件可能是非法 JSON，报告与
  下游解析不可靠，拒绝。
