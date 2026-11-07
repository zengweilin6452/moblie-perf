"""真机前置实验：不装证书，用当前 addon 观察明文 HTTP 请求/响应。

只启动本机回环 HTTP 服务、mitmdump、adb reverse 与系统代理；退出时按原值恢复。
不安装 CA、不卸载应用、不访问外网、不修改主项目源码。
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
ADB = ROOT / "SoloPi" / "platform-tools" / "adb.exe"
MITMDUMP = ROOT / ".scratch" / "venv-mitm" / "Scripts" / "mitmdump.exe"
ADDON = ROOT / "src" / "mobile_perf" / "netproxy_addon.py"
WORK = Path(__file__).resolve().parent / "probe-plain-http"
SERIAL = "749622c9"
PROXY_PORT = 8080
TEST_PORT = 18080
BROWSER = "com.android.browser"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"mobile-perf capture probe ok\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Set-Cookie", "session=PROBE_SECRET; Path=/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print("[test-server]", fmt % args, flush=True)


def adb(*args: str, timeout: float = 20.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(ADB), "-s", SERIAL, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    log_path = WORK / "net_proxy.jsonl"
    stdout_path = WORK / "mitmdump.stdout.log"
    original_proxy = adb("shell", "settings", "get", "global", "http_proxy").stdout.strip()
    original_reverse = adb("reverse", "--list").stdout
    print("[probe] original_proxy=", repr(original_proxy), flush=True)
    print("[probe] original_reverse=", repr(original_reverse.strip()), flush=True)

    server = ThreadingHTTPServer(("127.0.0.1", TEST_PORT), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    env = dict(os.environ)
    env.update(
        {
            "MOBILE_PERF_NETPARAMS": json.dumps(
                {"latency_ms": 0, "down_kbps": 0, "up_kbps": 0, "loss_pct": 0}
            ),
            "MOBILE_PERF_NETTIER": "passthrough",
            "MOBILE_PERF_NETLOG": str(log_path),
        }
    )
    cmd = [
        str(MITMDUMP),
        "-q",
        "--listen-port",
        str(PROXY_PORT),
        "--set",
        "connection_strategy=lazy",
        "-s",
        str(ADDON),
    ]
    with stdout_path.open("w", encoding="utf-8") as stdout:
        proxy = subprocess.Popen(cmd, stdout=stdout, stderr=subprocess.STDOUT, env=env)
        try:
            # 等代理监听
            for _ in range(50):
                if proxy.poll() is not None:
                    raise RuntimeError(f"mitmdump 提前退出: {stdout_path.read_text(encoding='utf-8')}")
                try:
                    adb("reverse", f"tcp:{PROXY_PORT}", f"tcp:{PROXY_PORT}", timeout=5.0)
                    break
                except subprocess.TimeoutExpired:
                    time.sleep(0.2)
            adb("reverse", f"tcp:{TEST_PORT}", f"tcp:{TEST_PORT}")
            adb("shell", "settings", "put", "global", "http_proxy", f"127.0.0.1:{PROXY_PORT}")
            time.sleep(0.5)
            adb("shell", "am", "force-stop", BROWSER)
            launch = adb(
                "shell",
                "am",
                "start",
                "-a",
                "android.intent.action.VIEW",
                "-d",
                f"http://127.0.0.1:{TEST_PORT}/__capture__/hello?token=PROBE_SECRET",
                "-p",
                BROWSER,
            )
            print("[probe] launch=", launch.returncode, launch.stdout.strip(), flush=True)
            time.sleep(6.0)
            adb("shell", "input", "keyevent", "3")  # HOME，不改变后台状态
        finally:
            # 恢复顺序与生产代码一致：清代理 → 拆两条 reverse → 停代理 → 停测试服务
            restore_proxy = adb("shell", "settings", "put", "global", "http_proxy", original_proxy or ":0")
            print("[probe] restore_proxy=", restore_proxy.returncode, flush=True)
            for port in (PROXY_PORT, TEST_PORT):
                removed = adb("reverse", "--remove", f"tcp:{port}")
                print(f"[probe] remove_reverse_{port}=", removed.returncode, removed.stdout.strip(), flush=True)
            proxy.terminate()
            try:
                proxy.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proxy.kill()
                proxy.wait(timeout=5)
            server.shutdown()
            server.server_close()

    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines() if log_path.is_file() else []
    events: dict[str, int] = {}
    http_requests: list[dict] = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        event = str(record.get("ev"))
        events[event] = events.get(event, 0) + 1
        if event in ("http_request", "http_connect", "client_connected"):
            http_requests.append(record)
    print("[probe] log_path=", log_path, flush=True)
    print("[probe] events=", json.dumps(events, ensure_ascii=False, sort_keys=True), flush=True)
    print("[probe] requests=", json.dumps(http_requests, ensure_ascii=False, indent=2), flush=True)
    print("[probe] final_proxy=", repr(adb("shell", "settings", "get", "global", "http_proxy").stdout.strip()), flush=True)
    print("[probe] final_reverse=", repr(adb("reverse", "--list").stdout.strip()), flush=True)
    return 0 if events.get("http_request", 0) else 2


if __name__ == "__main__":
    sys.exit(main())
