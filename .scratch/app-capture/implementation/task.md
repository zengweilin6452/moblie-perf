# 开发任务书：mobile_perf APP 抓包（HAR 请求/响应）

你是本次实现的写作者，由编排方通过根仓库 `dispatch.sh opencode` 调用，模型为同模型的可用通道 `magpie/workbuddy/deepseek-v4.1-flash`。项目根为 `D:/workspace/claudecode-test/moblie-xn`。必须独立实现、测试、交付；不要询问编排方，不要改写任务规范。

## 0. 目标

给现有 Python 组件 `mobile_perf` 增加一个默认关闭的 APP 抓包能力：在采集窗口内，经现有 mitmproxy 子进程把可观察的 HTTP 请求/响应按 **HAR 1.2** 落到 `runs/<runId>/network/app.har`，并在 meta/report/HTML/事件中给出摘要和诚实降级原因。抓包产物与既有弱网档位、连接级/接口级指标、设备恢复、`net_proxy.jsonl` 唯一证据语义严格共存。

综合设计决议见 `D:/workspace/claudecode-test/moblie-xn/.scratch/app-capture/DESIGN.md`；两份调研报告在 `.scratch/app-capture/research-opencode/research-opencode.md` 与 `research-qoder/research-qoder.md`。若本任务书与设计有冲突，以本任务书为准；可读取报告和现有源码，但不得改动它们（调研报告为只读证据）。

## 1. 硬性边界

- 允许修改/新增的文件**仅限**：
  - `src/mobile_perf/har_writer.py`（新增）；
  - `src/mobile_perf/netproxy_addon.py`；
  - `src/mobile_perf/netprof.py`；
  - `src/mobile_perf/kit.py`；
  - `src/mobile_perf/cli.py`；
  - `src/mobile_perf/report.py`；
  - `tests/unit/test_har_writer.py`、`tests/unit/test_netprof_har.py`、`tests/unit/test_capture_cli.py`、`tests/unit/test_capture_kit.py`、`tests/unit/test_capture_report.py`（新增）；
  - `tests/device/test_netprof_device.py`（只追加 HAR 真机门控用例，不删改旧用例）；
  - `README.md`、`GLOSSARY.md`、`docs/嵌入指南.md`、`docs/adr/0005-app-har-capture.md`（新增）。
- 审查环工作目录 `.scratch/app-capture/implementation/` 下由脚本生成的 handoff/review/log 不属于越界；写作者不得手动创建其它文件。
- 禁止 git 操作、commit/push/PR/发布、安装依赖、删除文件、改设备状态、联网写操作；禁止修改 `SoloPi/` App 源码、根 `dispatch.sh`、`pyproject.toml`、`store.py` schema、`collector.py`、`metrics.py`、既有冻结基线或 `.scratch` 内调研/设计文件。
- 主包运行时仍为 Python ≥3.10、零第三方依赖。`har_writer.py` 与所有可被主包导入的代码只能用标准库；mitmproxy 仅在 `netproxy_addon.py` 子进程里使用，Main 进程不得 import addon 或 mitmproxy。
- 不改既有 `net_proxy.jsonl` 事件格式，不改 `PROFILE_PRESETS`、`TRUST_*`、`api_*`、`net_conn_duration_ms`、`NetSession.effective/api_effective`、恢复顺序和 `collector.enforce_retention`；旧 run 无 `network.har` 键时报告必须零变化。
- 在 `D:/workspace/claudecode-test/moblie-xn` 下用 `python -m pytest tests/unit tests/contract -q --disable-warnings` 与 `python -m ruff check src tests` 验证；两者必须通过。不运行真机用例（由编排方终验）。

## 2. 功能规范

### 2.1 开关与语义

- CLI 在 `collect` 新增：`--capture-har`（默认关）、`--capture-har-bodies`（默认关）、`--capture-body-limit-bytes`（默认 65536）、`--capture-max-entries`（默认 5000）、`--capture-max-bytes`（默认 67108864）、`--capture-har-no-redact`（默认关，报告显著标注危险）。
- 库 API：`PerfKit.collect` 与 `run_collect` 新增 `capture_har: bool = False` 和 `capture_options: CaptureOptions | None = None`；`CaptureOptions` 定义在 `har_writer.py` 并从 `mobile_perf.netprof` 或直接 `har_writer` 可稳定导入。CLI 参数映射为 `CaptureOptions` 字段。
- `--capture-*` 细项未配 `--capture-har` → 参数错误 exit 1。限额必须为正整数，否则 exit 1 / ValueError。
- `capture_har=True` 且显式 `net_tier` 不是 `decrypt` → 早失败 exit 1 / ValueError，提示显式 `--net-tier decrypt`；不自动改显式档位。
- **抓包与弱网正交**：`capture_har=True` 且 `net_profile is None` 时，仍启动代理，内部使用 `build_profile("none")` + `TIER_DECRYPT`，但 `sessions.net_profile` 必须为 `None`，`network.profile` 不得伪称用户选择了弱网档位（可用 `captureOnly: true` 标注）。同时给 `net_profile` 时沿用原档位，写库语义不变，`net_tier` 必须 decrypt。
- `capture_har=False` 时：不注入 HAR 环境变量、不创建 `app.har*`、不改既有 `network` 形状；所有旧测试必须原样通过。
- mitmproxy 缺失、端口占用、代理未起、证书拒绝、零可观察 HTTP 请求：不得阻断采集；HAR 状态为 `not_tested` 并给原因/安装提示，不创建空 `app.har`。证书拒绝但存在明文 HTTP 请求时，可以生成只包含真实明文请求的 `app.har`，状态必须为 `partial`、`httpsCoverage="none"`，`notTestedReason` 明确写 HTTPS 未解密；不得写 CONNECT 伪请求。
- 既有 `api_*` 的 `not_tested` 判定仍只基于 `api_effective`；HAR 不参与指标计算，不改 `sessions.net_profile` 可比性维度。

### 2.2 HAR 写入链路（Windows 耐久）

- `har_writer.py` 提供纯标准库实现：
  - `CaptureOptions`（enabled/body 模式/限额/脱敏开关等）、`HarSummary`；
  - `scrub_url`、头/Cookie/敏感查询参数脱敏（默认名单至少 `authorization/proxy-authorization/cookie/set-cookie/x-api-key/api-key/x-auth-token/x-access-token/x-refresh-token/x-csrf-token/x-xsrf-token/x-session-id/token/signature` 及 URL 中 `token/access_token/refresh_token/id_token/auth/authorization/session/sessionid/sid/password/passwd/pwd/secret/api_key/apikey/key/client_secret/sign/signature/sig/code/ticket/nonce/state`），脱敏值统一 `[REDACTED]`；开启 `--capture-har-no-redact` 时仍不得把原值写进 `report.json`/`report.html`；
  - `build_entry(...)`：按 HAR 1.2 生成 `startedDateTime/time/request/response/timings`，保留重复头，URL 与 `queryString` 同步脱敏，头/Cookie 值脱敏，`headersSize=-1`（不伪造），`bodySize` 记真实长度，默认正文只记 `size+mimeType+sha256`（大于 `max_hash_mb=8MiB` 不算摘要并标记），`--capture-har-bodies` 才写正文且每 body 按 `capture_body_limit_bytes` 截断并标记；二进制正文 base64；`response.status=0` 的错误 flow 也要有条目且 `_error` 非空；
  - `HarWriter`：逐行 `json.dumps(ensure_ascii=False)` append 到 `app.har.entries.jsonl`、每行 `flush()`；按 `max_entries/max_bytes` 截断，统计 `droppedEntries/truncatedReason/writerErrors`；写失败不得中断代理，调用方在 `net_proxy.jsonl` 记 `har_error` 事件；
  - `compose_har(entries_path, har_path, *, meta)`：流式读 JSONL，跳过坏行并计数，写同目录 `.tmp` 的合法 HAR 1.2（`log.version="1.2"`, `creator.name="mobile_perf"`, `pages=[]`, `entries`），最后 `os.replace` 原子落盘；无有效条目时**不创建** `app.har`；返回 `HarSummary`。
- `netproxy_addon.py` 通过环境变量 `MOBILE_PERF_HAR`（单行 JSON，含 `entriesPath`、limits、body 模式、脱敏开关）和 `MOBILE_PERF_HAR_ENTRIES`（可省略，必须与 `entriesPath` 一致）获取配置；按同目录文件加载 `har_writer.py`（`sys.path`/`importlib` 均可，但不能依赖主包已安装）。在 `response(flow)`、`error(flow)`、`websocket_end(flow)` 中写入已脱敏 entry；**明文 HTTP 在任何 tier 下都应写**（`response()` 不得保留现有 `if _TIER != "decrypt": return` 对 HAR 的拦截；原有 `api_request` 仍仅 decrypt 档产出）。`http_connect(CONNECT)` 不写 HAR 条目。异常统一捕获并 emit `har_error`，不中断转发；`load()` 的 `params` 事件可增加 `harEnabled` 字段，但不得破坏既有字段。
- `netprof.NetProfiler.__init__` 接受 `capture: CaptureOptions | None`；`start()` 在 `work_dir` 记录 `app.har` 与 `app.har.entries.jsonl` 路径；`_spawn_proxy` 注入 `MOBILE_PERF_HAR` 配置（路径含空格/中文也安全）；`stop()` 先按现有顺序恢复设备/停进程，再 `read_proxy_log`，最后当 `capture.enabled` 时调用 `compose_har`。`NetSession.as_dict()` 增加 `har` 子块：`enabled/status/path/entriesPath/entries/sizeBytes/bodyMode/redacted/truncated/truncatedReason/droppedEntries/skippedEntries/writerErrors/httpsCoverage/notTestedReason`；状态固定为 `not_enabled|not_tested|complete|partial|failed`。原因复用/对齐 `api_not_tested_reason` 与 mitmproxy 缺失 hint。`captureOnly` 时不写 `profile`，但现有代码若依赖 `profile` 键，可给内部 `none` 加 `captureOnly=True`，并在 `kit.py` 写 `sessions.net_profile=None`（这是唯一允许的兼容性调整，须有单测）。
- 不依赖 mitmproxy 的 `done()`/`hardump`；不得让 `process.terminate()` 丢掉已 flush 的 JSONL 行；`NetProfiler.stop()` 幂等，多次调用不重复合成/不残留 `.tmp`。

### 2.3 报告与文档

- `kit.py`：`CollectScope` 透传 capture 选项到 `_start_net_profile`；`finalize()` 在保持原有恢复/停采顺序的前提下，把 `net_session.as_dict()` 写入 `meta.network`（自然带 `har`）；仅 `capture.enabled` 时追加 `store.record_event(kind="capture_har", detail=har 摘要)`。`record_run_artifacts` 可追加 HAR 绝对路径（可选）。`captureOnly` 时 `sessions.net_profile` 为 None。
- `report.py`：`build_report` 无需新增顶层参数（`network` 原样透传）；`_network_markup` 仅在 `network.har` 存在时增加「HAR / 产物 / 正文 / 脱敏 / 截断 / 不可用原因」行，给相对 run 目录的 `network/app.har` 链接；无 `har` 键时输出与现版本逐字节一致。`assemble_run_report` 经 `meta.network` 自动重放 HAR 摘要。
- `README.md` 增加能力说明、CLI 示例、已知限制（HTTPS 需 App 信任代理 CA；QUIC/UDP、非 HTTP TCP、pinning 与未关闭 WebSocket 不在覆盖内）、mitmproxy MIT 可选依赖致谢；`GLOSSARY.md` 增加「APP 抓包产物」或等价术语；`docs/嵌入指南.md` 增库 API 与降级说明；新增 `docs/adr/0005-app-har-capture.md` 记录为什么不采用 `hardump`、流式 JSONL + 原子合成、默认脱敏/元数据正文、抓包与弱网正交。
- 不改 `SCHEMA_VERSION`；报告 JSON/HTML 不内嵌 HAR 正文或任何敏感值。

## 3. 测试要求（必须新增且通过）

1. `tests/unit/test_har_writer.py`：HAR 1.2 结构、`time` 为有效 timings 之和、脱敏（头/Cookie/查询/userinfo）、默认无正文但有 size+sha256、显式正文截断与二进制 base64、条目/字节上限、坏 JSONL 行跳过、无有效条目不产 `app.har`、原子替换无 `.tmp`、`HarSummary` 各状态。使用替身 flow 对象，不 import mitmproxy。
2. `tests/unit/test_netprof_har.py`：环境变量透传（含路径空格/JSON unicode）、`stop()` 合成与幂等、mitmproxy 缺失/零请求/证书拒绝时无空 HAR 或仅明文 partial、`NetSession.as_dict().har` 形状、`net_proxy.jsonl` 仍是唯一生效证据、恢复顺序不变。复用 `tests/unit/test_netprof.py` 的 FakeDevice/FakeProcess 模式但不得 import 测试模块私有符号造成耦合（可复制最小替身）。
3. `tests/unit/test_capture_cli.py`：flag 透传、未开启主开关时报参数错误、限额非法、显式 passthrough 冲突、`--capture-har` 单独使用时加密档且 `net_profile=None`。
4. `tests/unit/test_capture_kit.py`：captureOnly 不写 `sessions.net_profile`、默认关闭零行为、开启后 `meta.network.har`/事件/报告链接存在、异常退出仍恢复设备。可复用 `tests/unit/test_kit_netprof.py` 中公开的 `_patch_profiler` 思路，但用本文件内自足替身；不许真起代理。
5. `tests/unit/test_capture_report.py`：含 har/不含 har 的 HTML 兼容、相对链接、`assemble_run_report` 从 meta 重放 har。
6. `tests/device/test_netprof_device.py`：追加一个 `-m device` 门控用例，按现有双 skipif 模式；覆盖明文 HTTP 本地端点与正式包证书拒绝两分支（可用现有 shell helper，不安装 CA），断言恢复干净。真机用例不得在无设备/无 mitmdump 时计入通过。
7. 运行并保存输出证据：在任务最终回复中写明 `python -m pytest tests/unit tests/contract -q --disable-warnings` 与 `python -m ruff check src tests` 结果；无需把日志写入文件，审查环会独立重跑。

## 4. 完成定义

- 所有白名单文件实现与文档齐备；禁止越界文件；全套 unit/contract 与 ruff 通过；旧 API 行为零破坏；HAR 默认关闭且默认脱敏；Windows 硬终止不丢已写条目；证书拒绝/明文双路径语义如实；代码注释与文档中文、标识符英文，符合现有风格与 ruff 规则。
- 最终回复给出：改动文件清单、关键设计选择、两条验证命令与结果、已知边界。不得声称完成真机验收（由编排方独立执行），不得声称 commit/push。
