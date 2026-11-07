# 只读调研任务书：APP 抓包开源方案

任务：为 mobile_perf 组件的「APP 抓包（HAR 请求/响应）」做只读开源方案调研，产出一份可执行的中文方案报告 `research-opencode.md`。你由编排方通过根仓库 `dispatch.sh opencode` 调用，模型已锁定为 `opencode/deepseek-v4.1-flash`。

## 硬约束

- 只读：禁止修改主项目任何源码、配置、文档、数据、设备状态；唯一允许创建/修改的文件是**当前工作目录内**的 `research-opencode.md`。
- 禁止 git 操作、安装依赖、改设备状态、commit/push/发布；禁止删除任何文件。
- 优先使用 `curl` 检查公开源码/文档（WebFetch 在本机被拦截）。联网事实给出 URL、许可证、版本与查看日期 2026-11-07；无法访问时如实标注，不猜测。
- 可读取本机已安装的 mitmproxy 12.3.0 源码：`D:/workspace/claudecode-test/moblie-xn/.scratch/venv-mitm/Lib/site-packages/mitmproxy/addons/savehar.py`，以及主项目 `src/mobile_perf/netprof.py`、`netproxy_addon.py`、`kit.py`、`cli.py`、`report.py`。
- 中文输出；区分已经在本机验证的事实、公开文档信息与推断。

## 调研范围

比较至少 3 类方案：

1. mitmproxy 自带 `savehar` / `hardump`（`--set hardump=...`）：检查源码确认进程结束时写入、内存保留 flows、默认无脱敏、HAR 1.2 字段和 body/错误/CONNECT/HTTP2/WebSocket 处理方式；
2. 在现有 `netproxy_addon.py` 的 `request()` / `response()` / `error()` 钩子里流式写 HAR 1.2 或结构化 JSONL/摘要，支持敏感头脱敏、体大小上限与截断标志；
3. 其它开源方案（HTTP Toolkit、PacketCapture、reqable、PCAPdroid 等任选可核实者）作为客户端或端侧抓包的适配成本、许可证及与本组件 `adb reverse + settings global http_proxy` 链路的契合度。

结合本机代码结构给出最小改动路径，明确说明：

- HAR 开关默认关闭还是开启，CLI / 库 API 如何传；
- 文件落在 `runs/<runId>/network/` 的命名与报告入口；
- TLS 解密成功才有完整请求/响应；passthrough 与证书拒绝时如何如实标注 not_tested，不得伪造 HAR；
- 进程退出、JSON 写入中断、HAR 截断与大小限制的边界；
- 敏感值处理（Authorization、Cookie、Set-Cookie、Token、Proxy-Authorization 等），URL 查询串中的敏感参数策略，body 是否默认只记大小/摘要；
- CONNECT、HTTP/2、WebSocket、二进制 body、重定向、错误请求的边界；
- 许可证与归因（现有项目已使用 Apache-2.0 ECharts，mitmproxy 为 MIT）。

## 输出

- 给出明确推荐（含理由与放弃项）及至少 8 条可由自动化测试或真机验证的验收标准。
- 报告第一行写 `RESEARCH: DONE`；末尾列出证据来源（URL 或本机路径）。
- 完成后在回复中报告文件路径与一句话结论。验证标准：仅此一份报告文件、无其它文件改动，报告能支持编排方直接形成开发任务书。
