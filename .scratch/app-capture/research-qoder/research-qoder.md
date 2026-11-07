RESEARCH: DONE

# APP 抓包（HAR 请求/响应）只读调研报告 — mobile_perf 组件

调研基线：分支 `perf/m6-dev`（HEAD 659fb64），主项目 `D:/workspace/claudecode-test/moblie-xn`。
本报告为**只读调研产物**，未修改主项目任何文件。以下区分两类陈述：
- **【事实】** = 代码可证（附 file:line）；
- **【建议】** = 设计主张，供编排方转化为开发任务书。

---

## 一、现状证据

### 1.1 代理链路全貌（CLI → 报告）

【事实】调用链与关键行号：

| 环节 | 位置 | 要点 |
|---|---|---|
| CLI 参数 | `cli.py:91-119` | `--net-profile/--net-tier/--net-latency/--net-down/--net-up/--net-loss/--net-mitmdump/--net-port`；无 capture 相关参数 |
| CLI 校验 | `cli.py:_parse_net_profile` 312-335 | 早失败：给了覆盖参数没给档位、custom 缺项、非法档位都抛 ValueError；`_run_collect` 捕获后 **exit 1**（262-267） |
| 库入口 | `kit.py:PerfKit.collect` 1471-1558 / `run_collect` 1560-1619 | `net_profile/net_tier/net_mitmdump/net_port` kwargs 透传给 `CollectScope` |
| 作用域 | `kit.py:CollectScope.__init__` 175-226 | 建 `runs/<runId>/` 与 `artifacts/`（215-216） |
| 施加弱网 | `kit.py:CollectScope.__enter__` 255-262 → `PerfKit._start_net_profile` 1293-1317 | `profiler.start(profile, work_dir=run_dir / "network")`（1316）——**代理日志与 stdout 落在 run 目录的 network/ 子目录，在 artifacts 之外** |
| 起代理 | `netprof.py:NetProfiler.start` 709-771 / `_spawn_proxy` 808-839 | 探测 mitmproxy（727-742）→ 端口占用检查（743-745）→ 日志路径 `work_path/net_proxy.jsonl`（749）→ 环境变量注入 `MOBILE_PERF_NETPARAMS/NETTIER/NETLOG`（813-817）→ `Popen([mitmdump, -q, --listen-port, …, -s, netproxy_addon.py])`（818-831，列表参数、无 shell） |
| 停代理 | `netprof.py:NetProfiler.stop` 773-797 | 恢复顺序：**先清系统代理 → 拆 reverse → 停子进程**（777-780，与设置相反）；然后 `read_proxy_log`（785）→ evidence/trust 结论回填（786-791） |
| 收口时序 | `kit.py:CollectScope.finalize` 343-347 | **弱网必须先恢复、再停采集**（注释明示：否则 stop/pull 被自己的代理拖慢）——任何改动不得调换此顺序 |
| 结论入库 | `kit.py:finalize` 497-515（net_meta/api 聚合）、573-598（not_tested 标注）、613-638（meta 落盘，`network` 键在 630）、751-780（`net_profile(_degraded)` 与 `net_profile_decrypt_(un)available` 事件）、842-844（报告 JSON+HTML 落盘） |
| addon | `netproxy_addon.py` 全文 | **只在 mitmdump 子进程里加载**（Python≥3.12 独立 venv，主进程 ≥3.10 不 import，见模块 docstring 1-32 行与 `netprof.py:962` 边界注释） |

### 1.2 addon 各钩子行为与「为什么现在没有 HAR」

【事实】`netproxy_addon.py` 关键状态机：

- 环境变量读取：`_PARAMS/_LOG_PATH/_TIER`（44-46 行），全走 env、不走命令行——**这是刻意规避 Windows 引号问题的既有约定**（docstring 9-15 行）。
- `http_connect()`（220-227）：把客户端 id 记入 `_TUNNEL_CLIENTS` 并 emit `http_connect`。
- `next_layer()`（95-108）：`_TIER != "decrypt"` **或 `_DECRYPT_OK is False`** 时，对 CONNECT 隧道强制 `TCPLayer`（裸 TCP）。
- `tls_established_client()`/`tls_failed_client()`（131-159）：信任探测，**首个结论即定论**（`_PROBE_EMITTED`，116-128）；证书类失败（`_CERT_ERROR_MARKERS`，63-68）→ `_DECRYPT_OK=False` 锁定 → 后续连接全部降直通；非证书类失败**不锁档**（140-147 的 docstring 明确此语义）。
- `response()`（169-187）：首行 `if _TIER != "decrypt": return`——**直通档根本没有 HTTP 层可解密，此钩子不触发**；解密档产出 `api_request`（fid/host/route/status/durationMs/sizeBytes），**不含请求头/响应头/请求体/响应体**。
- `request()`（276-284）/`error()`（287-288）：明文 HTTP 事件与通用错误事件，同样不含 body。
- `_emit()`（71-80）：每事件 open→write→close **逐行立即落盘**，docstring 明确「进程被 kill 也不丢已发生的事件」——这是 Windows 硬杀子进程下的既有 durability 不变量。

【事实】因此 **passthrough / 证书拒绝时没有 HAR 请求/响应** 的因果链是：
1. 直通档：`next_layer` 强制裸 TCP → 隧道内无 HTTP 层 → `response()` 不触发（且钩子自身还有 `_TIER` 守卫）→ 无 URL 级数据可写 HAR。
2. 证书拒绝：首个 TLS 握手失败即 `_DECRYPT_OK=False` → 之后每个 CONNECT 都走裸 TCP；失败的那条流握手断在 TLS 层，也没有完整 flow → **代理侧没有任何可解密的 HTTP 报文，HAR 不可能存在**。`netprof.py:api_not_tested_reason`（574-594）与真机测试 `test_decrypt_tier_degrades_honestly_on_untrusted_app`（`tests/device/test_netprof_device.py:140-187`）已把这一定型为「正确行为而非缺陷」。

【事实】生效判定铁律（不能动）：`netprof.py` docstring 11-14 行与 96 行——**代理侧日志 `net_proxy.jsonl` 是生效的唯一证据**；`NetSession.evidence`（324-331）、`effective`（560-563）、`api_effective`（566-572，必须 decrypt 档 + `TRUST_OK`）全部由此派生。`TRUST_PENDING/UNKNOWN/OK/REJECTED` 四态定义在 100-104 行；not_tested 三分支文案在 574-614 行（证书拒绝必须先于「未生效」判断，理由见 578-582 注释）。

### 1.3 报告/归档/入库契约

【事实】
- `report.py:build_report`（141-215）：`network` 块整段透传并加 `source: "mitmproxy"`（205-206）；`api` 块加 `source: "mitmproxy_decrypt"`（209-210）；`artifacts` 是**字符串路径列表**（177）。`SCHEMA_VERSION = 1`（19 行）。
- `report.py:assemble_run_report`（255-341）：从 `meta.json` 重放时只取 `network=meta.get("network")`（340），**不重放 `api` 瀑布**（既有缺口）；run 目录缺失退化为纯 store 数据（263-264）。→ 【结论】**凡要跨重组活下来的 HAR 元数据，必须进 `meta.network`，而不是只进 `build_report` 参数**。
- `report.py:write_html`（431+）：`_network_markup`（897-931）与 `_api_markup`（853-889）按 report dict 的键存在与否渲染，无 network 块即整节不渲染（`test_report.py:585` 断言此行为）。
- `collector.py`：run 目录布局 `runs/<runId>/{artifacts,meta.json,report.json}`（1-5 行）；`RUN_DIR_PATTERN`（34）；`enforce_retention`（278-322）只 rmtree `run-*` 目录 → **放在 run 目录内的 HAR 自动被保留策略覆盖，无残留风险**。`validate_artifacts`/`aggregate_artifacts` 只 glob `*.csv`（59-69、101-118）→ 非 CSV 文件放 artifacts 不参与聚合，但会混进产物清单；**HAR 应放 `network/` 子目录（与 `net_proxy.jsonl` 同级），不放 artifacts/**（与现有约定一致，见 `kit.py:1316` 注释「artifacts 之外，便于事后核查」）。
- `store.py`：`runs_artifacts(run_id, dir_uri)` 主键去重、`INSERT OR REPLACE`（79-83、395-400）；`events(run_id, ts, kind, detail_json)`（60-66、281-295）。`kit.py:694` 已登记 `[artifacts_dir, run_dir]` 两条 → run_dir 条目已隐含覆盖其内 HAR 文件。

### 1.4 测试基建（可直接复用）

【事实】
- `tests/unit/test_kit_netprof.py:16-91`：`_FakeNetProcess`（用预置 JSON Lines 冒充代理日志）+ `_patch_profiler`（替换 availability/popen/端口探测，**保留真实编排逻辑**）——capture 接线测试可原样扩展。
- `tests/unit/test_netprof.py:1179-1239`：`_addon_probe` 在 **mitmdump 同目录解释器**里加载真实 addon、用 `Marker` 替身验证 `next_layer` 行为，以 `MITMDUMP is None` 跳过（1242 行）——addon HAR 写入逻辑的无设备验证通道。`test_addon_params_and_log_path_reach_subprocess_env`（486 行）是「env 透传到子进程」的现成模式。
- `tests/unit/test_cli.py:30-47`：假 kit 捕获 `run_collect` kwargs 后逐一断言——新增 flag 的直通验证模式。
- `tests/device/test_netprof_device.py:43-47`：双 skipif（设备在线 + mitmdump 存在）的门控写法；140-187 行「两分支都断言」的证书拒绝降级用例是 HAR 真机用例的模板。
- 「代理侧日志是唯一证据」的断言在 `test_netprof.py:443`（`test_session_evidence_comes_from_proxy_log_not_from_app_behaviour`）与恢复顺序断言 `test_netprof.py:300`、`test_kit_netprof.py:191`——新增用例不得绕开这些不变量。

---

## 二、最小改动清单（设计建议，≥10 条文件级建议）

> 总原则【建议】：capture 是**纯附加输出分支**——不改 `next_layer`/TLS 钩子时机、不改既有事件、不改弱网参数校验、不改恢复顺序、不改 not_tested 语义；HAR 可用性沿用 `api_effective` 同一道闸（decrypt + TRUST_OK），原因文案复用 `api_not_tested_reason`。

**建议 1｜netproxy_addon.py：新增独立 HAR 事件流（不污染唯一证据日志）**
【事实】现在 `analyze_proxy_log`（`netprof.py:351-431`）对未知事件是静默跳过的，往里塞大 body 事件虽不致错但会稀释「唯一证据」日志的信噪比。
【建议】新增环境变量 `MOBILE_PERF_HARCFG`（JSON：`bodyLimitBytes/maxBytes/redact`）与 `MOBILE_PERF_HARLOG`（如 `net_har_entries.jsonl`）；在 `response()`（169-187）现有 `api_request` emit **之后**追加 `_emit` 一条 `har_entry`（含 method/url/status/请求响应头/截断后的 body base64 或文本/timings），复用 `_emit` 的逐行落盘保证 kill 不丢。维护模块级字节预算 `_HAR_BUDGET`，超 `maxBytes` 后只丢 body 字段并标 `truncated:true`。头部脱敏在 **addon 侧**完成（`Authorization/Cookie/Set-Cookie/Proxy-Authorization/X-Api-Key` → `"<redacted>"`），保证未脱敏内容从不落盘。
验收标准：capture 关闭（env 缺省）时 `net_har_entries.jsonl` 不创建、`net_proxy.jsonl` 内容与现版本逐字节等价；开启时 `har_entry` 条数 == `api_request` 条数。

**建议 2｜netproxy_addon.py：`load()` 的 params 首行附注 capture 状态**
【建议】91 行 `tier=_TIER` 旁加 `har=bool(_HARCFG.get("enabled"))`——报告可复述口径，与现有「整形参数逐条写进日志首行供报告复述」的约定（16-17 行 docstring）一致。

**建议 3｜netprof.py：新增 `CaptureConfig` 与校验函数（与 `build_profile` 同风格）**
【建议】仿 `NetProfile`/`build_profile`（118-180 行）加 frozen dataclass `CaptureConfig(enabled, body_limit_bytes=8192, max_bytes=8*1024*1024)` 与 `build_capture_config(...)`：负数、上限超过某硬顶（如单条 body 1MiB）一律 ValueError——「宁可早失败也不静默用错误配置跑采集」。
验收：单测覆盖未知参数、负值、超限；`enabled=False` 时其余字段被忽略。

**建议 4｜netprof.py：`NetProfiler` 接 capture、`stop()` 组装 HAR**
【建议】`__init__`（652-668 行）加 `capture: CaptureConfig | None = None`，构造时校验 **capture 只允许与 `TIER_DECRYPT` 并存**（否则 ValueError，与 669-670 行未知 tier 拒绝同风格）；`_spawn_proxy`（808-839 行）注入两个新 env；`start()` 记录 `self._har_entries_path = work_path / "net_har_entries.jsonl"`、`self._har_path = work_path / "requests.har"`；**`stop()` 在 `read_proxy_log` 之后（785 行附近）**，仅当 `capture.enabled and session.api_effective` 时读 entries 文件、纯函数 `build_har(lines) -> dict`（坏行跳过、缺文件返回空，仿 `analyze_proxy_log` 容错）组装成 HAR 1.2 结构一次性写 `requests.har`（tmp 文件 + `os.replace` 原子落盘）；否则**不产出 requests.har**，只在 NetSession 上记 `har.status` + `not_tested` 原因。
验收：`build_har` 为纯函数可单测（不依赖 mitmproxy）；passthrough/证书拒绝/mitmproxy 缺失三种会话结束后目录里没有半成品 `requests.har`。

**建议 5｜netprof.py：`NetSession.as_dict()` 增加 `har` 子块（关键：借道 meta 活过报告重组）**
【建议】616-639 行的 as_dict 增加 `"har": {"status": "captured|not_tested|disabled", "path", "entries", "bytes", "truncated", "redactedHeaders": [...], "notTestedReason": …}`；`notTestedReason` 直接取 `api_not_tested_reason`（574-594 行），不新写一套原因文案。
理由【事实】：`kit.py:630` 把 `net_session.as_dict()` 整体存进 `meta["network"]`，`assemble_run_report` 经 `meta.get("network")`（report.py:340）原样重放——**这是兼容旧 run、且不需要 build_report 新参数的唯一通道**。
验收：旧 run 的 meta.json 无 `har` 键时，重组与渲染均不报错（渲染层按键存在判断，参照 `test_report.py:585` 的省略断言）。

**建议 6｜kit.py：三层签名各加一个透传参数**
【建议】`PerfKit.collect`（1471-1489）/`run_collect`（1560-1578）/`CollectScope.__init__`（175-192）各加 `capture_har: bool = False, capture_body_limit_bytes: int | None = None, capture_max_bytes: int | None = None`；`__enter__`（255-262）把构造好的 `CaptureConfig` 交给 `kit._start_net_profile`（1293-1317 加参数），后者传给 `NetProfiler`。校验时机与 `net_profile` 相同：**collect() 入口早失败**（1537-1541 行旁边），`capture_har=True` 但 `net_tier != decrypt` 或未给 `net_profile` 直接抛 ValueError。
【建议】finalize 不改恢复/停采时序（343-347 行）；仅在 751-780 事件段之后按同风格补一条 `store.record_event(kind="capture_har", detail=har 摘要)`（仅当 capture 开启），使「这次有没有 HAR、为什么没有」不翻 meta 就能看出。`record_run_artifacts`（694 行）可选追加 har 绝对路径——`runs_artifacts` 主键去重（store.py:79-83），重放安全。

**建议 7｜cli.py：三个 flag + 早失败校验 + 退出码约定**
【建议】`collect_parser`（91-119 行区）加 `--capture-har`（store_true，**默认关闭**）、`--capture-body-limit-bytes`、`--capture-max-bytes`；`_run_collect` 仿 `_parse_net_profile`（312-335）加 `_parse_capture(args)`：
- `--capture-har` 未配 `--net-profile` → 报错「抓 HAR 必须走代理，请指定 --net-profile（可先用 none 档）」，exit 1；
- `--capture-har` 且 `--net-tier passthrough`（默认值，cli.py:100-103）→ 报错「抓 HAR 需要解密档，请显式 --net-tier decrypt」，exit 1。**不建议静默改档**——档位是可比性维度（工单 20 的口径），显式失败符合仓库「宁可早失败」风格（netprof.py:155-158 docstring）；
- 限额参数负数/非法 → exit 1（模式同 cli.py:262-267）。
退出码语义【建议】：参数错误 → 1；采集正常完成但 HAR 因运行时原因不可得（mitmproxy 缺失/证书拒绝/零流量）→ **仍 exit 0**，报告与 `capture_har` 事件里如实 not_tested——与现有弱网降级哲学完全一致（未生效不抛错，`__enter__` 253-254 行注释）。
`netprof probe`（340-343）输出可顺带加 `"harCapable"` 字段（= available），属附加字段。

**建议 8｜report.py：`_network_markup` 增 HAR 小节，`SCHEMA_VERSION` 保持 1**
【建议】926-931 行（网络节 rows 组装处）后追加：有 `network.har` 时渲染「HAR 产物 / requests.har 路径、条目数、截断与脱敏口径」或「HAR 不可用原因」行；无该键完全不渲染（旧报告零变化）。不 bump schema——纯附加字段，`assemble_run_report` 无需新参数（见建议 5 通道）。可选顺带项：`assemble_run_report` 补 `api=…` 重放（现状缺口，340 行只重放 network），但**不属本工单必改项**。
验收：`test_report.py:550/585` 同类断言扩展——有 har 才出该段、无 har 段落与现在逐字节一致。

**建议 9｜collector.py / store.py：零 schema 改动**
【事实+建议】HAR 落 `runs/<runId>/network/`（kit.py:1316 的 work_dir），`enforce_retention` 的 rmtree（collector.py:317-319）自动纳入保留策略；`runs_artifacts` 已有 run_dir 行（kit.py:694）。**两文件不改**是本方案的安全性证明——任何要求改这两个文件的方案都应被质疑。

**建议 10｜netprof.py：Windows shim 修正（前置小缺陷，capture 会放大它）**
【事实】`find_mitmproxy`（220-256）候选池经 `shutil.which`，Windows 上可能命中 venv `Scripts\mitmdump.cmd`；`_probe_version`（259-271）与 `_spawn_proxy` 的 `Popen`（829-831）都不带 shell，**`.cmd` 在 Windows 上不可直接 exec**。现有单测全部注入 fake probe 所以从未暴露；真机若 PATH 顺序变化会命中此雷。
【建议】`os.name == "nt"` 时过滤掉 `.cmd/.bat` 候选，或在 hint（250-256）里给出指向 `…\Scripts\mitmdump.exe` 的可执行提示。capture 功能把「用户需要正确装 mitmdump」的门槛提高了，此项应并入首期。

**建议 11｜文档与口径（设计方案-M6 / GLOSSARY）**
【建议】HAR 口径写进 `docs/设计方案-M6-GPU与弱网.md` 追加节：仅解密档且 TRUST_OK 才产出；直通/拒绝/未安装三态降级文案；脱敏清单；「HAR 是 api_request 明细的超集、非独立证据」定位（生效唯一证据仍是 `net_proxy.jsonl`）。

---

## 三、风险与降级

| # | 风险 | 证据/推理 | 缓解与编排方复验检查点 |
|---|---|---|---|
| R1 | **passthrough/证书拒绝时用户误以为该有 HAR** | §1.2 因果链【事实】 | har.status=not_tested + 复用 `api_not_tested_reason` 文案；`capture_har` 事件单独入库。复验：跑一次 `--net-tier passthrough --capture-har`（应早失败 exit 1，见建议 7）与一次真机证书拒绝，报告均出现明确原因 |
| R2 | **子进程被硬杀 → JSON 半成品** | Windows `terminate()/kill()`（netprof.py:841-854）无优雅退出；addon 无 finalize 钩子可依赖 | 采用「addon 逐行 append JSONL + 主进程 stop() 组装完整 HAR 一次写（tmp+os.replace）」——沿用 `_emit` 的 kill 不丢不变量（71-80 行），主进程是**唯一**写 .har 的人。复验：kill -9 mitmdump 后 `net_har_entries.jsonl` 每行可解析、无 `requests.har` 残片 |
| R3 | **mitmdump.exe / .cmd shim** | 建议 10【事实】 | 过滤扩展名 + probe 失败换下一候选的现有循环（242-247）兜底。复验：`python -m mobile_perf.cli netprof probe` 在装了 venv 的 Windows 机器上 `executable` 以 `.exe` 结尾 |
| R4 | **Git Bash 引号/路径空格** | addon docstring 9-15 行【事实】：参数全走 env 就是为了绕 Windows 引号问题；`Popen` 用列表参数不经 shell | 新配置继续走 env（`MOBILE_PERF_HARCFG` JSON），**不得**改为 `--set` 命令行传值。复验：`--runs-dir "D:\有 空格\runs"` 跑通且 `requests.har` 路径正确 |
| R5 | **编码（GBK 家族干扰）** | `collector.py:26` SoloPi CSV 用 gb18030【事实】；addon `_emit` 用 utf-8 + ensure_ascii=False（78 行）【事实】 | HAR 组装/落盘显式 `encoding="utf-8"`；二进制 body 一律 base64 并带 mimeType，禁止猜测文本。复验：抓一条含中文响应 + 一条图片响应，.har 用 `json.loads(..., encoding utf-8)` 可解析 |
| R6 | **磁盘与保留** | `enforce_retention` rmtree run 目录【事实】 | `max_bytes` 默认 8MiB、超限后保头去 body 并标 truncated；不新增目录外的产物。复验：故意设 `--capture-max-bytes 1024` 跑真实流量，文件存在且 ≤ 限额+头部开销，`truncated:true` |
| R7 | **敏感信息外泄** | 现日志只有 host/route/size【事实】；HAR 首次引入 header/body 全文 | 脱敏发生在 addon（未脱敏内容从不落任何盘）；脱敏清单写进报告。复验：真机抓包后 grep `Authorization` 值不出现在 net_proxy.jsonl、har_entries、requests.har 任何一处 |
| R8 | **弱网可比性被破坏** | 档位参数是可比性维度（工单 20，`profiles_are_comparable` netprof.py:183-194）【事实】 | capture 不新增档位、不改 `PROFILE_PRESETS`；带 capture 的 run 与不带的 run 在 `net_conn_duration_ms` 口径可比（同一代理路径）。复验：同设备同档位两次 run（capture 开/关），`trend --net-profile weak` 正常出趋势 |
| R9 | **restore 时序被改动牵连** | `finalize` 343-347、`stop` 777-780【事实】 | 本方案所有新逻辑挂在 stop() 日志解析**之后**，设备恢复路径零改动。复验：现有 `test_collect_net_profile_restores_device_even_when_body_raises`（test_kit_netprof.py:191）必须原样绿 |
| R10 | **api_breakdown 重组缺口被误当成 HAR 缺口** | `assemble_run_report` 不重放 api（report.py:324-341）【事实】 | HAR 元数据走 `meta.network.har` 通道（建议 5）刻意绕开该缺口；若编排方顺手修 api 重放，另立工单 |

---

## 四、测试计划（文件位置 + 关键断言）

> 铁律【事实】：新增用例不得绕开「代理侧日志是生效唯一证据」（test_netprof.py:443）与恢复顺序断言（test_netprof.py:300、test_kit_netprof.py:191）。

1. **tests/unit/test_netprof.py**（addon 与编排单元）
   - `build_capture_config` 校验族：负数/非法/关闭态忽略参数（仿 26-110 行 build_profile 用例组）。
   - env 透传：`MOBILE_PERF_HARCFG/MOBILE_PERF_HARLOG` 到达子进程 env（复用 486 行 `test_addon_params_and_log_path_reach_subprocess_env` 模式）。
   - `build_har()` 纯函数：正常条目→HAR1.2 结构、坏行跳过、缺文件→空、truncated 标记、脱敏字段原样透传。
   - `stop()` 矩阵：decrypt+trusted→产出 requests.har；passthrough→**不产出**且 status 说明原因（文案含「直通档不解密」，对齐 586 行）；证书拒绝→不产出、原因取 `api_not_tested_reason`（589-591 行）；mitmproxy 缺失→不产出、hint 保留。
   - `_addon_probe`（1205-1239）扩展：har enabled 时 `response()` 同时 emit `api_request` 与 `har_entry` 且条数一致；har 关闭时 addon 行为与现版本一致（`net_proxy.jsonl` 无新行）。
2. **tests/unit/test_kit_netprof.py**（接线，扩展 `_patch_profiler` 写假 entries jsonl）
   - capture 关→`report.network` 无 har 子块、meta.json 无该键（旧形状兼容）。
   - capture 开+假 TRUST_OK→`runs/<runId>/network/requests.har` 存在、`meta.network.har.path` 指向它、`events` 有 `capture_har`。
   - with 块体抛异常→设备恢复三步仍全 True（复用 191 行用例骨架）且 har 组装不炸。
   - `capture_har=True + net_tier=passthrough`→collect() 入口 ValueError（早失败，仿 330 行 `test_collect_rejects_unknown_net_profile_early`）。
3. **tests/unit/test_cli.py**（假 kit 捕获 kwargs，模式见 30-47 行）
   - 三 flag 组合 → `run_collect` kwargs 逐一对账。
   - `--capture-har` 缺 `--net-profile` / 缺 `--net-tier decrypt` / 限额负数 → exit 1 且 stderr 含下一步指引。
4. **tests/unit/test_report.py**
   - `build_report` 对含 har 子块的 network 原样透传（534 行模式）。
   - `write_html`：有 har 渲染产物行；无 har 与 585 行既有断言一致（整段不变）。
   - `assemble_run_report`：从 meta.json 重放出 har 子块（旧 run 无该键不报错）。
5. **tests/device/test_netprof_device.py**（沿用 43-47 双 skipif 门控）
   - `test_capture_har_real_device`：decrypt 档驱动真实流量后，**两分支都断言**（模板即 140-187 行）：TRUST_OK → requests.har 存在、entries>0、Authorization 值已脱敏、har entries 行数与 net_proxy.jsonl 的 api_request 行数一致（代理日志仍是唯一证据）；TRUST_REJECTED → 无 requests.har、`capture_har` 事件给出证书原因。两分支共同断言：恢复三步 True + `doctor` 复验干净（102-112 行模式），证明 capture 未扰动弱网与恢复语义。

---

## 五、真机验收清单（编排方复验步骤）

1. `python -m mobile_perf.cli netprof probe` → `available:true` 且 `executable` 以 `mitmdump.exe` 结尾（R3）。
2. 基线回归：`pytest tests/unit/test_netprof.py tests/unit/test_kit_netprof.py tests/unit/test_report.py tests/unit/test_cli.py` 全绿（未加新用例前也必须全绿——capture 默认关闭时零行为变化）。
3. `collect --net-profile none --net-tier decrypt --capture-har …`：`runs/<runId>/network/` 下同时有 `net_proxy.jsonl`、`net_har_entries.jsonl`、`requests.har`；`requests.har` 可被 `json.load` 解析、`log.entries` 数与 report.network.har.entries 一致。
4. 降级三态：a) 停掉 mitmdump 场景（unset env/改名）→ 采集完成 exit 0、har.status=not_tested 含安装 hint；b) 正式包证书拒绝 → 无 requests.har、原因含「不信任代理 CA」且**连接级指标仍有值**（现有降级不破坏，netprof.py:578-582 语义）；c) `--net-tier passthrough --capture-har` → exit 1 早失败。
5. 脱敏抽查：`grep -i "authorization\|cookie" network/*` 只见 `<redacted>`（R7）。
6. 设备卫生：验收会话全部结束后 `netprof doctor --port 8080` → `proxyClean:true, reverseClean:true, cleaned:false`（无残留）。
7. 可比性：同档位带/不带 capture 各跑一次，`trend <pkg> net_conn_duration_ms --net-profile weak` 两条 run 同组出趋势（R8）。
8. 硬杀耐久：采集途中 `taskkill /F mitmdump` → 会话正常收口、JSONL 每行可解析、无半成品 .har（R2）。

---

### 一句话结论

在不触碰弱网档位、信任探测与 not_tested 三条语义的前提下，最小侵入路径是：addon 在 `response()` 追加独立 `har_entry` JSONL 事件流（脱敏在代理侧完成）→ `NetProfiler.stop()` 读日志后组装 `runs/<runId>/network/requests.har` → HAR 摘要挂进 `NetSession.as_dict()` 的 `har` 子块从而借道 `meta.network` 免改 `build_report` 契约、兼容旧 run → CLI 以默认关闭的 `--capture-har` 显式要求 decrypt 档并早失败，报告/入库仅做附加字段与一条 `capture_har` 事件。
