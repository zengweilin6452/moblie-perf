VERDICT: PASS

复审对象：第 1 轮审查报告 `review-round-1.md` 的两条阻断缺陷 + 编排方修复说明所列 5 项改动。
方式：只读源码走查 + 在本机真实 mitmproxy/Python 3.12 环境独立驱动 addon 与 `NetProfiler`（临时目录，未落仓库）
+ 独立重跑单测与 ruff。未修改/删除主项目任何文件，本轮唯一新建文件即本 `review-final.md`。

---

## 一、两条阻断缺陷：均已修复（独立证据）

### 问题 1（阻断）：addon 动态加载 har_writer 必然失败 → 已修复且实测生效

- 修复位置：`src/mobile_perf/netproxy_addon.py:104-111` —— `sys.modules[spec.name] = module` 在
  `spec.loader.exec_module(module)` **之前**执行，`except Exception:` 分支 `sys.modules.pop(spec.name, None)`
  后重新抛出（半初始化模块不留残留）。模块/函数 docstring 也写明了 3.12 dataclasses 回查 `sys.modules` 这条成因。
- 独立验证 A（真实解释器 + 真实 mitmproxy，非桩）：本机 `python 3.12.10`，`mitmproxy` 可导入。按文件 exec
  `netproxy_addon.py`（`MOBILE_PERF_HAR` 单行 JSON 指向临时 `entriesPath`），结果：
  `HAR_WRITER_INIT: True`，`sys.modules_has_writer: True`；第 1 轮报的那条
  `AttributeError: 'NoneType' object has no attribute '__dict__'` 不再出现。
- 独立验证 B（真子进程端到端，最强证据）：`NetProfiler(mitmdump=...Python312\Scripts\mitmdump.exe,
  tier=decrypt, capture=CaptureOptions(enabled=True))` + 本地明文 HTTP 服务，经代理发一条带
  `?token=SECRET`/`Authorization: Bearer TOK`/`Cookie: sid=A`/`Set-Cookie: sid=COOKIE_SECRET` 的绝对形式请求，
  `stop()` 后实测：
  - `har.status="complete"`、`har.entries=1`、`app.har` 存在、**无 `.tmp` 残留**（terminate 后已 flush 的行未丢）；
  - `log.version="1.2"`、`creator.name="mobile_perf"`、`pages=[]`；
  - `url="http://127.0.0.1:12998/probe?token=[REDACTED]&page=2"`、`queryString` 同步脱敏且保留 `page=2`；
  - `Authorization` 头值 `[REDACTED]`，`cookies`/`set-cookie` 均 `[REDACTED]`；
  - `content={'size': 11, 'mimeType': 'application/json', 'sha256': '4062…'}`（默认不写正文）、
    `headersSize=-1`、`bodySize=11`、`time=4.509 == sum(timings)`；
  - 产物全文对 `SECRET`/`Bearer TOK`/`COOKIE_SECRET`/`sid=A` 四项泄漏检测全为 False；
  - `net_proxy.jsonl` 事件序列仍为唯一证据（`params(harEnabled=true)`→`http_request`→`api_request`…）。
- 附带影响（"把写入链路没起来说成 App 没发请求"）也已修复：`netproxy_addon.py:155-157` 的 init 失败以
  `har_error: "init: …"` 落日志，`netprof.py:943-951` 见到 `initErrors` 即置
  `status=failed`、`path=None`、原因写「HAR 写入链路初始化失败：<原文>」，并 return（不再走零流量文案）。
  单测 `tests/unit/test_netprof_har.py:280-300` 正向锁死（含 `"可能走缓存" not in` 反向断言）。
  我另用两条错误配置实测 addon 侧：缺 `entriesPath` → `init: HAR 配置缺少 entriesPath`、writer 为 None；
  `MOBILE_PERF_HAR_ENTRIES` 与 `entriesPath` 不一致 → `init: … 不一致`、writer 为 None。均不阻断转发。

### 问题 2（阻断）：真机用例用设备 shell 调 `reverse` → 已修复

- 修复位置：`tests/device/test_netprof_device.py:219-223`（建立）与 `:260-263`（清理）。
  两处均改为**宿主侧** `caller(["-s", DEVICE_SERIAL, "reverse", …], 15.0)`，并
  `assert int(getattr(result, "returncode", 1)) == 0`，失败即报"reverse 未建立"而不是让 HAR 断言背锅；
  清理用 `reverse --remove tcp:<port>` 且同样断言返回码。
- 命令形态与生产代码一致：`netprof.py:1051`/`netprof.py:1108` 正是 `["-s", serial, "reverse", …]`，
  而 `_shell()`（`:55-56`）会插入 `shell` —— 旧写法确实不可行，修法正确。
- 用例结构复核：`:226` 明文分支断言 `har.status in {"complete","partial"}`、`entries>=1`、`redacted is True`、
  `secret not in text`、`"token=[REDACTED]" in text`、`https_coverage == "none"`、末尾 `doctor` 断言
  `proxyClean`/`reverseClean`；`:287` 证书拒绝分支只允许 `not_tested` 或仅明文 `partial`，且
  `status == "not_tested"` 时 `har.path is None`。双 skipif 门控（`:43-47`）与旧 5 个用例完好未改。
- 说明：按任务书边界未跑 `-m device`（无设备），上述为静态复核 + 与 `netprof.py` 生产命令一致性核对；
  编排方修复说明称真机双路径 `passed=true`，与此不矛盾。

---

## 二、非阻断加固：两条有效，一条有效但带来副作用，三条未处理

1. **URL 型头脱敏（有效）**：`har_writer.py:214` 新增 `_URL_HEADERS = {location, content-location, referer, refresh}`，
   `:217-229` 在 `redact` 下对这些头走 `scrub_url`，头名保留。实测（真实 addon）同一条 302：
   `Referer → http://a/r?token=[REDACTED]`、`Location → http://a/n?token=[REDACTED]`、
   `redirectURL` 与 `Location` 头一致脱敏；`redact=False` 分支不脱敏但原值也不进 report（摘要无原值）。
   新单测 `tests/unit/test_har_writer.py:99-123` 覆盖 Referer/Location/Content-Location + 非敏感头 `X-Trace-Id` 保留。
   小瑕疵：`refresh` 的值形如 `5; url=…`，`scrub_url` 对这种"非 URL 前缀"值不会命中查询段脱敏（无副作用，只是不彻底）。
2. **`har_dropped` 不再逐条灌证据日志（有效，但 `droppedEntries` 语义变了）**：
   `netproxy_addon.py:139-146` 用 `dropped_reasons` 集合按原因去重，只 emit 首条。实测 `maxEntries=2` 写 27 条：
   **内存 `dropped_entries=23`，`net_proxy.jsonl` 里 `har_dropped` 恰好 1 行**——灌爆问题解决。
   副作用：`netprof.py:936` 仍把 `read_har_log_stats()["dropped"]`（=**事件行数**）直接当
   `HarSummary.droppedEntries`（=**条目数**）上报，于是真实丢 23 条时报告写 `droppedEntries: 1`（上限 2 种原因 → 恒 ≤2），
   `docs/嵌入指南.md:217` 仍按"条目上限触发 droppedEntries"描述。这是本轮新引入的口径失真：只低估、不谎报，
   `truncated=true` 与 `truncatedReason` 依旧正确，且该字段不参与任何指标计算，故**不构成阻断**；
   但第 1 轮的建议原文是"计数留在内存不可行时在 compose 侧折算"，折算这一步没做，
   且 `tests/unit/test_netprof_har.py:274` 已把 `dropped_entries == 1` 断言固化成"事件计数"语义。
   最小整改（任选其一）：drop 事件带上当时累计数并在后续事件覆盖、compose 侧改为 `droppedEvents`，
   或在 ADR/嵌入指南明确写"该字段是按原因去重后的事件数（下界），不是丢弃条目数"。
3. **`har_error` 写失败路径未去重（残留同类风险）**：`netproxy_addon.py:177-178` 与
   `har_writer.py:540-545` 对每条写失败的 flow 各 emit 一行 `har_error`。实测把 writer 句柄关闭后连打 30 条：
   `writer_errors=30`、`net_proxy.jsonl` **新增 30 行**。持续写失败（磁盘满/句柄失效）时仍会灌唯一生效证据日志。
   需要一条同样的"首错 + 计数由 `writer_errors` 表达"的处理，或按错误文本去重。
4. **addon 自动化覆盖（未处理，但有客观原因）**：全仓库仍无单测 import/exec `netproxy_addon.py` 的 HAR 分支
   （`grep -rn "addon" tests/unit/` 命中的 `test_netprof.py` 整形用例走 `_addon_probe` 子进程，但该文件不在白名单，
   本轮不能加 HAR 用例）。我这次的桩驱动 + 真子进程 E2E 已证明这些规则在真实 addon 里成立，
   建议后续轮把同一段桩测试落到白名单文件（如 `test_netprof_har.py`）里锁回归——否则问题 1 这类缺陷仍只能靠真机发现。
5. **空断言（未处理）**：`tests/unit/test_capture_report.py:147-157` 仍断言 `"PROBE_SECRET" not in dumped`，
   但 `_meta()/_network()/_har()` 载荷里根本没有该串（`grep -rn PROBE_SECRET tests/unit/` 只命中
   `test_capture_report.py:154` 与本文件 fixture 之外），断言恒真；`:156` 的 `"text" not in har` 同为零信息。
   往 `har` 载荷塞一个敏感原值再断言即可。
6. **`max_hash_bytes` 命名口径（未处理）**：任务书写 `max_hash_mb=8MiB`，实现为 `max_hash_bytes`
   （`har_writer.py:112`，env 键 `maxHashBytes`，`netproxy_addon.py:137`）。`docs/`、`README.md`、`GLOSSARY.md`
   里搜不到 `max_hash_bytes`/`maxHashBytes`，只写"大 body 不计算摘要并标记"（ADR-0005:32），
   读者无法按名寻址这个旋钮。纯文档一行补充。
7. **非 entry 结构的合法 JSON 行照抄（未变，第 1 轮已判可接受）**：`compose_har`（`har_writer.py:631-636`）
   只跳"非法 JSON"和"非 dict"，`{"a":1}` 会进最终 HAR。

---

## 三、本轮新发现的真实缺陷（不阻断）

**主进程残留的 `MOBILE_PERF_HAR*` 环境变量会让"默认关闭"的抓包在关闭态被激活。**
- 位置：`netprof.py:987`（`environment = dict(os.environ if self._env is None else self._env)`）
  与 `:993-1008`（仅 `capture.enabled` 时才 set 两个键，未开启时不 set 也不删除）。
- 触发输入：外层 shell/CI/封装脚本曾 `export MOBILE_PERF_HAR='{"entriesPath":"…"}'`（例如有人照文档手工调试 addon 后忘了清），
  再跑不带 `--capture-har` 的 `mobile-perf collect`：addon `_init_har()` 读到继承值 → 建 writer → 在**非本 run 的路径**写
  `app.har.entries.jsonl`（甚至合成产物），而主进程侧 `har` 摘要、事件、报告全都不知道，任务书 §2.1
  "capture_har=False 时不注入 HAR 环境变量、不创建 `app.har*`" 只在"由我方注入"的意义上成立。
- 证据：代码路径唯一入口是 env（`grep -n MOBILE_PERF_HAR src/mobile_perf/*.py` 只有 addon 读、netprof 条件写两处），
  没有任何"关闭态净化子进程环境"的分支。
- 修法一行：`else: environment.pop("MOBILE_PERF_HAR", None); environment.pop("MOBILE_PERF_HAR_ENTRIES", None)`。
  同理也建议对 `MOBILE_PERF_NETLOG`/`MOBILE_PERF_NETTIER` 保持"总是显式覆盖"（现在确实是总写入，无此问题）。

其余复核未发现新问题：`capture_har=False` 零行为（无 `har` 键、无事件、无文件）由
`test_netprof_har.py:234`、`test_capture_kit.py:273` 锁住且全绿；恢复顺序四步命令未变
（`test_netprof_har.py:302-315` 与第 1 轮一致）；`report.py:972-1026` 对 `status="failed"` 加了 ⚠️ 前缀、
`notTestedReason` 行仍 escaping、无 `har` 键时 `_network_markup` 零新增（`test_capture_report.py:109`）；
`SCHEMA_VERSION` 未改；主进程无 `mitmproxy`/`netproxy_addon` import，`har_writer.py` 纯标准库（复核 import 清单：
base64/hashlib/http.client/json/os/time/dataclasses/datetime/pathlib/typing/urllib.parse）。

## 四、越界核对（不使用 git）

按 mtime（`find … -newermt "2026-11-07 14:55"`）列出的本轮改动仅 7 个文件，全部在白名单内：
`src/mobile_perf/{har_writer,netproxy_addon,netprof,report}.py`、
`tests/device/test_netprof_device.py`、`tests/unit/{test_netprof_har,test_har_writer}.py`。
`kit.py`/`cli.py`/文档四件套本轮未触碰（无需触碰）；`collector.py`、`metrics.py`、`store.py`、`pyproject.toml`、
`SoloPi/`、`dispatch.sh`、`.scratch` 调研/设计文件均无新改动。

## 五、独立重跑的两条验证命令

- `python -m pytest tests/unit tests/contract -q --disable-warnings` → **680 passed, 8 skipped, 9 warnings**（exit 0；
  8 个 skip 全部是 `tests/device` 的 `-m device` 门控用例，不计通过）。
- `python -m ruff check src tests` → **All checks passed!**

## 六、结论与建议后续动作（均非阻断）

两条阻断缺陷已真实修复，并在"3.12 + 真 mitmdump 子进程"这条第 1 轮唯一失败的链路上拿到端到端证据；
非阻断加固中 URL 脱敏与 drop 事件去重有效。建议下一轮按优先级处理：
①关闭态净化 `MOBILE_PERF_HAR*` 继承环境变量；②`droppedEntries` 口径（改名或在 ADR/嵌入指南写明是去重事件下界，并修正
`test_netprof_har.py:274` 的断言语义）；③`har_error` 写失败同样去重；④把 addon 桩驱动 HAR 用例落进白名单单测文件；
⑤补 `test_capture_report.py` 的空断言与 `max_hash_bytes` 文档命名。
