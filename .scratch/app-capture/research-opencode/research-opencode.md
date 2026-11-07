RESEARCH: DONE

# APP 抓包（HAR 请求/响应）开源方案调研

- 调研对象：`mobile_perf` 组件的「APP 抓包」能力，产出 HAR 1.2 供 Chrome DevTools / 分析脚本使用。
- 调研日期：2026-11-07。
- 证据标记：【本机】= 本机源码/运行结果已核实；【文档】= 公开文档/仓库文件已核实（给出 URL 与查看日期）；【推断】= 基于源码结构或行业惯例的合理推断，未实测。
- 本报告为只读产物，未修改主项目任何源码、配置、数据或设备状态。

---

## 0. 结论先行

**推荐方案 B：在现有 `netproxy_addon.py` 的 `response()` / `error()` / `websocket_end()` 钩子里，按「写入即脱敏」的方式把单条 HAR 1.2 entry 追加到 `network/app.har.entries.jsonl`（每行一条、写完 flush），采集停止后由主进程 `netprof` 把 JSONL 流式合成标准 `network/app.har`（先写 `.tmp` 再原子替换）。**

理由三条：

1. **方案 A（mitmproxy 自带 `savehar` / `--set hardump=...`）只在进程优雅退出时写文件，且把全部 flow（含完整 body）留在内存。** 本机源码已确认：flows 存 `self.flows` 列表、`done()` 才落盘；本机 `netprof._stop_proxy()` 用的是 `process.terminate()`，Windows 上等价于 `TerminateProcess`，`atexit`/`done()` 不会执行（本机实验已复现），`hardump` 在 Windows 上会**直接丢文件**。【本机】
2. **方案 A 默认不脱敏**：请求/响应头、Cookie、查询串、body 文本原样写入。这与本项目「报告只放已脱敏结论、不含流量内容」的既有约定冲突（`report.py:203-206` 注释明写）。【本机】
3. **方案 B 可复用现有可信链路**：`adb reverse` + `settings global http_proxy` + `netproxy_addon` 的 tier/trust 探测已经就绪，改动集中在「多写一份结构化文件 + 报告多几行」，不引入新进程、新语言、新许可证负担。

**放弃项**：HTTP Toolkit（AGPL-3.0，桌面 Node 应用，需注入系统 CA，集成成本高）、PCAPdroid（GPL-3.0，端侧 VPN，绕开现有代理链路）、PacketCapture / Reqable（闭源或未核实，不满足开源与可审计要求）。详见 §5。

**默认开关：关闭。** `--har` 显式开启；库 API 传 `har=True` 或 `HarOptions(...)`。原因：HAR 含业务敏感数据、体积大、且要求 MITM 解密（增大侵入性），不能作为默认行为。

---

## 1. 现状盘点（为什么改动可以很小）

### 1.1 现有链路【本机】

```
PerfKit.collect / run_collect
  └─ CollectScope.__enter__
       └─ PerfKit._start_net_profile(profile, run_dir, mitmdump, port, tier)
            └─ NetProfiler.start(profile, work_dir=run_dir/"network")
                 ├─ 找 mitmdump（find_mitmproxy）
                 ├─ Popen([mitmdump, -q, --listen-port, N,
                 │         --set connection_strategy=lazy,
                 │         -s netproxy_addon.py], env=...)
                 ├─ adb reverse tcp:N tcp:N
                 └─ adb shell settings put global http_proxy 127.0.0.1:N
  └─ CollectScope.finalize
       ├─ netprofiler.stop()          # 清代理→拆 reverse→停进程→读日志
       ├─ collector.aggregate_artifacts / validate_artifacts
       ├─ report.build_report(..., network=net_meta, api=api_breakdown)
       └─ collector.write_meta → report.write_report / write_html
```

关键文件与职责：

| 文件 | 现有职责 | 与 HAR 的关系 |
| --- | --- | --- |
| `src/mobile_perf/netprof.py` | 起停代理、准备环境、解析代理日志、聚合 `api_*`、组装 `NetSession` | 新增 HAR 配置下发、停止时合成 HAR、`NetSession.har` 摘要 |
| `src/mobile_perf/netproxy_addon.py` | mitmdump 子进程内：整形 + TLS 信任探测 + JSONL 事件 | 新增 HAR entry 构造与追加写 |
| `src/mobile_perf/kit.py` | 门面编排，`network` 段与 `api` 段进报告 | 透传 `har` 参数、把 `har` 摘要放进 `network` |
| `src/mobile_perf/cli.py` | `collect` 参数解析与早失败 | 新增 `--har*` 参数与校验 |
| `src/mobile_perf/report.py` | `build_report` / `_network_markup` | `network.har` 原样透传 + HTML 增加几行 |
| `src/mobile_perf/collector.py` | run 目录 `run-YYYYmmdd-HHMMSS-xxxx` | 不新增目录，复用 `network/` |

### 1.2 现有 run 目录形状（本机实跑产物）【本机】

```
runs/<runId>/
├── artifacts/                 # SoloPi 拉回的 CSV（原样保存）
├── network/
│   ├── net_proxy.jsonl        # 代理侧唯一生效证据（JSON Lines）
│   └── net_proxy.stdout.log   # mitmdump stdout/stderr
├── meta.json
├── report.json
└── report.html
```

`kit.py:1316` 已经把工作目录固定为 `run_dir / "network"`，HAR 直接落这里即可，不需要新目录、不需要改保留策略（`enforce_retention` 按 run 目录清理，HAR 随 run 一起清理）。

### 1.3 现有 tier / trust 语义【本机】

- `TIER_PASSTHROUGH`：addon 在 `next_layer()` 里强制 `TCPLayer`，只观察 TLS 隧道，拿不到 URL/状态码。
- `TIER_DECRYPT`：放行给 mitmproxy TLS 层；`tls_established_client` / `tls_failed_client` 做**一次**信任探测，证书类失败判定 `TRUST_REJECTED` 后本 run 降为直通、不重试。
- 报告/指标层：`api_effective` 要求 `effective && decrypt && trust == ok`；否则 4 个 `api_*` 全部 `not_tested` 并写明原因（`kit.py:585-598`）。

**HAR 必须沿用同一套语义**：没有解密就没有 HTTPS 请求/响应，不能产出「看起来抓到了」的空壳 HAR。

---

## 2. 方案 A：mitmproxy 自带 `savehar` / `hardump`

### 2.1 源码事实（本机 12.3.0）【本机】

本机路径：`D:/workspace/claudecode-test/moblie-xn/.scratch/venv-mitm/Lib/site-packages/mitmproxy/addons/savehar.py`
本机版本：`mitmdump --version` → `Mitmproxy: 12.3.0 / Python: 3.12.10 / Windows-11`。

| 关注点 | 源码位置 | 事实 |
| --- | --- | --- |
| 开关 | `savehar.py:78-88` | 选项名 `hardump`，默认 `""`；帮助文本明写「Save a HAR file with all flows on exit」「enabling this option will mean that flows are kept in memory」 |
| 内存 | `savehar.py:32`、`116-120` | `self.flows: list[flow.Flow]`；每个匹配的 flow 整体 append |
| 写入时机 | `savehar.py:122-128` | 只在 `done()` 写；`hardump == "-"` 时打印到 stdout |
| 触发保存 | `savehar.py:104-114` | `response()`（websocket 为 None 时）、`error()`、`websocket_end()` 入列表 |
| 过滤 | `savehar.py:90-98` | 复用 `save_stream_filter`（flowfilter 表达式），无状态码/域名等语义过滤 |
| HAR 版本 | `savehar.py:65-76` | `log.version = "1.2"`、`creator.name = "mitmproxy"`、`pages: []` |
| 请求头/Cookie/查询串 | `savehar.py:241-252`、`311-312` | 原样 `format_multidict`，**无任何脱敏** |
| 响应体 | `savehar.py:180-213` | `flow.response.content`（解压后）判断 `is_mostly_bin`，二进制 base64；文本用 `get_text(strict=False)` 全量写入 |
| 请求体 | `savehar.py:258-264` | 仅 `POST/PUT/PATCH` 写 `postData`，`text` 为全量文本 |
| 二进制判定 | `utils/strutils.py:128+` | 只看前 ~100 字节，短文本/带 NUL 的文本可能被误判 |
| CONNECT | `savehar.py:231-234` | `url = f"https://{flow.request.pretty_url}/"`，造一条「HTTPS 请求」条目 |
| 错误请求 | `savehar.py:214-229` | 无响应时 `status=0`、`content={}`、`_error=flow.error.msg`（`_error` 是 mitmproxy 扩展字段，非 HAR 1.2 标准） |
| HTTP/2 | 透传 `flow.*.http_version` | 值为 `HTTP/2.0`（mitmproxy 口径），不是 Chrome 常用的 `h2`【推断：DevTools 可容忍】 |
| WebSocket | `savehar.py:269-285` | `_resourceType="websocket"`、`_webSocketMessages`（Chrome 扩展字段），消息全量 |
| 非 HTTP flow | `savehar.py:59-63` | TCP/UDP/DNS 直接跳过，只 `logger.info` 计数 |
| headersSize | `savehar.py:202`、`248` | `len(str(headers))`。`Headers.__str__` 未重写，实际是 `Headers[(b'...', b'...')]` 的 repr；本机实测 2 个头的 repr 长 68，真实报文头约 40 字节 → **不是字节数** |
| content.size | `savehar.py:185-199` | 用的是 `raw_content`（压缩后）长度，HAR 规范要求是**解压后**长度，存在口径偏差 |

### 2.2 「进程结束才写」在本项目会丢文件【本机 + 实验】

- `NetProfiler._stop_proxy()`（`netprof.py:841-854`）调用 `process.terminate()`，超时才 `kill()`。
- Windows 上 `Popen.terminate()` = `TerminateProcess`，不会给子进程任何清理机会。
- 本机临时实验（Windows 11 / Python 3.12）：子进程注册 `atexit` 回调，父进程 `terminate()` 后子进程退出码 1，回调未执行、目标文件不存在。
- 因此 `hardump` 在「Windows 本机 + 现有 stop 实现」下**不会产出 HAR**。POSIX 上 `SIGTERM` 会被 mitmproxy 的 `tools/main.py:126-133` 转成 `master.shutdown()`，`Master.done()`（`master.py:88-94`）再触发 `DoneHook`，`savehar.done()` 才执行——**跨平台行为不一致**。【本机源码 + 推断】

### 2.3 其它硬伤

1. **内存无上限**：一条 200 MB 的视频下载也会整体留在 `self.flows`，直到退出才写。
2. **无脱敏、无截断**：`Authorization` / `Cookie` / `Set-Cookie` / URL 里的 `token` / body 全量落盘。
3. **写文件是「一次性 `json.dumps` 整个对象」**（`savehar.py:39`），峰值内存再翻倍；中途崩溃留下半个 JSON。
4. **空 HAR 也会写**：直通档下 mitmproxy 只有 `TCPFlow`，全部被跳过，`entries=[]`，仍会生成一个「合法但误导」的空 HAR。
5. **CONNECT 伪造条目**：把隧道写成 `https://host:443/`，在 DevTools 里看起来像一次真实请求。

**结论：A 只能作为「本机 POSIX 手工调试」的临时手段，不能作为组件能力。** 不建议实现 `--har-mode native` 之类的兼容开关，避免两套语义。

---

## 3. 方案 B：现有 addon 内「脱敏后流式写 + 停止时合成」

### 3.1 数据流

```
mitmdump 子进程（netproxy_addon.py）
  response(flow)        ─┐
  error(flow)           ─┼─→ har_writer.build_entry(...)   # 纯函数，已脱敏、已限额
  websocket_end(flow)   ─┘        │
                                  ├─ 追加一行 JSON 到 network/app.har.entries.jsonl
                                  └─ flush()（每行落盘；进程被 kill 不丢已写完的条目）
主进程（netprof.NetProfiler.stop）
  ├─ _stop_proxy()                       # 现有：terminate + wait
  ├─ compose_har(entries, app.har)       # 新增：逐行读→校验→流式写 .tmp→os.replace
  ├─ read_proxy_log(...)                 # 现有：证据/trust/api 记录
  └─ NetSession.har = HarSummary(...)    # 新增：状态、条目数、截断原因
```

**为什么先写 JSONL 再合成，而不是直接增量写 HAR：**

- HAR 是一个 JSON 对象，必须有闭合的 `]}}`；进程被 kill 时文件必然不完整。
- JSONL 每行独立、天然可截断；合成阶段可以跳过坏行并计数，保证「能打开、能导入」。
- 合成是流式的（读一行写一行），不会把全部 entry 再读进内存。
- 旁路文件本身也是可审计的中间产物：报告能说清「丢了几条、为什么丢」。

### 3.2 文件命名与位置

| 文件 | 说明 | 是否默认生成 |
| --- | --- | --- |
| `runs/<runId>/network/app.har` | 最终 HAR 1.2，供 DevTools 导入 | 仅 `--har` 且解密可用时 |
| `runs/<runId>/network/app.har.entries.jsonl` | 已脱敏的单条 entry（崩溃恢复/审计） | 同上 |
| `runs/<runId>/network/app.har.tmp` | 合成中间文件，成功后 `os.replace` 掉 | 瞬时 |
| `runs/<runId>/network/net_proxy.jsonl` | 既有证据日志，**不改格式**；新增 `har_*` 事件只追加 | 始终（现有） |

命名理由：`app.har` 与 `net_proxy.jsonl` 同目录、同前缀语义清晰；`entries.jsonl` 明确是中间产物，不会被误当正式 HAR 导入。

### 3.3 开关、CLI 与库 API

**默认关闭。** 三处入口：

CLI（`mobile-perf collect`）：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--har` | 关 | 开启 HAR 抓包；隐含要求解密档 |
| `--har-body {none,meta,text}` | `meta` | `meta`=只记大小/摘要；`text`=记正文（有上限） |
| `--har-max-body-kb` | `64` | 单条正文/响应体文本上限（仅 text 模式） |
| `--har-max-entries` | `5000` | 条目总数上限 |
| `--har-max-mb` | `64` | `app.har` 总大小上限 |
| `--har-max-hash-mb` | `8` | 超过此大小的 body 不算 sha256（避免大文件 CPU 抖动） |
| `--har-redact-key` | 可重复 | 追加敏感 key（头名/查询参数名），正则或字面量 |
| `--har-no-redact` | 关 | **危险**：关闭脱敏，仅限本地排查；报告会显著标注 |

校验规则（早失败，避免跑完才发现没意义）：

1. `--har` + `--net-tier passthrough` → `ValueError`，提示「HAR 需要 `--net-tier decrypt`」。不自动改档，避免静默 MITM。
2. `--har` 且未给 `--net-profile` → 内部按 `none` 档起代理（仍走 `adb reverse` + 系统代理，只是不整形）。HAR 是抓包能力，不应强迫用户先选弱网档位。库入口同理：`har.enabled and net_profile is None` 时构造 `build_profile("none")`，并把 `CollectScope.__enter__` 的启动条件从「有档位」改为「有档位或开了 HAR」，避免因为没有弱网档位而跳过代理。
3. `--har` 且 mitmproxy 不可用 → 不阻断采集，`network.har.status = "not_tested"`，原因「mitmproxy 不可用」+ 现有安装提示。
4. `--har-no-redact` 与 `--har-body text` 同时出现 → 允许，但报告写 `redaction: "disabled"`、`bodyMode: "text"`，HTML 用醒目行提示。

库 API（`PerfKit.collect` / `run_collect`）：

```python
scope = kit.collect(
    target="com.example.app",
    items=["CPU", "Memory"],
    net_profile=None,          # None + har=True → 自动用 none 档
    net_tier="decrypt",
    har=True,                  # 或 HarOptions(...)
)
# 细粒度：
har = netprof.HarOptions(
    enabled=True, body="meta", max_body_kb=64,
    max_entries=5000, max_mb=64, redact=True,
    extra_sensitive_keys=("x-tenant-token",),
)
```

`har` 参数从 `CollectScope` 透传到 `_start_net_profile` → `NetProfiler(har=har)`；`run_collect` 增加同名参数。**不新增全局状态**，默认值集中在 `HarOptions`。

下发给 addon 用环境变量（沿用现有 `MOBILE_PERF_NETPARAMS` 的做法，避开 Windows 引号问题；实际必须是单行 JSON，下面为可读性折行）：

```text
MOBILE_PERF_HAR={"entriesPath":".../app.har.entries.jsonl","body":"meta",
                 "maxBodyBytes":65536,"maxEntries":5000,"maxBytes":67108864,
                 "redact":true,"extraKeys":["x-tenant-token"]}
```

### 3.4 HAR 1.2 条目构造

每条 entry 由纯函数 `build_entry(...)` 生成，只依赖标准库，**不 import mitmproxy**，便于在主 venv（Python ≥3.10，无 mitmproxy）里做单元测试。

| 字段 | 取值 | 备注 |
| --- | --- | --- |
| `startedDateTime` | `datetime.fromtimestamp(request.timestamp_start, timezone.utc).isoformat()` | 带时区，符合 HAR 示例 |
| `time` | `sum(v for v in timings.values() if v >= 0)` | 与规范「非 -1 之和」一致 |
| `request.method/url/httpVersion` | 原值；URL 先过脱敏 | `url` 与 `queryString` 必须一致 |
| `request.cookies` | name 保留、value 打码 | 来自 `request.cookies` |
| `request.headers` | 二元组列表（保留重复头） | 敏感头打码；长值截断 |
| `request.queryString` | 解析 `urlsplit` 后的 `parse_qsl(keep_blank_values=True)` | 敏感 key 打码 |
| `request.headersSize` | `-1`（不伪造） | 不学 savehar 用 repr 长度 |
| `request.bodySize` | `len(raw_content or b"")`；缺失 `-1` | |
| `request.postData` | 仅 `POST/PUT/PATCH` 且 text 模式 | 默认只给 `{mimeType, _size, _sha256}` |
| `response.status/statusText/httpVersion` | 原值；无响应 `0` | |
| `response.headers/cookies` | 同请求侧规则 | `Set-Cookie` 整值打码 |
| `response.content` | `{size, compression, mimeType}` + 可选 `text/encoding` | `size` = **解压后**字节数（修正 savehar 口径） |
| `response.redirectURL` | `Location` 经 URL 脱敏 | 与 3xx 条目成对 |
| `response.headersSize` | `-1`（不伪造） | |
| `response.bodySize` | 原始字节长度；缺失 `-1`；304 为 `0` | |
| `response._error` | 仅错误条目，`flow.error.msg` | 扩展字段，明确非标准；此时 `content={size:0,mimeType:""}` |
| `timings` | `connect/ssl/send/wait/receive`，不可得 `-1` | 与 savehar 同口径 |
| `connection` | `flow.client_conn.id` 前 8 位 | HAR 1.2 可选字段，用于同连接分组 |
| `serverIPAddress` | `flow.server_conn.peername[0]` | 可选 |
| `_resourceType` / `_webSocketMessages` | 仅 WebSocket | Chrome 扩展字段 |
| `_mobilePerf` | `{runId, tier, redacted, bodyMode, truncated, seq}` | 自定义溯源块，放在 entry 上 |


`log._mobilePerf` 再放一份全局信息：

```json
{"runId":"run-...","tier":"decrypt","trust":"trusted",
 "redaction":"headers+query+cookies","bodyMode":"meta",
 "limits":{"maxBodyBytes":65536,"maxEntries":5000,"maxBytes":67108864},
 "truncated":false,"droppedEntries":0,"skippedEntries":0}
```

`creator` 用 `{"name":"mobile_perf","version":<importlib.metadata 版本，失败则 "unknown">}`，并在 `comment` 写明「redacted / bodyMode」。

> 说明：`_error`、`_resourceType`、`_webSocketMessages`、`_mobilePerf` 都是扩展字段；Chrome DevTools 会忽略未知字段。正式字段严格按 HAR 1.2。

### 3.5 脱敏策略（写入时执行，sidecar 也是脱敏后的）

**原则：先脱敏再落盘；任何时候都不写原文。** 合成阶段只做结构拼接，不再接触原始值。

| 对象 | 默认处理 | 细节 |
| --- | --- | --- |
| 请求/响应头 | 敏感头整值替换为 `[REDACTED]` | 精确名单：`Authorization`、`Proxy-Authorization`、`Cookie`、`Set-Cookie`、`X-Api-Key`、`Api-Key`、`X-Auth-Token`、`X-Access-Token`、`X-Refresh-Token`、`X-Csrf-Token`、`X-Xsrf-Token`、`X-Session-Id`、`Session`、`Token`、`Signature`、`X-Signature`；再用正则 `(?i)(token|secret|auth|cookie|session|password|passwd|pwd|api[_-]?key|credential|signature)` 兜底 |
| Cookie / Set-Cookie | 保留 cookie 名，值全部 `[REDACTED]` | 例：`sid=[REDACTED]; theme=dark`；`cookies` 数组同步 |
| URL 查询串 | 敏感参数值 `[REDACTED]`，key 与顺序保留 | 名单：`token/access_token/refresh_token/id_token/auth/authorization/session/sessionid/sid/jsessionid/phpsessid/password/passwd/pwd/secret/api_key/apikey/key/client_secret/sign/signature/sig/code/ticket/nonce/state`；支持 `--har-redact-key` 追加 |
| URL userinfo | `https://user:pass@host/` → `https://[REDACTED]@host/` | 避免 Basic 凭据泄漏 |
| `Location` / `Referer` / `Origin` | 当 URL 再跑一遍查询串脱敏 | 防止重定向把 token 带进 HAR |
| 请求/响应 body | **默认只记 `size` + `sha256` + `mimeType`，不记正文** | `body=text` 才写正文；`body=none` 连摘要都不算 |
| `postData` | 默认 `{mimeType, _size, _sha256}` | text 模式下 `application/x-www-form-urlencoded` 的敏感参数同样打码；`multipart/form-data` 不解析正文 |
| 响应 `content.size` | 记录真实解压后大小 | 即使不记正文也保留，供性能分析 |
| 大 body 摘要 | 超过 `max_hash_mb` 不算 sha256 | 置 `_sha256: null, _hashSkipped: true`，避免大文件 CPU 抖动 |
| 头值长度 | 单值 > 2 KB 截断 | 置 `_truncated: true`，防止畸形头撑爆文件 |

**残余风险（必须在报告/文档写清）：**

- 自定义鉴权头（如 `X-Tenant-Token`）不在名单里时可能漏脱敏 → 用 `--har-redact-key` 补；默认正则覆盖常见命名。
- 路径段里的 token（如 `/api/v1/<jwt>`）默认不脱敏：误伤面大、且无法可靠识别。默认策略是「路径保留」，在文档标注为已知边界；如需更严，P2 可加启发式（长度 > 32 的 base64url 段打码）。
- `body=text` 时正文可能含 PII/业务数据；该模式必须显式开启，报告显著标注。
- sha256 对低熵秘密（如固定 token）可能被字典反推；因此**只对 body 算摘要**，不对头/查询串算。

### 3.6 TLS / not_tested 语义（不得伪造）

| tier | trust | 代理侧证据 | HAR 行为 | `network.har.status` |
| --- | --- | --- | --- | --- |
| passthrough | 不适用 | 任意 | 不启动 HAR 写入；即使误开也不产生 HTTPS 条目 | `not_tested`，原因「直通档不解密 TLS」 |
| decrypt | `pending` / `unknown` | 无握手结论 | 不生成 `app.har` | `not_tested`，原因「未观察到客户端 TLS 握手」 |
| decrypt | `rejected` | 证书类失败 | 不生成 HTTPS 条目；若确有明文 HTTP 条目则保留并标注 | `not_tested`（无条目）/ `partial`（仅明文），`httpsCoverage="none"` |
| decrypt | `trusted` | 观察到请求 | 正常写 | `complete` / `partial`（截断时） |
| decrypt | `trusted` | 无请求 | 不生成 `app.har` | `not_tested`，原因「未观察到 HTTP 请求」 |
| 任意 | 任意 | mitmproxy 不可用 | 不生成 | `not_tested`，附安装提示 |

关键点：

- **明文 HTTP 不需要 CA 信任**，因此「decrypt + rejected」时明文条目是真实数据，可以保留；但必须在 `network.har.httpsCoverage="none"` 与 `notTestedReason` 里说清「HTTPS 部分未测」，不能让人以为抓全了。
- `rejected` 时**不创建空 `app.har`**：空文件会被 DevTools 当成「没有请求」，是误导。
- 复用 `NetSession.api_not_tested_reason` 的文案逻辑，避免两套解释。
- 不把 HAR 摘要写进 `metricsSummary`：HAR 是证据/附件，不是性能指标；用 `network.har` + `har_capture` 事件表达。
- **关闭时也要有结论**：`network.har = {enabled: false, status: "not_enabled"}`，报告能区分「没开」与「开了但没抓到」，避免读者把空白当遗漏。`status` 取值固定为 `not_enabled | not_tested | complete | partial | failed`。

### 3.7 退出、写入中断、截断与大小边界

| 场景 | 行为 | 报告如何体现 |
| --- | --- | --- |
| 正常 `stop()` | 先 `terminate()`，再合成 HAR | `status=complete`（无截断） |
| 进程被 kill（Windows terminate） | 已 flush 的 JSONL 行仍在；合成照常 | `writerClosed=false`，但 `status` 仍可为 `complete`；不把「没跑 done」误报成丢数据 |
| 采集期间磁盘写失败 | addon 捕获异常，向 `net_proxy.jsonl` 写 `har_error`；不影响转发 | `status=partial`、`writerErrors>0` |
| JSONL 有坏行 | 合成跳过，计数 | `skippedEntries>0`、`status=partial` |
| 超过 `maxEntries` | 停止追加，后续条目丢弃 | `truncated=true`、`truncatedReason="max_entries"`、`droppedEntries=N` |
| 超过 `maxBytes` | 同上 | `truncatedReason="max_bytes"` |
| 单条正文超 `maxBodyBytes` | 写前缀，标 `_truncated`、`_originalSize` | 条目级标记；全局 `truncated=true` |
| 合成中途异常 | 保留 `.tmp`，不 `os.replace` | `status=failed`、`error=...`；不产生半个 `app.har` |
| 主机崩溃（没走 `stop()`） | `app.har` 不存在，JSONL 仍在 | 正常 `report` 无法重建 meta（无 finalize），属已知边界；P1 可选 `mobile-perf report --rebuild-har` |
| 大响应/视频下载 | meta 模式只存大小+摘要；text 模式截断 | 不会 OOM（不持有 entry 列表） |
| 运行保留清理 | HAR 随 run 目录被 `enforce_retention` 清理 | 与现有行为一致 |

并发说明：mitmproxy 在单个事件循环里串行调用同步 addon 钩子，`HarWriter` 仍是单线程写；即便如此，`HarWriter` 内部用一把 `threading.Lock` 保护 `stats`，为将来切 `asyncio.to_thread` 留余地（成本一行）。【推断：12.x 钩子串行】

### 3.8 特殊协议与请求形态边界

| 形态 | 处理 | 说明 |
| --- | --- | --- |
| CONNECT | **不写 HAR 条目** | 只在 `net_proxy.jsonl` 记 `http_connect`；避免像 savehar 那样伪造 `https://host:443/`。隧道数量看 `network.proxy.connectRequests` |
| HTTP/1.1 | 正常 | 标准 entry |
| HTTP/2 | 正常 | `httpVersion` 用 mitmproxy 的 `HTTP/2.0`；不写伪头（`:authority` 已被 mitmproxy 还原为 host） |
| HTTP/3 / QUIC | **抓不到** | Android 的 QUIC 走 UDP 443，绕过 `http_proxy`；报告 `network.note` 提示「QUIC 流量不在代理覆盖内」，不能据此断言无请求 |
| WebSocket | 仅 `websocket_end` 时写一条 | `_resourceType="websocket"`、`_webSocketMessages` 顺序保留；二进制 base64；单 socket 最多 200 条消息 / 1 MB，超出标 `_truncated`。**长连接在采集结束仍未关闭 → 不写条目**，用 `network.har.websocketOpen` 计数说明 |
| 二进制 body | meta 模式只记大小/摘要；text 模式 base64 + `encoding="base64"` | 判断用 `mitmproxy.utils.strutils.is_mostly_bin`，与 savehar 一致 |
| 重定向 | 每一跳一条 entry | mitmproxy 不自动跟随；3xx 条目 `redirectURL` 已脱敏 |
| 错误请求 | `error()` 钩子写一条 | `response.status=0`、`_error`、`wait/receive=-1`、`bodySize=-1`；与 savehar 的错误约定一致，但明确标注扩展字段 |
| 304 / HEAD | `bodySize=0` | 不伪造正文 |
| 流式响应 | `raw_content is None` | 不写正文，标 `_bodyUnavailable="streamed"`；本项目 addon 未设置 `stream`，默认是缓冲的（`http.py:247-262` 明写「在 response 钩子里设置 stream 已经太晚，body 已缓冲」） |
| gzip/brotli 解码失败 | 捕获 `ValueError` | 只记原始大小，标 `_decodeError`，不让单条坏响应打断整个 HAR |
| 非 HTTP TCP/UDP | 不写 | 与连接级指标分开，避免口径混淆 |

### 3.9 报告入口

`report.json` 新增（挂在现有 `network` 段下，随 `source: "mitmproxy"` 一起透传）：

```json
{
  "network": {
    "har": {
      "enabled": true,
      "status": "complete",
      "path": "network/app.har",
      "entriesPath": "network/app.har.entries.jsonl",
      "entries": 128,
      "sizeBytes": 91234,
      "bodyMode": "meta",
      "redacted": true,
      "truncated": false,
      "truncatedReason": null,
      "droppedEntries": 0,
      "skippedEntries": 0,
      "writerErrors": 0,
      "writerClosed": false,
      "websocketOpen": 0,
      "httpsCoverage": "decrypted",
      "notTestedReason": null
    }
  }
}
```

`report.html` 的 `_network_markup`（`report.py:897`）在现有表格里加 5 行：

- HAR / HAR：`已生成（128 条，89 KB）` 或 `未生成：<notTestedReason>`
- 正文口径 / Body：`仅大小+摘要` / `正文（上限 64 KB）`
- 脱敏 / Redaction：`开（头+查询串+Cookie）` / `关（危险）`
- 截断 / Truncated：`否` / `是（max_entries，丢弃 12 条）`
- 文件 / File：`<a href="network/app.har">network/app.har</a>`（相对路径，单文件报告本地打开可点）

约束：**报告只放路径与统计，不内嵌 HAR 内容**，保持「报告不含流量内容」的既有约定，也避免 `report.json` 被秘密撑大。

`httpsCoverage` 取值固定为 `decrypted | none | partial`；`path` / `entriesPath` 使用相对 run 目录的路径，便于 run 目录整体搬迁后报告仍可点击。

---

## 4. 最小改动路径（可直接转开发任务书）

### 4.1 改动清单

| # | 文件 | 改动 | 预估 |
| --- | --- | --- | --- |
| 1 | **新增** `src/mobile_perf/har_writer.py` | 纯标准库：`HarOptions`、`HarLimits`、`scrub_headers/scrub_url/scrub_cookies`、`build_entry(...)`、`HarWriter.append`、`compose_har(entries, out, meta)`、`HarSummary` | ~260 行 |
| 2 | `src/mobile_perf/netproxy_addon.py` | 顶部按路径加载 `har_writer`（同目录）；`response/error/websocket_end` 各加 10~15 行；`har_error` 事件；不改现有 `api_request` 语义 | +60 行 |
| 3 | `src/mobile_perf/netprof.py` | `HAR_FILENAME/HAR_ENTRIES_FILENAME` 常量；`HarOptions` 透传；`_spawn_proxy` 注入 `MOBILE_PERF_HAR`；`stop()` 调 `compose_har` 并填 `NetSession.har`；mitmproxy 缺失分支在 `har.enabled` 时不走「免代理」捷径 | +90 行 |
| 4 | `src/mobile_perf/kit.py` | `collect` / `run_collect` / `CollectScope` 增加 `har` 参数；`_start_net_profile` 透传；`net_meta["har"]`；`har_capture` 事件 | +30 行 |
| 5 | `src/mobile_perf/cli.py` | `--har*` 参数；`_parse_har_options` 早失败（passthrough 冲突、负数、档位缺省 none） | +45 行 |
| 6 | `src/mobile_perf/report.py` | `_network_markup` 增加 HAR 行；`build_report` 无需改（`network` 整体透传） | +12 行 |
| 7 | `tests/unit/test_har_writer.py` | 纯函数/限额/合成/坏行/脱敏单测 | ~18 例 |
| 8 | `tests/unit/test_netprof_har.py` | `NetProfiler.stop` 合成、trust 拒绝不产文件、mitmproxy 缺失降级、`har` 透传 | ~8 例 |
| 9 | `README.md` | 「致谢与许可」加 mitmproxy（MIT，可选依赖，未 vendor） | +1 行 |

**总计约 500 行实现 + 26 个测试**，1 人 2~3 天可完成，另加 0.5 天真机验证。

### 4.2 关键实现骨架

`har_writer.py`（纯函数，可测）：

```python
REDACTED = "[REDACTED]"

def scrub_url(url: str) -> tuple[str, bool]:
    parts = urlsplit(url)
    query = [(k, REDACTED if _sensitive(k) else v)
             for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    netloc = parts.netloc
    if "@" in netloc:
        netloc = "[REDACTED]@" + netloc.rsplit("@", 1)[1]
    scrubbed = urlunsplit((parts.scheme, netloc, parts.path,
                           urlencode(query, doseq=True), parts.fragment))
    return scrubbed, scrubbed != url

class HarWriter:
    def append(self, entry: dict) -> bool:
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        if self._would_exceed(len(line)):      # maxBytes / maxEntries
            self.stats.mark_truncated(...); return False
        self._fh.write(line + "\n"); self._fh.flush()
        self.stats.entries += 1; return True
```

`netproxy_addon.py`：

```python
_HAR_CFG = json.loads(os.environ.get("MOBILE_PERF_HAR") or "{}")
_HAR = _load_har_module(__file__)       # 同目录 import，失败则 _HAR=None
_WRITER = None
if _HAR and _HAR_CFG.get("entriesPath"):
    _WRITER = _HAR.HarWriter(...)

def _har_safe(fn):
    try:
        fn()
    except Exception as exc:            # 绝不打断代理/采集
        _emit("har_error", error=str(exc))

def response(flow):
    ...  # 现有 api_request 逻辑不变
    if _WRITER is not None:
        _har_safe(lambda: _WRITER.append(_HAR.build_entry(flow, "response")))
```

`netprof.NetProfiler.stop()`：

```python
self._session.restored = {...}          # 现有
if self._har.enabled:
    work = Path(self._log_path).parent if self._log_path else None
    self._session.har = compose_har(
        entries_path=work / "app.har.entries.jsonl",
        har_path=work / "app.har",
        meta={"runId": ..., "limits": ..., "redacted": ...},
    )
self._session.log_summary = read_proxy_log(self._log_path)
```

`compose_har` 的实现要点：

1. 读 `entries.jsonl` 逐行 `json.loads`；坏行 `skipped++` 继续。
2. 写 `app.har.tmp`：先写 `{"log":{...,"entries":[`，条目之间写 `,`，最后 `]}}`。
3. `os.replace(tmp, app.har)`（同目录原子替换）。
4. 返回 `HarSummary(status, entries, sizeBytes, truncated, ...)`。

### 4.3 为什么把纯逻辑放新模块而不是塞进 addon

`netproxy_addon.py` 顶部 `import mitmproxy`，主 venv（Python 3.10，无 mitmproxy）无法 import，现有单测也不能直接测它。把脱敏/构造/限额/合成放到 `har_writer.py` 后：

- 主 venv 直接 `from mobile_perf import har_writer` 跑单测（符合现有 `tests/unit` 不碰设备、不装可选依赖的约定）。
- addon 侧只做「取 flow 字段 → 调纯函数」，胶水代码薄，回归风险低。
- addon 通过 `sys.path.insert(0, os.path.dirname(__file__)); import har_writer` 加载；不依赖包安装，符合「mitmproxy 独立解释器」的边界。

---

## 5. 方案 C：其它开源/商业方案的适配评估

| 方案 | 许可证 | 形态与链路 | 与现有 `adb reverse + http_proxy` 的契合度 | 结论 |
| --- | --- | --- | --- | --- |
| **mitmproxy `savehar`** | MIT【文档】 | 代理侧；`--set hardump` | 100%（就是同一进程） | 仅调试可用，见 §2 |
| **HTTP Toolkit** | AGPL-3.0【文档】 | 桌面 Electron/Node 应用；Android 通过 ADB 注入系统 CA（官方文档：模拟器/已 root 设备），非 root 需 Frida | 部分契合：都用 ADB，但它自带 agent/端口/证书管理，需整体接管；无法嵌入 Python 组件 | 放弃（AGPL 传染 + 集成重） |
| **PCAPdroid** | GPL-3.0【文档】 | 端侧 `VpnService`，无需 root；本地解析，支持导出 HAR；TLS 解密集成 mitmproxy 作为本地代理 | 不契合：走 VPN 抓全设备流量，绕开 `adb reverse + 系统代理`；自动化需其 App API/UI；GPL-3.0 | 放弃（链路冲突 + copyleft），可作人工交叉验证 |
| **PacketCapture（Grey Shirts）** | 闭源（未能核实） | 端侧 VPN，导出 pcap | 不契合；非开源、非 HAR | 放弃 |
| **Reqable** | 闭源商业（未能核实） | 桌面/移动抓包 IDE | 不契合；无法作为组件内嵌 | 放弃 |

补充说明：

- HTTP Toolkit 的 README 明确写「AGPL for HTTP Toolkit-specific components + Apache-2/MIT for reusable libraries」，desktop 仓库 `LICENSE` 是 AGPL-3.0 全文。若只是开发者本机手工使用不构成分发，但**把它打包进本组件或随产品分发**会触发 AGPL 义务，成本远高于自建 500 行。
- PCAPdroid 的 README 明确「simulates a VPN ... without root」「Decrypt the HTTPS/TLS traffic」「export them to HAR」，并致谢 mitmproxy 作为 TLS 解密代理；其 HAR 仍受「App 是否信任用户 CA」限制，和本组件遇到的是同一个问题。
- `github.com` / `raw.githubusercontent.com` / `softwareishard.com` / `f-droid.org` / Play Store 在本机直连超时（见 §9.3 网络说明），上述仓库文件改由 jsDelivr 读取 GitHub 原文；许可证结论以文件正文为准。

---

## 6. 验收标准（14 条，均可自动化或真机复验）

> 约定：`RID` 为 run 目录；所有断言都能写成 pytest（unit/device）或一条 shell 检查。

| # | 类型 | 场景 | 断言 |
| --- | --- | --- | --- |
| A1 | unit | 默认关闭 | 不传 `har` 时 `network.har.enabled is False`、`status=="not_enabled"`；`RID/network/app.har` 不存在；`report.json` 无 `network.har.path` |
| A2 | unit | `--har` + `passthrough` | CLI 退出码非 0，stderr 含「HAR 需要 --net-tier decrypt」；不创建 run 目录 |
| A3 | unit | HAR 结构合法 | `json.load(app.har)` 成功；`log.version=="1.2"`；每条 entry 有 `startedDateTime/time/request/response/timings`；`time == pytest.approx(sum(v for v in timings.values() if v >= 0))`；无 `app.har.tmp` 残留 |
| A4 | unit | 脱敏（头/Cookie/查询串） | 构造含 `Authorization: Bearer SECRET`、`Cookie: sid=SECRET`、`Set-Cookie: token=SECRET`、`?token=SECRET&a=1` 的 flow → `app.har` 与 `app.har.entries.jsonl` 全文不含 `SECRET`；`queryString` 仍含 `a=1`；头名保留 |
| A5 | unit | 默认 body 只记 meta | 1 MB JSON 响应 → `content.text` 不存在；`content.size==1048576`；`_sha256` 为 64 位 hex；HAR 文件 < 20 KB |
| A6 | unit | 正文截断 | `body="text", max_body_kb=8` → `len(content.text) <= 8192`；`_truncated is True`；`_originalSize==1048576` |
| A7 | unit | 条目/字节上限 | `max_entries=2` 跑 3 条 → `entries==2`、`status=="partial"`、`truncated is True`、`truncatedReason=="max_entries"`、`droppedEntries==1`；`app.har` 仍可 `json.load` |
| A8 | unit | 中断恢复 | 往 `entries.jsonl` 末尾追加半行坏 JSON → 合成成功、`skippedEntries==1`、`status=="partial"`、`app.har` 可解析 |
| A9 | unit | 错误请求 | 连接失败 flow → entry `response.status==0`、`_error` 非空、`timings.wait==-1`、`response.bodySize==-1` |
| A10 | unit | 证书拒绝不产文件 | 假 `net_proxy.jsonl` 只有 `trust_probe ok=false`、无 `api_request` → `stop()` 后 `app.har` 不存在；`network.har.status=="not_tested"`；原因含「证书」；`api_*` 仍 `not_tested` |
| A11 | device | 明文端到端（无需 CA） | 复用 `.scratch/app-capture/verification/probe_plain_http.py` 的形态：本机回环 HTTP 服务 + mitmdump + `adb reverse` + 系统代理；浏览器访问后 `app.har` 至少 1 条 entry，`request.url` 为回环地址，`Set-Cookie` 已打码（浏览器可能额外请求 favicon，断言 `>= 1`，不断言恰好 1） |
| A12 | device | 解密端到端（需信任用户 CA） | 模拟器（可写 system）或 debug 包 + `networkSecurityConfig`：访问 `https://nghttp2.org/httpbin/redirect/2` → 生成 2 条 entry（3xx + 200），首条 `redirectURL` 指向第二跳；`httpVersion` 为 `HTTP/2.0` 或 `HTTP/1.1`；DevTools 可导入无报错 |
| A13 | device | 截断/长连接 | 持续 WebSocket 长连接在采集结束时未关闭 → 无该 socket 的 HAR 条目；`network.har.websocketOpen >= 1`，且不出现半条/坏 JSON |
| A14 | 安全检查 | 全 run 目录无泄漏 | `grep -R "PROBE_SECRET\|Bearer \|session=" RID`（排除预期字段名）无命中；`report.json` / `report.html` 不含任何 HAR 正文 |

（A1~A10 进 `tests/unit`，A11~A13 进 `tests/device` 标记，A14 作为 CI 脚本或 device 断言。）

---

## 7. 风险与未决问题

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| mitmproxy 版本漂移（addon 钩子/字段改名） | HAR 写入失效 | 只在 `12.3.x` 验证；addon 内 `try/except` + `har_error` 事件；单测不依赖 mitmproxy |
| WebSocket 长连接未关闭就停止 | 缺条目 | 明确标注 `websocketOpen`，不伪造；必要时 P2 加 `websocket_start` 计数 |
| QUIC/HTTP3、自定义 TCP 协议 | 抓不到 | 报告 `network.note` 标注覆盖边界；不把「没抓到」说成「没请求」 |
| 证书固定（pinning） | TLS 失败 | 复用现有 `trust_probe` 判定；HAR `not_tested`，不重试 |
| `body=text` 泄漏业务数据 | 合规风险 | 默认 meta；text 需显式；报告显著标注 |
| 自定义敏感头漏脱敏 | 秘密入盘 | 默认正则 + `--har-redact-key`；文档写明残余风险 |
| 合成阶段主机崩溃 | 无 `app.har` | JSONL 仍在；P1 加 `report --rebuild-har`（不在首版范围） |
| HAR 体积影响保留策略 | 磁盘增长 | 默认 `max_mb=64`；随 run 目录清理；报告展示 `sizeBytes` |
| `headersSize` 不精确 | 与浏览器口径有差 | 直接写 `-1`（规范允许），不伪造 |

未决问题（建议开发前定稿）：

1. `--har` 是否允许与 `--net-profile` 同时使用？建议允许（弱网 + 抓包是合理组合），只是 `--net-tier` 必须 decrypt。
2. 是否需要在 `report` 命令里重建 HAR？建议 P1，不阻塞首版。
3. 是否给 HAR 单独做「下载」入口（HTML `<a download>`）？建议 `<a href>` 即可，避免单文件报告内嵌大文件。

---

## 8. 许可证与归因

| 组件 | 许可证 | 使用方式 | 义务/结论 |
| --- | --- | --- | --- |
| mitmproxy 12.3.0 | MIT（PyPI 元数据与本地 `METADATA` 均为 MIT 全文）【文档/本机】 | **可选运行时依赖**，独立 venv + subprocess 调用；不 vendor、不修改其源码 | 分发本组件不引入 MIT 之外的义务；若未来复制 `savehar.py` 片段，需保留版权与许可声明。本方案按 HAR 1.2 规范自行实现，不复制其代码 |
| Apache ECharts 5.5.1 | Apache-2.0（已 vendor 至 `src/mobile_perf/assets/echarts.min.js`） | 现有报告图表 | 保持现有 `README` 致谢；HAR 小节用纯 HTML 表格，不新增 JS 库 |
| HTTP Toolkit | AGPL-3.0（desktop `LICENSE` 全文；README 说明 AGPL + Apache/MIT 混合）【文档】 | **不引入** | 若打包/分发会触发 AGPL；仅作为开发者本机手工工具提及 |
| PCAPdroid | GPL-3.0（仓库 `COPYING`）【文档】 | **不引入** | copyleft；且链路冲突，仅作人工交叉验证 |
| HAR 1.2 | W3C Web Performance WG 草案（2012-08-14），非正式标准 | 输出格式 | 明确「按 1.2 草案实现」，Chrome DevTools 兼容；扩展字段加 `_` 前缀 |

README 建议新增一行：

> APP 抓包（可选）使用 [mitmproxy](https://mitmproxy.org/)（MIT）；本组件不打包其源码，仅在独立 Python ≥3.12 环境中以子进程方式调用。

---

## 9. 证据来源

### 9.1 本机文件（均已读取，行号为报告撰写时的文件行号）

- `D:/workspace/claudecode-test/moblie-xn/.scratch/venv-mitm/Lib/site-packages/mitmproxy/addons/savehar.py`
  - L32 / L116-120（flows 内存列表）、L122-128（`done()` 才写）、L104-114（保存钩子）、L78-88（`hardump` 选项）、L65-76（HAR 1.2 头）、L180-213（响应体/base64）、L214-229（错误响应）、L231-234（CONNECT URL）、L241-264（头/Cookie/查询串/postData）、L269-285（WebSocket）、L202/L248（headersSize=repr 长度）
- `.../mitmproxy/addons/__init__.py` L23 / L62-63（`savehar.SaveHar()` 默认加载）
- `.../mitmproxy/addons/save.py`（`save_stream_filter` 语义、流式 writer 对比）
- `.../mitmproxy/http.py` L247-262（`stream` 必须在 `responseheaders` 设置；`response` 时 body 已缓冲）、L319-322（`raw_content` 可为 None）
- `.../mitmproxy/tools/main.py` L126-133（SIGINT/SIGTERM → `master.shutdown`）
- `.../mitmproxy/master.py` L88-94（`finally: await self.done()` → `DoneHook`）
- `.../mitmproxy/utils/strutils.py` L128+（`is_mostly_bin`）
- `.../mitmproxy-12.3.0.dist-info/METADATA`（MIT 许可全文；`Classifier: License :: OSI Approved :: MIT License`）
- `D:/workspace/claudecode-test/moblie-xn/src/mobile_perf/netprof.py`：L95-104（证据/信任三态）、L351-431（日志解析）、L645-797（生命周期/stop）、L808-839（spawn）、L841-854（terminate）、L870-879（reverse/系统代理）
- `D:/workspace/claudecode-test/moblie-xn/src/mobile_perf/netproxy_addon.py`：L34-52（参数环境变量）、L71-80（`_emit` 追加写）、L95-108（tier 决定 TCPLayer）、L131-159（信任探测）、L169-187（`response` / `api_request`）、L220-227（`http_connect`）、L276-288（`request` / `error`）
- `D:/workspace/claudecode-test/moblie-xn/src/mobile_perf/kit.py`：L500-524（网络/接口聚合）、L585-598（`api_*` not_tested）、L630/655-656（network/api 入报告）、L1293-1317（起代理）、L1560-1619（`run_collect`）
- `D:/workspace/claudecode-test/moblie-xn/src/mobile_perf/cli.py`：L90-120（`--net-*`）、L246-309（collect）、L312-335（`_parse_net_profile`）
- `D:/workspace/claudecode-test/moblie-xn/src/mobile_perf/report.py`：L141-215（`build_report`，L203-206「只放已脱敏结论」）、L897-940（`_network_markup`）、L19（`SCHEMA_VERSION=1`）
- `D:/workspace/claudecode-test/moblie-xn/src/mobile_perf/collector.py`：L45-56（run 目录/保留）
- `D:/workspace/claudecode-test/moblie-xn/runs/m6-net-e2e-4/network/net_proxy.jsonl`（真实代理日志样本，315 行）
- 本机实验：Windows 11 / Python 3.12，`subprocess.Popen.terminate()` 后子进程 `atexit` 未执行（临时脚本已清理，未留仓库文件）
- 本机 `mitmdump --version`：`Mitmproxy: 12.3.0 / Python: 3.12.10 / Windows-11-10.0.26300-SP0`

### 9.2 公开来源（查看日期均为 2026-11-07）

- mitmproxy PyPI 元数据（版本、许可证、发布时间）：<https://pypi.org/pypi/mitmproxy/json>
  - 12.3.0 wheel 上传 `2026-10-22T02:28:10Z`；最新 12.3.1 上传 `2026-10-25T23:32:31Z`；`info.license` 为 MIT 全文
- HAR 1.2 规范（W3C Web Performance WG 草案镜像）：<https://w3c.github.io/web-performance/specs/HAR/Overview.html>
  - `timings` 不可得填 `-1`；`time` 为所有非 -1 timings 之和；`content.size` 为解压后长度；`headersSize` 不可得可填 `-1`；`entries`/`request`/`response` 必填
- HTTP Toolkit 桌面端许可证（AGPL-3.0 全文，经 jsDelivr 读取 GitHub 原文）：
  <https://cdn.jsdelivr.net/gh/httptoolkit/httptoolkit-desktop@master/LICENSE>
- HTTP Toolkit README（开源构成与许可证说明，经 jsDelivr）：
  <https://cdn.jsdelivr.net/gh/httptoolkit/httptoolkit@master/README.md>
  - 原文：「licensed as a mixture of copyleft AGPL ... and permissive Apache-2/MIT」
- HTTP Toolkit Android 文档（ADB 注入系统 CA；模拟器/已 root；非 root 用 Frida）：
  <https://httptoolkit.tech/docs/guides/android/>
- PCAPdroid `COPYING`（GPL-3.0 全文，经 jsDelivr）：
  <https://cdn.jsdelivr.net/gh/emanuele-f/PCAPdroid@master/COPYING>
- PCAPdroid README（VPN/免 root、HAR 导出、TLS 解密依赖 mitmproxy，经 jsDelivr）：
  <https://cdn.jsdelivr.net/gh/emanuele-f/PCAPdroid@master/README.md>

### 9.3 本机网络限制说明

`github.com`、`raw.githubusercontent.com`、`softwareishard.com`、`f-droid.org`、Play Store 在本机 `curl` 直连超时/被拒绝；`pypi.org`、`w3c.github.io`、`docs.mitmproxy.org`、`httptoolkit.tech`、`cdn.jsdelivr.net` 可访问。凡是无法直连的仓库文件，均通过 jsDelivr 读取 GitHub 原文，并在对应条目注明；未能核实的内容（PacketCapture/Reqable 的具体许可证与功能）已在 §5 标注「未能核实」，未作为结论依据。
