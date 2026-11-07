VERDICT: FAIL

审查对象：`handoff-round-1.md`（APP 抓包 HAR）交付物。独立重跑结果与载荷一致：
`python -m pytest tests/unit tests/contract -q --disable-warnings` 全部通过（含 8 个 device skip），
`python -m ruff check src tests` → All checks passed!。白名单核对：今天被改写的文件仅
`har_writer.py / netproxy_addon.py / netprof.py / kit.py / cli.py / report.py / README.md /
GLOSSARY.md / docs/嵌入指南.md / docs/adr/0005-app-har-capture.md / tests/unit/test_capture_*.py /
test_har_writer.py / test_netprof_har.py / tests/device/test_netprof_device.py`，无越界改动
（`.scratch/app-capture/verification/probe_plain_http.py` 时间戳 14:18，早于写作者工作窗口 14:35–14:52，
属编排方产物，不计越界）。主进程无 `netproxy_addon`/`mitmproxy` import，`har_writer.py` 纯标准库，均符合。

但真机链路上有一个阻断缺陷和一个真机用例缺陷，unit 测试全部覆盖不到，故判 FAIL。

---

## 问题 1（阻断）：addon 动态加载 har_writer 必然失败，抓包在真实子进程里完全不生效

- **位置**：`src/mobile_perf/netproxy_addon.py:91-99`（`_load_har_writer`，尤其 `spec.loader.exec_module(module)`，line 98）；
  触发点 `netproxy_addon.py:174`（模块级 `_init_har()`）→ `netproxy_addon.py:117`。
- **原因**：`module_from_spec` 之后**没有把模块注册进 `sys.modules`** 就 `exec_module`。`har_writer.py` 同时具备
  ① `from __future__ import annotations`（line 16，所有注解变成字符串）②`@dataclass(frozen=True)`（line 102-103）。
  CPython 3.12 的 `dataclasses._is_type` 在无模块名前缀分支上是
  `ns = sys.modules.get(cls.__module__).__dict__`（`D:/python312/Lib/dataclasses.py:749`，无 None 保护），
  `cls.__module__ == "mobile_perf_har_writer"` 不在 `sys.modules` → 返回 None → `AttributeError`。
  而 mitmproxy 子进程解释器按本项目文档恰好是 **≥3.12**（`netproxy_addon.py` 模块文档、README、ADR-0004），
  即真实运行环境必然命中该分支。任务书 §2.2 明确要求"按同目录文件加载 har_writer.py（不能依赖主包已安装）"，
  当前写法在该约束下不可用。
- **触发输入**：任何真实抓包，例如
  `mobile-perf collect <pkg> --capture-har`（mitmdump 装在 3.12 venv、`MOBILE_PERF_MITMDUMP` 指过去）。
  代理照常转发（异常被吞），但 `_HAR_WRITER` 恒为 None → `net_proxy.jsonl` 只多一条
  `{"ev":"har_error","error":"init: 'NoneType' object has no attribute '__dict__'"}`，
  `app.har.entries.jsonl` 都不会被创建，`app.har` 永不产出，`har.status` 恒为 `not_tested`。
- **复现证据**（在系统临时目录跑，未落仓库；桩掉 mitmproxy 后 import addon 文件）：
  - 按现写法加载：`as-written: FAIL -> AttributeError 'NoneType' object has no attribute '__dict__'`；
    traceback 落在 `har_writer.py:102 @dataclass(frozen=True)` → `dataclasses.py:749`。
  - 只补一行 `sys.modules[spec.name] = module` 后重载：`registered: OK`，addon 后续链路
    （明文 HTTP 在 passthrough 档写条目、CONNECT 不写成请求、`error()` 落 `_error` 条目、
    `max_entries` 截断并 emit `har_dropped`、`compose_har` 原子合成且无 `.tmp` 残留）经同样的桩驱动验证**全部正确**
    ——也就是说缺陷面就是这一行。
- **附带影响（也违反 §2.1 的"如实原因"）**：init 失败后 JSONL 不存在，`_compose_har`
  （`netprof.py:943-946`）给出的 `notTestedReason` 是
  "代理侧未观察到任何 HTTP 请求：被测 App 可能走缓存/未发起请求…"，把"我方写入链路没起来"
  说成"App 没发请求"，指错排查方向（唯一线索是 `har.writerErrors≥1`，报告里不显著）。
- **建议修法**：`_load_har_writer` 在 `exec_module` 前 `sys.modules[spec.name] = module`（失败时 `pop` 掉），
  或改用 `importlib.import_module` 之外的显式注册路径；同时把 `init:` 类 `har_error`
  反映成 `status="failed"` 或至少让 `notTestedReason` 引用 `har_error` 文本。

## 问题 2（真机门控用例失效）：用 `adb shell reverse` 建立明文 HTTP 通道，命令本身不成立

- **位置**：`tests/device/test_netprof_device.py:217`、`tests/device/test_netprof_device.py:255`
  （helper 定义在 `:55-56`：`_shell` 会拼成 `["-s", serial, "shell", *args]`）。
- **原因**：`reverse` 是 **adb 宿主侧子命令**，不是设备端可执行文件。正确用法见
  `src/mobile_perf/netprof.py:1051`（`["-s", serial, "reverse", ...]`）与 `netprof.py:1108`。
  写成 `_shell(caller, "reverse", ...)` 得到
  `adb -s <serial> shell reverse tcp:<port> tcp:<port>` → 设备 shell 报 `reverse: not found`，
  返回码未被断言，静默失败。
- **触发输入**：编排方按 §3.6 用 `-m device` 跑新增用例
  `test_har_plain_http_local_endpoint_records_redacted_request`：设备侧 `127.0.0.1:<port>`
  因没有 reverse 而连不上 PC 上的明文 HTTP 服务 → 零明文请求 → `har.status == "not_tested"`、
  `har.entries == 0`，断言 `har.status in {"complete","partial"}`（`:261`）与 `har.entries >= 1`（`:262`）失败；
  `:255` 的清理同样无效。任务书要求的"明文 HTTP 本地端点"这条真机分支实际未被覆盖。
- **建议修法**：改用宿主命令 `caller(["-s", DEVICE_SERIAL, "reverse", f"tcp:{port}", f"tcp:{port}"], ...)`，
  并对返回码做断言（失败即明确报"reverse 未建立"，而不是让后面的 HAR 断言背锅）。

---

## 非阻断观察（不构成本轮 FAIL 的额外理由，供下一轮一并处理）

1. **默认脱敏漏了"值本身是 URL"的响应/请求头**：`har_writer.py:214-223` 的 `scrub_headers` 只按头**名**判断，
   `Location` / `Referer` / `Content-Location` 这类值是 URL，其敏感查询参数原样进 `app.har`。
   实测：同一条 302，`response.redirectURL` 已写成 `...?token=[REDACTED]`，
   而 `response.headers[0].value` 仍是 `http://a.test/n?token=SECRET`；`request.headers` 里的 Referer 同理。
   任务书 §2.2 的名单是"至少"，属可加固项；但它与 `_redirect_url` 的脱敏意图自相矛盾，建议对已知 URL 型头走 `scrub_url`。
2. **`har_dropped` 事件按被丢弃条数逐条 emit**（`har_writer.py:527-550` + `netproxy_addon.py:131`）：
   命中 `max_entries` 后每丢弃一条就向"唯一生效证据" `net_proxy.jsonl` 追加一行且每次 `open/close`，
   重流量场景会把代理日志灌爆（5000 上限之后仍持续写）。建议只记首个原因、计数留在内存不可行时在
   compose 侧折算，避免每丢弃一条写一行。
3. **`netproxy_addon.py` 零自动化覆盖**：全仓库没有任何测试 import/exec 该文件（`grep -rn netproxy_addon tests/` 空），
   §2.2 的 addon 侧规则（三钩子写条目、CONNECT 不写、env 解析、tier 拦截解除）只能靠真机验证——
   问题 1 正是从这个缺口漏过的。建议加一个用 `sys.modules` 桩住 mitmproxy、再按文件 exec addon 的单测
   （顺带就能锁死问题 1 的回归）。
4. **空断言**：`tests/unit/test_capture_report.py:147-156` 断言 `"PROBE_SECRET" not in dumped`，
   但该用例的 fixture 里根本没有这个串，永远为真。建议往 `network`/`har` 载荷里真放一个敏感串再断言。
5. **命名与规范用词不一致（纯文档层面）**：任务书写 `max_hash_mb=8MiB`，实现为 `max_hash_bytes`
   （`har_writer.py:112`，env 键 `maxHashBytes`）；任务书写"`compose_har(..., *, meta)`"，实现 `meta=None` 可省。
   语义等价，建议 ADR/嵌入指南里以 `max_hash_bytes` 口径写清，避免下轮读者按 mb 传值。
6. `compose_har` 对"合法 JSON 但不是 entry 结构"的行（如 `{"a":1}`）照抄进 entries（实测 skipped=2/entries=1），
   产出的 HAR 里可能含非法 entry。任务书只要求"跳过坏行"，可接受；如要更硬可校验必备键。

---

## 已逐条核对且通过的规范点（抽样说明，避免下轮重复劳动）

- §2.1 CLI 六个 flag 与默认值/文档、`CaptureOptions` 限额正整数早失败（0/-1/"5"/True/1.5 全部 ValueError）、
  细项脱离主开关 exit 1、显式 `passthrough` 冲突 exit 1、`--capture-har` 单开时 tier=decrypt 且 `net_profile=None`。
- §2.1 正交语义：`sessions.net_profile` 写 None + `network.captureOnly=true` + `profile` 不伪称档位
  （`kit.py:532-535`、`kit.py:716-720`），且有单测（`test_capture_kit.py:199`），符合"唯一允许的兼容性调整须有单测"。
- §2.2 HAR 形状与口径：`headersSize=-1`、`bodySize` 真实长度、重复头保留（实测两条 `Cookie` 都在）、
  `time == sum(timings)`、错误 flow `status=0` 且 `_error` 非空、默认 size+MIME+sha256、
  `--capture-har-bodies` 截断标记与二进制 base64、>8MiB 跳过摘要并标记、脱敏名单 14 头/24 查询键与任务书逐字一致、
  userinfo 用 `REDACTED:REDACTED@`（已实测 `[REDACTED]@host` 会让 `urlsplit` 抛 ValueError，该偏差合理且已注释）。
- §2.2 耐久：逐行 `ensure_ascii=False` + 每行 flush、`os.replace` 原子落盘、零有效条目不创建 `app.har`、
  `stop()` 幂等不重复合成/不残留 `.tmp`、合成在设备恢复与进程终止之后、恢复顺序 4 步命令未变。
- §2.3 报告：`build_report` 无新增顶层参数、`_har_rows_html` 六类行齐备（HAR/产物/正文/脱敏/截断/覆盖/原因）、
  相对链接 `network/app.har`、无 `har` 键时 `_network_markup` 零新增、`redacted=false` 在 HTML 显著标⚠️、
  报告 JSON/HTML 不内嵌正文、`SCHEMA_VERSION` 未改。
- §3.1–§3.5 用例清单逐条比对：要求的每种场景都有对应断言（本审查未发现缺项）；device 用例按双 skipif 门控追加、
  旧 5 个用例结构完好，仅问题 2 的那两行需要修。
- 文档四件套：README（能力/CLI 示例/QUIC·UDP·非 HTTP TCP·pinning·未关闭 WS 限制/mitmproxy MIT 致谢）、
  GLOSSARY「APP 抓包产物」、嵌入指南 3.3 库 API 与降级、ADR-0005（hardump 拒绝理由、流式 JSONL+原子合成、
  默认脱敏/元数据正文、正交）均落地。
