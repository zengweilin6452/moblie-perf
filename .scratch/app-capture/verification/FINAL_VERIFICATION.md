# APP 抓包终验证据（2026-11-07）

## 1. 测试与静态检查

```bash
MOBILE_PERF_MITMDUMP=<repo>/.scratch/venv-mitm/Scripts/mitmdump.exe \
  python -m pytest tests/unit tests/contract -q --disable-warnings
python -m ruff check src tests
```

- pytest：exit 0，全部通过；启用本地 mitmproxy 后原先 8 个 addon 集成用例不再 skip。
- ruff：exit 0，`All checks passed!`。
- 证据：`.scratch/app-capture/verification/final-unit.log`、`final-ruff.log`。

## 2. 审查缺陷的红绿对照

- 红：Python 3.12 下按旧 `importlib.util.module_from_spec + exec_module` 加载 `har_writer.py`，抛 `AttributeError: 'NoneType' object has no attribute '__dict__'`。
- 绿：`sys.modules[spec.name] = module` 后 `CaptureOptions()` 正常构造；在 3.12 解释器里加载真实 addon、`response()` 写条目、`compose_har()` 原子合成全部成功。
- 修复：动态模块先注册 `sys.modules`，失败时撤销；HAR init 失败不再伪装成「App 没发请求」，而是 `status=failed` 并写明 `init:` 错误。

## 3. 真机双路径验收（不安装证书）

设备：`749622c9`（Xiaomi 2206123SC / Android 14），系统代理与 `adb reverse` 均在采集窗口后恢复。

证据：`.scratch/app-capture/verification/acceptance-20261107-152728/result.json`，`passed=true`。

### 路径 A：完整明文 HTTP HAR

- 设备侧 `curl --proxy http://127.0.0.1:8080` 访问经 `adb reverse` 映射的本机 `127.0.0.1:<port>/probe?token=HAR_PROBE_SECRET`。
- 本地服务返回 200；代理侧有 `http_request`、`api_request`；`app.har` 可解析，HAR 1.2、1 条 GET 200 条目、无 CONNECT 伪请求。
- `?token=[REDACTED]`、`Set-Cookie=[REDACTED]`、正文仅 `size+mimeType+sha256`；全文不含 `HAR_PROBE_SECRET`。
- `network.har.status=partial`、`httpsCoverage=none`，如实说明 HTTPS 未解密。

### 路径 B：正式包证书拒绝降级

- `com.sinosoft.trainapp` 在 decrypt 档发起 HTTPS；代理日志观察到 25 个 `http_connect`、`trust_probe ok=false`（证书不信任）。
- `app.har` 不存在（不创建空 HAR），`har.status=not_tested`；`apiEffective=false`，连接级证据仍在。
- 全程未安装 CA、未改系统安全设置；`netprof doctor`：`proxyClean=true`、`reverseClean=true`。

## 4. 设备卫生

最终读取：设备在线，`http_proxy=:0`，`reverse` 列表为空；`mobile-perf netprof doctor --port 8080` 返回 `proxyClean/reverseClean=true`、`cleaned=false`。

## 5. 已知边界

- HTTPS 完整解密需被测 App 信任代理 CA；QUIC/UDP、非 HTTP TCP、证书 pinning 与未关闭的 WebSocket 长连接不保证覆盖。
- 默认脱敏名单覆盖常见头/Cookie/查询参数；自定义敏感头与路径段中的 token 仍需宿主按业务补充策略。
- 本次真机验收验证明文 HTTP 完整 HAR 与正式包 HTTPS 降级，未验证「信任代理 CA 的 debug 包或 root 设备」下的 HTTPS HAR。
