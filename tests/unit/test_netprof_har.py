"""NetProfiler 的 HAR 接线：环境变量、合成幂等、诚实降级与恢复顺序。

不 import tests/unit/test_netprof.py 的私有符号；本文件自带最小替身。
"""

import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from mobile_perf import netprof
from mobile_perf.har_writer import CaptureOptions


class FakeDevice:
    def __init__(self):
        self.calls = []

    def __call__(self, args, timeout_s=15.0):
        self.calls.append(list(args))
        return SimpleNamespace(stdout="", returncode=0, stderr="")

    def commands(self):
        return [" ".join(call) for call in self.calls]


class FakeProcess:
    def __init__(self):
        self.terminated = False
        self.killed = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


def _profiler(device, *, available=True, port_busy=False):
    state = {"busy": port_busy}
    calls = []

    def popen(args, **kwargs):
        calls.append((args, kwargs))
        state["busy"] = True
        return FakeProcess()

    profiler = netprof.NetProfiler(
        adb_path="adb",
        serial="s1",
        mitmdump="/fake/mitmdump",
        tier=netprof.TIER_DECRYPT,
        call_device=device,
        popen=popen,
        sleep=lambda _s: None,
        now=lambda: 0.0,
        probe=lambda _p: "12.3.0" if available else None,
        which=lambda _n: None,
        in_use=lambda _port: state["busy"],
        capture=CaptureOptions(enabled=True),
    )
    profiler.popen_calls = calls
    return profiler


def _entry() -> dict:
    return {
        "startedDateTime": "2026-11-07T00:00:00.000Z",
        "time": 1.0,
        "request": {"method": "GET", "url": "http://127.0.0.1:18080/a"},
        "response": {"status": 200},
        "timings": {"send": 0.0, "wait": 1.0, "receive": 0.0},
    }


def _write_entries(work_dir: Path, count: int = 1) -> None:
    path = work_dir / "app.har.entries.jsonl"
    path.write_text(
        "".join(json.dumps(_entry(), ensure_ascii=False) + "\n" for _ in range(count)),
        encoding="utf-8",
    )


def _write_log(work_dir: Path, records) -> None:
    (work_dir / netprof.LOG_FILENAME).write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def test_capture_environment_is_json_and_survives_spaces_and_unicode(tmp_path):
    work_dir = tmp_path / "run 空格" / "network"
    profiler = _profiler(FakeDevice())

    profiler.start(netprof.build_profile("none"), work_dir=work_dir)

    _args, kwargs = profiler.popen_calls[0]
    config = json.loads(kwargs["env"]["MOBILE_PERF_HAR"])
    entries_path = kwargs["env"]["MOBILE_PERF_HAR_ENTRIES"]
    assert config["entriesPath"] == entries_path
    assert entries_path == str(work_dir / "app.har.entries.jsonl")
    assert " " in entries_path and "空格" in entries_path
    assert config["bodyMode"] == "metadata"
    assert config["redact"] is True
    assert config["maxEntries"] == 5000


def test_stop_composes_har_and_is_idempotent(tmp_path):
    profiler = _profiler(FakeDevice())
    profiler.start(netprof.build_profile("none"), work_dir=tmp_path)
    _write_entries(tmp_path)
    _write_log(
        tmp_path,
        [
            {"ev": "trust_probe", "ok": True, "host": "api.test"},
            {"ev": "http_request", "fid": "f1", "host": "api.test", "path": "/a"},
        ],
    )

    first = profiler.stop()
    second = profiler.stop()

    har_path = tmp_path / "app.har"
    assert har_path.is_file()
    assert json.loads(har_path.read_text(encoding="utf-8"))["log"]["entries"]
    assert first.har.status == netprof.STATUS_COMPLETE
    assert first.har.entries == 1
    assert first.har.https_coverage == "full"
    assert second.har is first.har
    assert not (tmp_path / "app.har.tmp").exists()


def test_missing_mitmproxy_does_not_create_empty_har(tmp_path):
    device = FakeDevice()
    profiler = _profiler(device, available=False)

    session = profiler.start(netprof.build_profile("none"), work_dir=tmp_path)
    stopped = profiler.stop()

    assert session.applied is False
    assert stopped.har.enabled is True
    assert stopped.har.status == netprof.STATUS_NOT_TESTED
    assert "mitmproxy" in stopped.har.not_tested_reason
    assert "MOBILE_PERF_MITMDUMP" in stopped.har.not_tested_reason
    assert not (tmp_path / "app.har").exists()
    assert device.calls == []


def test_zero_http_requests_does_not_create_empty_har(tmp_path):
    profiler = _profiler(FakeDevice())
    profiler.start(netprof.build_profile("none"), work_dir=tmp_path)
    _write_log(
        tmp_path,
        [
            {"ev": "params", "harEnabled": True},
            {"ev": "tcp_start", "fid": "f1", "host": "api.test", "port": 443},
        ],
    )

    stopped = profiler.stop()

    assert stopped.har.status == netprof.STATUS_NOT_TESTED
    assert "未观察到任何 HTTP 请求" in stopped.har.not_tested_reason
    assert not (tmp_path / "app.har").exists()


def test_cert_rejection_with_plaintext_entries_is_partial_only_plain_http(tmp_path):
    profiler = _profiler(FakeDevice())
    profiler.start(netprof.build_profile("none"), work_dir=tmp_path)
    _write_entries(tmp_path)
    _write_log(
        tmp_path,
        [
            {
                "ev": "trust_probe",
                "ok": False,
                "error": "tlsv1 alert unknown ca",
                "host": "api.test",
            },
            {"ev": "http_request", "fid": "f1", "host": "127.0.0.1", "path": "/a"},
        ],
    )

    stopped = profiler.stop()

    assert stopped.har.status == netprof.STATUS_PARTIAL
    assert stopped.har.https_coverage == "none"
    assert "HTTPS 未解密" in stopped.har.not_tested_reason
    assert (tmp_path / "app.har").is_file()
    payload = json.loads((tmp_path / "app.har").read_text(encoding="utf-8"))
    assert payload["log"]["entries"][0]["request"]["method"] == "GET"


def test_session_as_dict_includes_har_contract_fields(tmp_path):
    profiler = _profiler(FakeDevice())
    profiler.start(netprof.build_profile("none"), work_dir=tmp_path)
    _write_entries(tmp_path)
    _write_log(
        tmp_path,
        [
            {"ev": "trust_probe", "ok": True, "host": "api.test"},
            {"ev": "http_request", "fid": "f1", "host": "api.test", "path": "/a"},
        ],
    )

    payload = profiler.stop().as_dict()

    assert set(payload["har"]) == {
        "enabled",
        "status",
        "path",
        "entriesPath",
        "entries",
        "sizeBytes",
        "bodyMode",
        "redacted",
        "truncated",
        "truncatedReason",
        "droppedEntries",
        "skippedEntries",
        "writerErrors",
        "httpsCoverage",
        "notTestedReason",
    }


def test_default_disabled_session_has_no_har_key():
    profiler = netprof.NetProfiler(adb_path="adb", serial="s1")

    assert "har" not in profiler.session.as_dict()


def test_proxy_log_remains_the_only_evidence_when_har_has_no_entries(tmp_path):
    profiler = _profiler(FakeDevice())
    profiler.start(netprof.build_profile("none"), work_dir=tmp_path)
    _write_log(
        tmp_path,
        [
            {"ev": "trust_probe", "ok": True, "host": "api.test"},
            {"ev": "http_request", "fid": "f1", "host": "api.test", "path": "/a"},
        ],
    )

    stopped = profiler.stop()

    assert stopped.effective is True
    assert stopped.har.status == netprof.STATUS_NOT_TESTED
    assert not (tmp_path / "app.har").exists()


def test_har_writer_degradation_events_are_reflected_in_summary(tmp_path):
    profiler = _profiler(FakeDevice())
    profiler.start(netprof.build_profile("none"), work_dir=tmp_path)
    _write_entries(tmp_path)
    _write_log(
        tmp_path,
        [
            {"ev": "trust_probe", "ok": True, "host": "api.test"},
            {"ev": "http_request", "fid": "f1", "host": "api.test", "path": "/a"},
            {"ev": "har_dropped", "reason": "max_entries"},
            {"ev": "har_error", "error": "disk full"},
        ],
    )

    stopped = profiler.stop()

    assert stopped.har.dropped_entries == 1
    assert stopped.har.truncated is True
    assert stopped.har.truncated_reason == "max_entries"
    assert stopped.har.writer_errors == 1


def test_har_init_failure_is_reported_as_failed_not_missing_traffic(tmp_path):
    profiler = _profiler(FakeDevice())
    profiler.start(netprof.build_profile("none"), work_dir=tmp_path)
    _write_entries(tmp_path)
    _write_log(
        tmp_path,
        [
            {"ev": "trust_probe", "ok": True, "host": "api.test"},
            {"ev": "http_request", "fid": "f1", "host": "api.test", "path": "/a"},
            {"ev": "har_error", "error": "init: writer unavailable"},
        ],
    )

    stopped = profiler.stop()

    assert stopped.har.status == netprof.STATUS_FAILED
    assert stopped.har.path is None
    assert "HAR 写入链路初始化失败" in stopped.har.not_tested_reason
    assert "writer unavailable" in stopped.har.not_tested_reason
    assert "可能走缓存" not in stopped.har.not_tested_reason


def test_capture_still_restores_device_in_reverse_order(tmp_path):
    device = FakeDevice()
    profiler = _profiler(device)
    profiler.start(netprof.build_profile("none"), work_dir=tmp_path)

    profiler.stop()

    commands = device.commands()
    assert commands[0].endswith("reverse tcp:8080 tcp:8080")
    assert commands[1].endswith("settings put global http_proxy 127.0.0.1:8080")
    assert commands[2].endswith("settings put global http_proxy :0")
    assert commands[3].endswith("reverse --remove tcp:8080")


def test_capture_requires_decrypt_tier_at_construction():
    with pytest.raises(ValueError, match="net-tier decrypt"):
        netprof.NetProfiler(
            adb_path="adb",
            serial="s1",
            tier=netprof.TIER_PASSTHROUGH,
            capture=CaptureOptions(enabled=True),
        )


# ---------- addon 子进程侧：桩掉 mitmproxy，按文件加载（覆盖动态加载回归） ----------


class _Headers:
    def __init__(self, pairs):
        self._pairs = list(pairs)

    def items(self, multi=False):
        return list(self._pairs)


def _install_fake_mitmproxy(monkeypatch):
    root = types.ModuleType("mitmproxy")
    proxy = types.ModuleType("mitmproxy.proxy")
    layers = types.ModuleType("mitmproxy.proxy.layers")
    layers.TCPLayer = object
    proxy.layers = layers
    root.proxy = proxy
    for name, module in {
        "mitmproxy": root,
        "mitmproxy.proxy": proxy,
        "mitmproxy.proxy.layers": layers,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)


def _load_addon(monkeypatch, tmp_path, *, tier="passthrough", max_entries=5000):
    _install_fake_mitmproxy(monkeypatch)
    entries = tmp_path / "app.har.entries.jsonl"
    log = tmp_path / "net_proxy.jsonl"
    config = {
        "entriesPath": str(entries),
        "captureBodies": False,
        "bodyLimitBytes": 65536,
        "maxEntries": max_entries,
        "maxBytes": 67108864,
        "redact": True,
        "maxHashBytes": 8 * 1024 * 1024,
    }
    monkeypatch.setenv("MOBILE_PERF_HAR", json.dumps(config, ensure_ascii=False))
    monkeypatch.setenv("MOBILE_PERF_HAR_ENTRIES", str(entries))
    monkeypatch.setenv("MOBILE_PERF_NETLOG", str(log))
    monkeypatch.setenv("MOBILE_PERF_NETTIER", tier)
    monkeypatch.setenv("MOBILE_PERF_NETPARAMS", "{}")

    path = Path(netprof.__file__).resolve().parent / "netproxy_addon.py"
    spec = importlib.util.spec_from_file_location("test_netproxy_addon_har", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module, entries, log


def _close_addon(module):
    if module._HAR_WRITER is not None:
        module._HAR_WRITER.close()
    sys.modules.pop(module.__name__, None)


def _flow(
    url="http://127.0.0.1:18080/probe?token=SECRET", *, method="GET", response=True
):
    request = SimpleNamespace(
        method=method,
        url=url,
        headers=_Headers(
            [("Host", "127.0.0.1"), ("Referer", "http://ref.test/?token=SECRET")]
        ),
        raw_content=b"",
        timestamp_start=100.0,
    )
    if response:
        resp = SimpleNamespace(
            status_code=200,
            headers=_Headers(
                [
                    ("Content-Type", "application/json"),
                    ("Set-Cookie", "sid=SECRET; Path=/"),
                ]
            ),
            raw_content=b'{"ok":true}',
            timestamp_end=100.25,
        )
    else:
        resp = None
    return SimpleNamespace(
        id="f1",
        request=request,
        response=resp,
        error=None if response else "connection reset",
    )


def test_addon_loads_har_writer_and_writes_plaintext_in_passthrough(
    tmp_path, monkeypatch
):
    """回归：动态模块必须注册 sys.modules；明文 HTTP 在非解密档也写条目。"""
    module, entries, log = _load_addon(monkeypatch, tmp_path, tier="passthrough")
    try:
        assert module._HAR_WRITER is not None
        module.response(_flow())
        module.error(_flow(response=False))
        # CONNECT 是隧道建立，即使误入 response() 也绝不写伪请求
        connect = SimpleNamespace(
            request=SimpleNamespace(method="CONNECT", host="api.test", port=443),
            client_conn=SimpleNamespace(id="c1"),
        )
        module.response(connect)
    finally:
        _close_addon(module)

    text = entries.read_text(encoding="utf-8")
    lines = [json.loads(line) for line in text.splitlines()]
    assert len(lines) == 2
    assert "SECRET" not in text
    assert lines[0]["request"]["queryString"][0]["value"] == "[REDACTED]"
    assert (
        lines[0]["request"]["headers"][1]["value"]
        == "http://ref.test/?token=[REDACTED]"
    )
    assert lines[1]["response"]["status"] == 0
    assert lines[1]["_error"]
    events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert "har_error" not in [event["ev"] for event in events]


def test_addon_drop_event_flood_is_throttled(tmp_path, monkeypatch):
    module, entries, log = _load_addon(monkeypatch, tmp_path, max_entries=1)
    try:
        assert module._HAR_WRITER is not None
        for index in range(4):
            module.response(_flow(f"http://127.0.0.1/{index}"))
    finally:
        _close_addon(module)

    events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    drops = [event for event in events if event["ev"] == "har_dropped"]
    assert len(drops) == 1
    assert drops[0]["reason"] == "max_entries"
    assert len(entries.read_text(encoding="utf-8").splitlines()) == 1


def test_init_error_is_reported_as_failed_not_zero_requests(tmp_path):
    profiler = _profiler(FakeDevice())
    profiler.start(netprof.build_profile("none"), work_dir=tmp_path)
    _write_log(
        tmp_path,
        [
            {
                "ev": "har_error",
                "error": "init: 'NoneType' object has no attribute '__dict__'",
            }
        ],
    )

    stopped = profiler.stop()

    assert stopped.har.status == netprof.STATUS_FAILED
    assert "初始化失败" in stopped.har.not_tested_reason
    assert "init:" in stopped.har.not_tested_reason
    assert stopped.har.path is None
    assert not (tmp_path / "app.har").exists()
