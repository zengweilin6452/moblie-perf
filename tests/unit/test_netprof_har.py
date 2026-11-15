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


def _profiler(device, *, available=True, port_busy=False, env=None, capture=None):
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
        env=env,
        capture=CaptureOptions(enabled=True) if capture is None else capture,
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
        "droppedEvents",
        "countersSource",
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
    """旧格式日志（无累计字段）按事件条数推算，来源如实标 proxy_log。"""
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
    assert stopped.har.dropped_events == 1
    assert stopped.har.counters_source == "proxy_log"
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


def _load_addon(
    monkeypatch, tmp_path, *, tier="passthrough", max_entries=5000, har=True,
    entries_path=None,
):
    _install_fake_mitmproxy(monkeypatch)
    entries = Path(entries_path) if entries_path else tmp_path / "app.har.entries.jsonl"
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
    if har:
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
    assert drops[0]["dropped"] == 1
    assert len(entries.read_text(encoding="utf-8").splitlines()) == 1


def test_har_drop_stats_use_cumulative_count_when_present(tmp_path):
    _write_log(
        tmp_path,
        [
            {"ev": "har_dropped", "reason": "max_entries", "dropped": 1},
            {"ev": "har_dropped", "reason": "max_entries", "dropped": 100},
        ],
    )

    stats = netprof.read_har_log_stats(tmp_path / netprof.LOG_FILENAME)

    assert stats["dropped"] == 100
    assert stats["reason"] == "max_entries"


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


# ---- P0-02：HAR 环境变量隔离（父进程残留不得泄漏给子进程） ----

_RESIDUAL_ENV = {
    "PATH": "/usr/bin",
    "LANG": "zh_CN.UTF-8",
    "MOBILE_PERF_HAR": json.dumps({"entriesPath": "D:/other-run/app.har.entries.jsonl"}),
    "MOBILE_PERF_HAR_ENTRIES": "D:/other-run/app.har.entries.jsonl",
}


def test_disabled_capture_strips_residual_har_env(tmp_path):
    """关闭抓包时，外层传入的残留 HAR 变量不得传给 mitmproxy 子进程。"""
    profiler = _profiler(
        FakeDevice(), env=dict(_RESIDUAL_ENV), capture=CaptureOptions(enabled=False)
    )

    profiler.start(netprof.build_profile("none"), work_dir=tmp_path)

    _args, kwargs = profiler.popen_calls[0]
    env = kwargs["env"]
    assert "MOBILE_PERF_HAR" not in env
    assert "MOBILE_PERF_HAR_ENTRIES" not in env
    # 用户无关环境变量原样保留
    assert env["PATH"] == "/usr/bin"
    assert env["LANG"] == "zh_CN.UTF-8"


def test_disabled_capture_strips_residual_har_env_from_os_environ(monkeypatch, tmp_path):
    """默认构造（env=None 继承 os.environ）同样要先清除残留。"""
    monkeypatch.setenv(
        "MOBILE_PERF_HAR", json.dumps({"entriesPath": "D:/other-run/e.jsonl"})
    )
    monkeypatch.setenv("MOBILE_PERF_HAR_ENTRIES", "D:/other-run/e.jsonl")
    profiler = _profiler(FakeDevice(), capture=CaptureOptions(enabled=False))

    profiler.start(netprof.build_profile("none"), work_dir=tmp_path)

    _args, kwargs = profiler.popen_calls[0]
    assert "MOBILE_PERF_HAR" not in kwargs["env"]
    assert "MOBILE_PERF_HAR_ENTRIES" not in kwargs["env"]


def test_enabled_capture_overrides_residual_har_env(tmp_path):
    """开启抓包时残留值被当前 run 的配置覆盖，两个变量一致且指向本 run。"""
    profiler = _profiler(FakeDevice(), env=dict(_RESIDUAL_ENV))

    profiler.start(netprof.build_profile("none"), work_dir=tmp_path)

    _args, kwargs = profiler.popen_calls[0]
    env = kwargs["env"]
    config = json.loads(env["MOBILE_PERF_HAR"])
    assert config["entriesPath"] == env["MOBILE_PERF_HAR_ENTRIES"]
    assert "other-run" not in env["MOBILE_PERF_HAR_ENTRIES"]
    assert env["MOBILE_PERF_HAR_ENTRIES"] == str(tmp_path / "app.har.entries.jsonl")


def test_managed_net_env_always_from_current_run(tmp_path):
    """NETPARAMS/NETTIER/NETLOG 由当前 run 无条件覆盖，残留值不生效。"""
    profiler = _profiler(
        FakeDevice(),
        env={
            **_RESIDUAL_ENV,
            "MOBILE_PERF_NETTIER": "bogus-tier",
            "MOBILE_PERF_NETLOG": "D:/other-run/old.jsonl",
        },
        capture=CaptureOptions(enabled=False),
    )

    profiler.start(netprof.build_profile("3g"), work_dir=tmp_path)

    _args, kwargs = profiler.popen_calls[0]
    env = kwargs["env"]
    assert env["MOBILE_PERF_NETTIER"] == profiler.tier  # 当前 run 档位，非残留值
    assert env["MOBILE_PERF_NETLOG"] == str(tmp_path / netprof.LOG_FILENAME)
    params = json.loads(env["MOBILE_PERF_NETPARAMS"])
    assert params and all(v is not None for v in params.values())


# ---- P0-03/P0-04：节流日志 + writer 状态文件 → 精确计数恢复 ----

def test_compose_recovers_exact_drops_from_writer_state(tmp_path):
    """maxEntries=2 实际写入 25 条：状态文件恢复精确丢弃 23，日志仅 1 条节流事件。"""
    profiler = _profiler(FakeDevice())
    profiler.start(netprof.build_profile("none"), work_dir=tmp_path)
    _write_entries(tmp_path, count=2)
    (tmp_path / "app.har.entries.jsonl.state.json").write_text(
        json.dumps(
            {"droppedEntries": 23, "writerErrors": 0, "truncatedReason": "max_entries"}
        ),
        encoding="utf-8",
    )
    _write_log(
        tmp_path,
        [
            {"ev": "trust_probe", "ok": True, "host": "api.test"},
            {"ev": "http_request", "fid": "f1", "host": "api.test", "path": "/a"},
            {"ev": "har_dropped", "reason": "max_entries", "dropped": 1, "truncated": True},
        ],
    )

    stopped = profiler.stop()

    assert stopped.har.dropped_entries == 23  # 不是节流事件推出的 1
    assert stopped.har.dropped_events == 1
    assert stopped.har.counters_source == "writer_state"
    assert stopped.har.truncated is True
    assert stopped.har.truncated_reason == "max_entries"
    har = json.loads((tmp_path / "app.har").read_text(encoding="utf-8"))
    assert len(har["log"]["entries"]) == 2


def test_old_format_drop_events_fall_back_to_event_count(tmp_path):
    """旧格式（无累计字段）按事件条数推算，countersSource 如实标 proxy_log。"""
    profiler = _profiler(FakeDevice())
    profiler.start(netprof.build_profile("none"), work_dir=tmp_path)
    _write_entries(tmp_path)
    _write_log(
        tmp_path,
        [
            {"ev": "trust_probe", "ok": True, "host": "api.test"},
            {"ev": "http_request", "fid": "f1", "host": "api.test", "path": "/a"},
            {"ev": "har_dropped", "reason": "max_bytes"},
            {"ev": "har_dropped", "reason": "max_bytes"},
        ],
    )

    stopped = profiler.stop()

    assert stopped.har.dropped_entries == 2
    assert stopped.har.dropped_events == 2
    assert stopped.har.counters_source == "proxy_log"
    assert stopped.har.truncated_reason == "max_bytes"


def test_cumulative_error_events_recover_writer_errors(tmp_path):
    """har_error 带 errors 累计字段时按累计恢复；无状态文件时来源标 proxy_log。"""
    profiler = _profiler(FakeDevice())
    profiler.start(netprof.build_profile("none"), work_dir=tmp_path)
    _write_entries(tmp_path)
    _write_log(
        tmp_path,
        [
            {"ev": "trust_probe", "ok": True, "host": "api.test"},
            {"ev": "http_request", "fid": "f1", "host": "api.test", "path": "/a"},
            {"ev": "har_error", "error": "disk full", "errors": 1},
            {"ev": "har_error", "error": "disk full", "errors": 100},
        ],
    )

    stopped = profiler.stop()

    assert stopped.har.writer_errors == 100
    assert stopped.har.counters_source == "proxy_log"
    assert stopped.har.status == netprof.STATUS_COMPLETE  # 运行时失败不是 init 失败


def test_state_file_overrides_cumulative_and_keeps_log_reason(tmp_path):
    """状态文件计数与日志累计并存时取大者；reason 缺失时回退状态文件里的值。"""
    profiler = _profiler(FakeDevice())
    profiler.start(netprof.build_profile("none"), work_dir=tmp_path)
    _write_entries(tmp_path, count=2)
    (tmp_path / "app.har.entries.jsonl.state.json").write_text(
        json.dumps({"droppedEntries": 5, "writerErrors": 2,
                    "truncatedReason": "max_bytes"}),
        encoding="utf-8",
    )
    _write_log(
        tmp_path,
        [
            {"ev": "trust_probe", "ok": True, "host": "api.test"},
            {"ev": "http_request", "fid": "f1", "host": "api.test", "path": "/a"},
            {"ev": "har_dropped", "reason": "max_entries", "dropped": 100},
        ],
    )

    stopped = profiler.stop()

    assert stopped.har.dropped_entries == 100  # max(状态 5, 日志累计 100)
    assert stopped.har.writer_errors == 2
    assert stopped.har.truncated_reason == "max_entries"  # 日志 reason 优先
    assert stopped.har.counters_source == "writer_state"


def test_addon_persists_exact_drop_count_to_state_file(tmp_path, monkeypatch):
    """addon 状态文件承载节流窗口内的精确丢弃数：maxEntries=2 写 25 条 → 23。"""
    module, entries, log = _load_addon(monkeypatch, tmp_path, max_entries=2)
    try:
        for index in range(25):
            module.response(_flow(f"http://127.0.0.1/{index}"))
    finally:
        _close_addon(module)

    state = json.loads(
        (tmp_path / "app.har.entries.jsonl.state.json").read_text(encoding="utf-8")
    )
    assert state["droppedEntries"] == 23
    assert state["truncatedReason"] == "max_entries"
    assert len(entries.read_text(encoding="utf-8").splitlines()) == 2
    events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    drops = [event for event in events if event["ev"] == "har_dropped"]
    assert len(drops) == 1 and drops[0]["dropped"] == 1  # 日志节流仍在


def test_addon_writer_failure_flood_is_throttled(tmp_path, monkeypatch):
    """writer 持续失败 120 次：日志仅 2 条 har_error（首条+第100条），状态记满 120。"""
    module, entries, log = _load_addon(monkeypatch, tmp_path)
    try:
        assert module._HAR_WRITER is not None
        module._HAR_WRITER._handle.close()  # 模拟句柄失效（磁盘满/管道断）
        for index in range(120):
            module.response(_flow(f"http://127.0.0.1/{index}"))
    finally:
        _close_addon(module)

    events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    errors = [event for event in events if event["ev"] == "har_error"]
    assert len(errors) == 2  # 有明确上限：1 + total//100
    assert errors[0]["errors"] == 1
    assert errors[1]["errors"] == 100
    state = json.loads(
        (tmp_path / "app.har.entries.jsonl.state.json").read_text(encoding="utf-8")
    )
    assert state["writerErrors"] == 120
    assert len(entries.read_text(encoding="utf-8").splitlines()) == 0


# ---- P1-01：addon 关键路径隔离回归（不依赖真机/公网） ----

def test_addon_without_har_config_creates_no_writer(tmp_path, monkeypatch):
    """HAR 关闭（无 MOBILE_PERF_HAR）时不创建 writer，转发路径与 params 事件正常。"""
    module, entries, log = _load_addon(monkeypatch, tmp_path, har=False)
    try:
        assert module._HAR_WRITER is None
        module.load(None)  # mitmproxy 启动时调用：params 事件记录 harEnabled=False
        module.response(_flow())  # 抓包关闭不影响代理事件流
        module.error(_flow(response=False))
    finally:
        _close_addon(module)

    assert not entries.exists()
    events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    kinds = [event["ev"] for event in events]
    assert "har_error" not in kinds and "har_dropped" not in kinds
    assert kinds[0] == "params" and events[0]["harEnabled"] is False


def test_addon_har_init_failure_emits_init_error_and_keeps_forwarding(
    tmp_path, monkeypatch
):
    """writer 初始化失败：init: har_error 落日志，代理事件流不受影响。"""
    # entriesPath 指向已存在的目录 → HarWriter open("a") 失败，走 _init_har 异常分支
    module, _entries, log = _load_addon(monkeypatch, tmp_path, entries_path=tmp_path)
    try:
        assert module._HAR_WRITER is None
        module.error(_flow(response=False))  # 初始化失败绝不阻断转发事件流
    finally:
        _close_addon(module)

    events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    init_errors = [
        event for event in events
        if event["ev"] == "har_error" and str(event.get("error", "")).startswith("init:")
    ]
    assert len(init_errors) == 1
    kinds = [event["ev"] for event in events]
    assert kinds.count("error") == 1  # error 钩子的事件照常发出


# ---- 复核缺口回归：HAR 合成延迟到代理确认终止 ----

class _FlakyStopProcess:
    """terminate 首次失败、之后成功的进程替身（模拟句柄占用/进程僵死）。"""

    def __init__(self):
        self.terminate_calls = 0

    def poll(self):
        return None

    def terminate(self):
        self.terminate_calls += 1
        if self.terminate_calls == 1:
            raise OSError("terminate failed")

    def kill(self):
        raise OSError("kill failed")

    def wait(self, timeout=None):
        return 0


def test_har_compose_deferred_until_proxy_confirmed_stopped(tmp_path):
    """复核缺口回归：终止失败时不合成 HAR，确认终止后合成包含全部条目。

    修复前实测：首次 stop 终止失败仍立即合成并置 _har_composed=True，进程
    继续写入的条目被永久漏掉（JSONL 2 条 → 最终 HAR 仅 1 条，数据丢失）。
    """
    profiler = _profiler(FakeDevice())
    profiler.start(netprof.build_profile("none"), work_dir=tmp_path)
    profiler._process = _FlakyStopProcess()  # 换成首次终止失败的进程替身
    _write_entries(tmp_path, count=1)  # 进程存活期间写入 1 条

    first = profiler.stop()

    assert first.har is not None
    assert first.har.status == netprof.STATUS_NOT_TESTED  # 尚未合成，保持占位
    assert not (tmp_path / "app.har").exists()  # 不产生截断快照
    assert profiler._har_composed is False
    assert profiler._process is not None  # 进程引用保留（可重试）

    _write_entries(tmp_path, count=2)  # 进程"继续运行"又写入 1 条

    second = profiler.stop()  # 第二次终止成功

    assert profiler._har_composed is True
    assert second.har.entries == 2
    har = json.loads((tmp_path / "app.har").read_text(encoding="utf-8"))
    assert len(har["log"]["entries"]) == 2  # 全量条目，无遗漏
    assert profiler._process is None  # 终止确认后引用才释放
