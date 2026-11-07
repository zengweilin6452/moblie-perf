# APP 抓包（HAR）综合设计决议 — 2026-11-07

## 1. 范围与已确认事实

- 用户目标：给 `mobile_perf` 增加 APP 抓包，产物为 **HAR 请求/响应**；真机验收不装证书，同时验证降级路径与完整 HAR 路径；由编排方调度其他 Agent 完成调研、开发、审查和验收。
- 用户授权本次任务内各次代理派发组合：OpenCode `opencode/deepseek-v4.1-flash` 系 与 Qoder `Qwen3.8-Flash`。实测 `opencode/deepseek-v4.1-flash` 直连 ID 返回「Insufficient account funds」；同模型的 `magpie/workbuddy/deepseek-v4.1-flash` 可用，已按同模型换通道处理，并在交付中如实说明。OpenCode 调研报告进程退出码虽为 1，但报告已完整落盘且首行 `RESEARCH: DONE`，内容经编排方复核。
- 两份报告：
  - `.scratch/app-capture/research-opencode/research-opencode.md`：开源方案比较，重点排除 mitmproxy `hardump` 的 Windows `terminate` 丢文件、全量内存和无脱敏问题；
  - `.scratch/app-capture/research-qoder/research-qoder.md`：现有调用链、报告/归档契约、测试计划与真机清单。
- 本机已验证：mitmproxy 12.3.0 / Python 3.12.10 位于 `.scratch/venv-mitm/Scripts/mitmdump.exe`；mitmproxy 为 MIT，HTTP Toolkit 为 AGPL-3.0，PCAPdroid 为 GPL-3.0；现有项目单测/契约测试及 ruff 全绿；真机 `749622c9` 在线。
- 真机前置实验（`.scratch/app-capture/verification/probe_plain_http.py`）：`adb reverse tcp:8080/18080` + `settings global http_proxy` 后，小米自带浏览器访问本地 HTTP 测试端点，代理 `request()` 钩子观察到明文 HTTP 请求，本地服务返回 200；结束后代理恢复为 `:0`、无残留 reverse。该实验证明「不装 CA 也能走完整请求/响应 HAR」路径可用，且不需局域网/防火墙例外。

## 2. 决议

采用「**自定义流式 HAR JSONL + 停止时原子合成 HAR 1.2**」，不采用 mitmproxy 内置 `hardump`，不引入 HTTP Toolkit / PCAPdroid / VpnService。

### 2.1 核心语义

1. **抓包与弱网是两件正交的事**：
   - `net_profile` 继续表示弱网档位与可比性维度，保持 `None` 为不施加弱网；
   - 新增 `capture_har=True` 时，即使没有 `net_profile` 也启动代理，内部以 `none` 档、`decrypt` 档运行，不写 `sessions.net_profile`（历史/对照口径不变）；`trend/judge` 的可比性语义零改动；
   - 同时给 `net_profile` 与 `capture_har` 时，沿用显式档位，要求 `net_tier=decrypt`，不得偷偷改档。
2. **默认关闭，默认脱敏，默认只记 body 元数据**：HAR 含业务敏感信息，必须显式开启；`Authorization/Cookie/Set-Cookie/Proxy-Authorization/*Token/*Key/Signature` 及 URL 常见敏感查询参数写前替换为 `[REDACTED]`；默认不写请求/响应正文，只记大小与 sha256，正文需 `--capture-har-bodies` 显式开启并有单条上限；允许 `--capture-har-no-redact` 仅供本地排查，报告显著标记（首版可保留该开关但不做静默默认）。
3. **可用性与诚实降级**：
   - `capture_har=True` 且 `net_tier != decrypt` 或自定义档位被显式指定为 passthrough → 参数错误，早失败 exit 1；
   - mitmproxy 缺失、端口占用、代理未起、证书拒绝或无可观察 HTTP 请求 → 采集本身正常完成，HAR 状态为 `not_tested` 并写清原因，不创建误导性的空 `app.har`；连接级指标、恢复顺序、`api_*` 既有 not_tested 文案均不改变；
   - **明文 HTTP 无需 CA 信任**：证书拒绝时若有明文 HTTP 请求，可生成只包含这些真实请求的 `app.har`，但状态必须为 `partial`、`httpsCoverage=none`、`notTestedReason` 明确写「HTTPS 未解密」；不得把 HTTPS CONNECT 伪装成请求。
4. **Windows 耐久性**：addon 在 `response()/error()/websocket_end()` 中逐行 append 已脱敏的 HAR entry 到 `app.har.entries.jsonl` 并 flush；`NetProfiler.stop()` 在设备恢复和进程终止之后，流式读取 JSONL、跳过坏行，写 `app.har.tmp` 后 `os.replace` 为 `app.har`。不依赖 mitmproxy `done()`（Windows `Popen.terminate()` 不会执行它）。
5. **报告契约**：`app.har` 与 `entries.jsonl` 落 `runs/<runId>/network/`；`NetSession.as_dict()["har"]` 挂进 `meta.network.har`，借现有 `assemble_run_report(meta.get("network"))` 重组通道活过 report 重放；`report.html` 在 network 段显示状态、条目数、截断、脱敏和相对路径链接，不内嵌 HAR 正文；旧 run 无 `har` 键时零变化。
6. **证据边界**：`net_proxy.jsonl` 仍是代理生效的唯一证据；HAR 是请求/响应产物，不参与 `net_conn_duration_ms` 或 `api_*` 指标计算；QUIC/UDP、非 HTTP TCP、证书 pinning 与未关闭的 WebSocket 长连接不保证覆盖，报告/文档如实标明。

### 2.2 用户接口（首版最小集）

CLI：

```text
mobile-perf collect <pkg> --capture-har \
  [--net-profile weak --net-tier decrypt] \
  [--capture-har-bodies] \
  [--capture-body-limit-bytes 65536] \
  [--capture-max-entries 5000] \
  [--capture-max-bytes 67108864] \
  [--capture-har-no-redact] \
  [--net-mitmdump ... --net-port 8080]
```

库 API：`PerfKit.collect/run_collect(..., capture_har: bool = False, capture_options: CaptureOptions | None = None)`；`CaptureOptions` 承载上述限额与脱敏/正文开关。CLI 的 `--capture-*` 参数在未开启 `--capture-har` 时报错；限额必须为正；`capture_har` 与显式 passthrough 冲突时报错。

### 2.3 文件白名单（写作者仅可改这些）

- 新增 `src/mobile_perf/har_writer.py`（纯标准库：脱敏、entry 构造、限额、JSONL writer、HAR 合成、摘要 dataclass）；
- 修改 `src/mobile_perf/netproxy_addon.py`、`netprof.py`、`kit.py`、`cli.py`、`report.py`；
- 新增 `tests/unit/test_har_writer.py`、`tests/unit/test_netprof_har.py`、`tests/unit/test_capture_cli.py`、`tests/unit/test_capture_kit.py`、`tests/unit/test_capture_report.py`；扩展 `tests/device/test_netprof_device.py` 仅限新增 HAR 真机门控用例；
- 修改 `README.md`、`GLOSSARY.md`、`docs/嵌入指南.md`、`docs/adr/0005-app-har-capture.md`（新增 ADR）；
- 所有 `.scratch/app-capture/implementation/` 下由审查环生成的日志/报告由脚本产生，不算作者越界。

明确不改：`.phase2/.phase3`（另一仓库，与本任务无关）、`SoloPi/` App 源码、根 `dispatch.sh`、`pyproject.toml` 依赖、`store.py` schema、`collector.py` 保留策略、既有 `net_proxy.jsonl` 事件格式、既有 `PROFILE_PRESETS`/`TRUST_*`/恢复语义。

## 3. 验收标准

1. 默认关闭：`capture_har=False` 时无 HAR 文件、无 `MOBILE_PERF_HAR*` 环境变量、旧测试逐字节语义不变；全套 unit/contract/device 门控与 ruff 通过。
2. CLI/库参数：早失败、限额校验、参数透传有对应单测；`--capture-har` 单独使用时自动起 `none+decrypt` 代理且不污染网络档位可比性。
3. HAR 1.2：`json.load(app.har)` 成功，`log.version=1.2`，entry 具备 `startedDateTime/time/request/response/timings`；CONNECT 不写成请求；错误 flow 单条记录；坏 JSONL 行跳过并计数；原子替换不留 `.tmp`。
4. 脱敏与大小：`PROBE_SECRET` 不出现在 HAR/JSONL/代理日志/报告；头名与查询参数 key 保留；默认无正文、有大小和摘要；`--capture-har-bodies` 截断带标记；条目/字节上限触发 `partial` 且 HAR 仍可解析。
5. 降级：passthrough 参数组合 exit 1；mitmproxy 缺失、零请求、证书拒绝三种运行态均给 `not_tested` 或仅明文的 `partial`，不出现空 HAR；既有 `api_*` 与连接级恢复断言保持通过。
6. 报告：`meta.network.har`、`report.json/network.har`、HTML 相对链接与旧 run 兼容；`assemble_run_report` 重组后 HAR 摘要仍在；捕获事件入库（仅开启时）。
7. 真机双路径（编排方独立执行，不装 CA）：
   - 正式包 decrypt 档：代理观察到 CONNECT/证书拒绝，HAR 如实 `not_tested`/仅明文 `partial`，设备代理与 reverse 恢复，`netprof doctor` 干净；
   - 内置浏览器访问 `127.0.0.1` 本地测试端点（经 `adb reverse`）：HAR 至少 1 条真实 GET 200 请求/响应，`Set-Cookie` 与 `?token=...` 脱敏，报告路径有效，设备恢复干净；
   - 两项均不安装证书、不改系统安全设置，不将后台 HTTPS CONNECT 误称为已解密请求。
8. 文档/许可：写清 HAR 覆盖边界、默认脱敏和 body 口径；README 致谢 mitmproxy（MIT，可选依赖，未 vendor），ADR 记录 Windows `hardump` 不可用与流式 JSONL 决策。

## 4. 调度与审查

- 写作者：OpenCode（同模型可用通道 `magpie/workbuddy/deepseek-v4.1-flash`），在独立工作目录 `.scratch/app-capture/implementation/` 内以任务书为唯一规范开发；其文件白名单指向主项目，禁止 git、安装依赖、删除、commit/push/发布。
- 审查者：Qoder `Qwen3.8-Flash`，只读审查交接载荷，首行 `VERDICT: PASS|FAIL`，最多 3 轮；写作者与审查者跨引擎。
- 编排方终验：亲自重跑全套 unit/contract、ruff、CLI 负例、独立 HAR 解析/脱敏对拍、真机双路径与恢复检查；必要时修复集成问题后再做最小定向回归。
