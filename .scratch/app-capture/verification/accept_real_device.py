"""APP 抓包真机双路径验收（不安装证书、不改系统安全设置）。

路径 A：内置浏览器访问本机明文 HTTP 测试端点（经 adb reverse），验证完整 HAR、脱敏与原子落盘。
路径 B：现有 targetSdk 36 正式包在 decrypt 档发起 HTTPS，验证证书拒绝后的如实降级。

只写 `.scratch/app-capture/verification/acceptance-*/` 下的证据；退出恢复系统代理与本脚本建立的
reverse。不会安装 CA、卸载 App 或改设备系统设置（除既有 http_proxy 采集窗口外）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from mobile_perf import netprof  # noqa: E402
from mobile_perf.adb import bundled_adb_path, default_call_device  # noqa: E402
from mobile_perf.har_writer import CaptureOptions  # noqa: E402

SERIAL = os.environ.get("MOBILE_PERF_DEVICE_SERIAL", "749622c9")
TARGET_PACKAGE = os.environ.get("MOBILE_PERF_DEVICE_TARGET", "com.sinosoft.trainapp")
BROWSER = os.environ.get("MOBILE_PERF_DEVICE_BROWSER", "com.android.browser")
ADB = bundled_adb_path()
MITMDUMP = Path(
    os.environ.get("MOBILE_PERF_MITMDUMP") or ROOT / ".scratch" / "venv-mitm" / "Scripts" / "mitmdump.exe"
)
PROXY_PORT = int(os.environ.get("MOBILE_PERF_NET_PORT", "8080"))
SECRET = "HAR_PROBE_SECRET"
DRIVE_SECONDS = float(os.environ.get("MOBILE_PERF_DRIVE_SECONDS", "9"))


class ProbeHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server 约定
        body = b'{"ok": true, "source": "mobile_perf_har_probe"}\n'
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Set-Cookie", f"sid={SECRET}; Path=/; HttpOnly")
        self.send_header("X-Trace-Id", "mobile-perf-probe")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print("[probe-server]", fmt % args, flush=True)


def cmd(*args: str, timeout_s: float = 20.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(ADB), "-s", SERIAL, *args],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )


def device(*args: str, timeout_s: float = 20.0):
    return cmd("shell", *args, timeout_s=timeout_s)


def caller():
    return default_call_device(str(ADB))


def proxy_events(path: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not path.is_file():
        return counts
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = str(json.loads(line).get("ev"))
        except json.JSONDecodeError:
            continue
        counts[event] = counts.get(event, 0) + 1
    return counts


def har_report(path: Path | None) -> dict:
    if path is None or not path.is_file():
        return {"exists": False}
    text = path.read_text(encoding="utf-8", errors="replace")
    payload = json.loads(text)
    entries = payload["log"]["entries"]
    return {
        "exists": True,
        "path": str(path),
        "sizeBytes": path.stat().st_size,
        "version": payload["log"]["version"],
        "entries": len(entries),
        "methods": sorted({str(item.get("request", {}).get("method", "")) for item in entries}),
        "hasConnectEntry": any(
            str(item.get("request", {}).get("method", "")).upper() == "CONNECT" for item in entries
        ),
        "secretPresent": SECRET in text,
        "tokenRedacted": "token=[REDACTED]" in text,
        "redactedMarkerPresent": "[REDACTED]" in text,
        "statuses": sorted({int(item.get("response", {}).get("status", 0)) for item in entries}),
    }


def start_server() -> tuple[ThreadingHTTPServer, int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), ProbeHandler)
    port = int(server.server_address[1])
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


def add_reverse(call, port: int) -> bool:
    result = call(["-s", SERIAL, "reverse", f"tcp:{port}", f"tcp:{port}"], 15.0)
    return int(getattr(result, "returncode", 1)) == 0


def remove_reverse(call, port: int) -> bool:
    result = call(["-s", SERIAL, "reverse", "--remove", f"tcp:{port}"], 15.0)
    return int(getattr(result, "returncode", 1)) == 0


def drive_probe_with_curl(port: int) -> dict:
    """用设备自带 curl/toybox nc 驱动明文测试端点，避免浏览器后台 HTTPS 噪声。

    先尝试 curl；设备无 curl 时用 toybox nc 发最小 HTTP/1.1 请求。端点返回体由
    本机服务记录，代理侧和 HAR 则给出请求/响应证据。
    """
    request = (
        f"GET /probe?token={SECRET} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Connection: close\r\n\r\n"
    )
    # 明文 loopback 端点经 reverse 映射；显式给 curl 的 --proxy 让请求进入 mitmproxy，
    # 再由代理按绝对 URL 转发到设备的 127.0.0.1:<port>。
    curl = device(
        "curl", "--proxy", f"http://127.0.0.1:{PROXY_PORT}", "--noproxy", "",
        "-sS", "-o", "/dev/null", "-w", "%{http_code}",
        f"http://127.0.0.1:{port}/probe?token={SECRET}", timeout_s=20.0,
    )
    if curl.returncode == 0 and curl.stdout.strip().startswith("2"):
        return {"method": "curl", "returncode": curl.returncode, "stdout": curl.stdout.strip()}
    quoted = request.replace("\r\n", "\\r\\n").replace('"', '\\"')
    # nc 备用路径直接连到代理，请求行用绝对 URL；命令由本脚本固定组装。
    absolute = request.replace(
        f"GET /probe?token={SECRET} HTTP/1.1",
        f"GET http://127.0.0.1:{port}/probe?token={SECRET} HTTP/1.1",
    )
    quoted = absolute.replace("\r\n", "\\r\\n").replace('"', '\\"')
    nc = cmd("shell", f'printf "{quoted}" | toybox nc 127.0.0.1 {PROXY_PORT}', timeout_s=20.0)
    return {"method": "toybox_nc", "returncode": nc.returncode, "stdout": nc.stdout[-500:]}


def plain_http_case(call, out_dir: Path) -> dict:
    print("== A. 明文 HTTP 完整 HAR 路径 ==", flush=True)
    server, test_port = start_server()
    reverse_set = add_reverse(call, test_port)
    if not reverse_set:
        raise RuntimeError(f"adb reverse tcp:{test_port} 建立失败")
    work = out_dir / "network-plain"
    profiler = netprof.NetProfiler(
        adb_path=str(ADB),
        serial=SERIAL,
        mitmdump=str(MITMDUMP),
        port=PROXY_PORT,
        tier=netprof.TIER_DECRYPT,
        call_device=call,
        capture=CaptureOptions(enabled=True),
    )
    launch = {}
    try:
        session = profiler.start(netprof.build_profile("none"), work_dir=work)
        if not session.applied:
            raise RuntimeError(f"代理未施加: {session.setup_errors}")
        # 强制让请求走 http_proxy：Android 对 127.0.0.1 可能按本地地址绕过代理。
        # 使用设备侧 loopback 别名 + http_proxy，使 mitmproxy 能观察到明文请求。
        launch = drive_probe_with_curl(test_port)
        time.sleep(1.0)
    finally:
        session = profiler.stop()
        reverse_removed = remove_reverse(call, test_port)
        server.shutdown()
        server.server_close()

    har = session.har
    report = har_report(Path(har.path) if har and har.path else None)
    return {
        "testPort": test_port,
        "browserLaunch": launch,
        "reverseRemoved": reverse_removed,
        "har": har.as_dict() if har else None,
        "harFile": report,
        "proxyEvents": proxy_events(work / netprof.LOG_FILENAME),
        "doctor": netprof.doctor(call, SERIAL, PROXY_PORT),
    }


def cert_rejection_case(call, out_dir: Path) -> dict:
    print("== B. 正式包 HTTPS 证书拒绝降级路径 ==", flush=True)
    work = out_dir / "network-cert-rejected"
    profiler = netprof.NetProfiler(
        adb_path=str(ADB),
        serial=SERIAL,
        mitmdump=str(MITMDUMP),
        port=PROXY_PORT,
        tier=netprof.TIER_DECRYPT,
        call_device=call,
        capture=CaptureOptions(enabled=True),
    )
    try:
        session = profiler.start(netprof.build_profile("none"), work_dir=work)
        if not session.applied:
            raise RuntimeError(f"代理未施加: {session.setup_errors}")
        device("am", "force-stop", TARGET_PACKAGE)
        device("monkey", "-p", TARGET_PACKAGE, "1")
        time.sleep(DRIVE_SECONDS)
        device("input", "swipe", "540", "1600", "540", "600", "300")
        time.sleep(2.0)
    finally:
        session = profiler.stop()

    har = session.har
    report = har_report(Path(har.path) if har and har.path else None)
    return {
        "targetPackage": TARGET_PACKAGE,
        "trust": session.trust,
        "trustError": session.trust_error,
        "effective": session.effective,
        "apiEffective": session.api_effective,
        "har": har.as_dict() if har else None,
        "harFile": report,
        "proxyEvents": proxy_events(work / netprof.LOG_FILENAME),
        "doctor": netprof.doctor(call, SERIAL, PROXY_PORT),
    }


def validate(plain: dict, rejected: dict) -> dict:
    checks: dict[str, bool] = {}
    ph = plain["har"]
    pf = plain["harFile"]
    checks["plainHARExists"] = bool(pf["exists"] and pf["version"] == "1.2" and pf["entries"] >= 1)
    checks["plainHARHas200"] = 200 in pf.get("statuses", [])
    checks["plainHARNoConnect"] = not pf.get("hasConnectEntry", True)
    checks["plainHARSecretRedacted"] = not pf.get("secretPresent", True) and (
        pf.get("redactedMarkerPresent", False) or pf.get("tokenRedacted", False)
    )
    checks["plainHARSummaryEnabled"] = bool(ph and ph["enabled"] and ph["entries"] >= 1)
    checks["plainHARStatusHonest"] = bool(ph and ph["status"] in {"complete", "partial"})
    checks["plainCoverageHonest"] = bool(
        ph and (ph["httpsCoverage"] == "none" or "HTTPS 未解密" in (ph.get("notTestedReason") or ""))
    )
    checks["plainDoctorClean"] = bool(plain["doctor"]["proxyClean"] and plain["doctor"]["reverseClean"])
    checks["plainReverseRemoved"] = bool(plain["reverseRemoved"])

    rh = rejected["har"]
    rf = rejected["harFile"]
    checks["rejectedHARHonest"] = bool(rh and rh["status"] in {"not_tested", "partial"})
    checks["rejectedNoEmptyHAR"] = bool(rh and (rh["status"] != "not_tested" or not rf["exists"]))
    checks["rejectedNoConnect"] = not rf.get("hasConnectEntry", True) if rf["exists"] else True
    checks["rejectedSecretRedacted"] = not rf.get("secretPresent", True) if rf["exists"] else True
    checks["rejectedHttpsCoverageHonest"] = bool(rh and rh["httpsCoverage"] in {"none", "unknown"})
    checks["rejectedTrustOrExplanation"] = bool(
        rejected["trust"] == netprof.TRUST_REJECTED
        or "证书" in (rh or {}).get("notTestedReason", "")
        or "未观察到" in (rh or {}).get("notTestedReason", "")
    )
    checks["rejectedApiStillNotEffective"] = rejected["apiEffective"] is False
    checks["rejectedDoctorClean"] = bool(rejected["doctor"]["proxyClean"] and rejected["doctor"]["reverseClean"])
    return checks


def main() -> int:
    if not ADB.is_file() or not MITMDUMP.is_file():
        print("缺少 adb 或 mitmdump", flush=True)
        return 2
    devices = cmd("get-state").stdout.strip()
    if devices != "device":
        print(f"设备 {SERIAL} 不在线: {devices}", flush=True)
        return 2
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = ROOT / ".scratch" / "app-capture" / "verification" / f"acceptance-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=False)
    call = caller()
    initial = {
        "serial": SERIAL,
        "proxy": device("settings", "get", "global", "http_proxy").stdout.strip(),
        "reverse": cmd("reverse", "--list").stdout.strip(),
        "foreground": device("dumpsys", "activity", "activities").stdout[
            : device("dumpsys", "activity", "activities").stdout.find("\n") + 1
        ],
    }
    try:
        plain = plain_http_case(call, out_dir)
        rejected = cert_rejection_case(call, out_dir)
        checks = validate(plain, rejected)
        final = {
            "serial": SERIAL,
            "proxy": device("settings", "get", "global", "http_proxy").stdout.strip(),
            "reverse": cmd("reverse", "--list").stdout.strip(),
            "doctor": netprof.doctor(call, SERIAL, PROXY_PORT),
        }
        result = {
            "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "mitmdump": str(MITMDUMP),
            "certInstalled": False,
            "initial": initial,
            "plainHttp": {k: v for k, v in plain.items() if k != "har"},
            "certRejection": rejected,
            "final": final,
            "checks": checks,
            "passed": all(checks.values()) and final["doctor"]["proxyClean"] and final["doctor"]["reverseClean"],
        }
        path = out_dir / "result.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"resultPath": str(path), "checks": checks, "passed": result["passed"]}, ensure_ascii=False, indent=2), flush=True)
        return 0 if result["passed"] else 1
    finally:
        # 只清本脚本可能遗留的代理设置；reverse 的 8080 由 NetProfiler 恢复，测试端口在各 case 内恢复。
        if device("settings", "get", "global", "http_proxy").stdout.strip() not in ("", ":0", "null"):
            device("settings", "put", "global", "http_proxy", initial["proxy"] or ":0")


if __name__ == "__main__":
    sys.exit(main())
