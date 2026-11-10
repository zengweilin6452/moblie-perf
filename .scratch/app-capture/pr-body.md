## Summary

```text
PerfKit.collect / mobile-perf collect
 └─ capture_har=True（默认关闭）
     ├─ NetProfiler（none + decrypt 或既有网络剖面）
     │   └─ mitmproxy 子进程 / netproxy_addon.py
     │       └─ app.har.entries.jsonl   # 逐行写入即脱敏，每行 flush
     └─ NetProfiler.stop()
         ├─ 恢复设备系统代理与 adb reverse
         └─ compose_har()              # 跳过坏行、.tmp + os.replace
             └─ runs/<runId>/network/app.har
                 └─ meta.network.har / report.json / report.html
```

```text
src/mobile_perf/
├── har_writer.py          # 新增：HAR 1.2、脱敏、限额、JSONL writer、原子合成
├── netproxy_addon.py      # 抓包钩子、明文 HTTP 记录、初始化失败留痕
├── netprof.py             # 生命周期、HAR 摘要、诚实降级与恢复
├── kit.py / cli.py        # capture_har / CaptureOptions / --capture-* 参数
└── report.py              # network.har 摘要、相对链接、危险/降级标注
```

- 为什么：现有组件已有弱网编排和 HTTP(S) 代理，但没有可交付的请求/响应证据产物；HAR 1.2 可直接用于 DevTools 和分析脚本。
- 抓包与网络剖面正交：单独开启时内部用 `none + decrypt` 起代理，但不写 `sessions.net_profile`，不污染趋势/基线可比性。
- 默认只记正文 `size + MIME + sha256`；默认脱敏头、Cookie、URL 查询参数、userinfo 与 URL 型头。正文需显式 `--capture-har-bodies`。
- Windows 耐久：不依赖 mitmproxy `done()` / `hardump`；addon 逐行 flush，主进程在子进程终止后原子合成。
- 诚实降级：mitmproxy 缺失、端口占用、零请求、证书拒绝或初始化失败分别给出 `not_tested` / `partial` / `failed`，不创建空 HAR、不把 CONNECT 伪装成请求。

## Evidence

- **Before:** 无 APP 抓包开关；`response()` 仅在 decrypt 档记录 URL/状态码/响应大小，无请求头、响应头、HAR 产物或报告入口。
  **After:** `mobile-perf collect <pkg> --capture-har` 或 `kit.collect(..., capture_har=True)` 生成 `network/app.har`、JSONL 中间产物、`network.har` 摘要及 HTML 链接。

- **Before:** Python 3.12 mitmdump 子进程动态加载 `har_writer.py` 未注册 `sys.modules`，dataclass 初始化抛 `AttributeError`，抓包能力静默失效；跨引擎审查首轮 `VERDICT: FAIL`。
  **After:** 注册动态模块并在失败时撤销；最终 Qoder 只读复审 `VERDICT: PASS`。红绿对照：旧加载 `FAIL -> AttributeError`，修复后 `OK metadata`，真实 addon 可写条目并合成 HAR。

- **Before:** 新增真机明文用例误用 `adb shell reverse`，通道必然失败。
  **After:** 改为宿主侧 `adb -s <serial> reverse` 并断言返回码；真机验收脚本同样独立使用宿主侧 reverse。

- **Tests:** `MOBILE_PERF_MITMDUMP=<Python 3.12 venv>/mitmdump.exe python -m pytest tests/unit tests/contract -q --disable-warnings` → 全部通过（启用可选依赖后 addon 集成用例不再 skip）；`python -m ruff check src tests` → `All checks passed!`

- **Real device:** 真机 `749622c9`，不安装证书，双路径 17/17 检查通过：
  - 明文 HTTP：`curl --proxy` 经 `adb reverse` 访问本地端点，HAR 1.2 可解析，1 条 GET 200，`token=[REDACTED]`、`Set-Cookie=[REDACTED]`、无 CONNECT 伪请求、正文仅元数据。
  - HTTPS：正式包 `com.sinosoft.trainapp` 证书拒绝，代理侧观察到 CONNECT 与 `trust_probe ok=false`，HAR `not_tested` 且无空文件，`apiEffective=false`。
  - 最终 `http_proxy=:0`、`adb reverse` 为空、`netprof doctor` 的 `proxyClean/reverseClean=true`。

## Merge Danger

**Door:** Two-way

新增能力默认关闭；既有采集、弱网档位、趋势/基线判定和设备恢复路径均保留。若需回退，可还原本组提交，已生成的 `runs/<runId>` HAR 是独立产物，不要求数据库迁移。

**Blast Radius:** Network

`netproxy_addon.py` 的明文 HTTP `response()` 现在会写 HAR 条目，但只在 `MOBILE_PERF_HAR` 环境变量存在时生效；关闭抓包时不注入该变量且不创建产物。`capture_har=True` 要求 decrypt 档，显式 passthrough 会早失败。

已知边界：HTTPS 完整解密仍需被测 App 信任代理 CA；QUIC/UDP、非 HTTP TCP、证书 pinning、未关闭的 WebSocket 长连接不保证覆盖。自定义敏感头和路径段中的 token 需宿主按业务补充策略。仓库内保留了调研、跨引擎审查和真机验收证据；未安装证书、未改系统安全设置。
