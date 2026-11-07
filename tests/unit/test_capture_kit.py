"""采集链路里的 APP 抓包：captureOnly、默认关闭零行为、事件与恢复。

自带最小替身，不 import 其它测试模块的私有符号；不真起代理。
"""

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mobile_perf import netprof
from mobile_perf.gate import FileLockGate
from mobile_perf.har_writer import CaptureOptions
from mobile_perf.keeper import CheckResult
from mobile_perf.kit import PerfKit
from mobile_perf.store import PerfStore


class FakeHarnessBackend:
    def __init__(self):
        self.started = []
        self.stopped = []

    def start(self, target, items):
        self.started.append((target, items))
        return "sid-fake-har"

    def stop(self, session_id, output_dir):
        self.stopped.append((session_id, output_dir))
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        rows = ["RecordTime,PSS-main-18390(MB),extra,SimpleTime"]
        rows += [f"{1792895595000 + i},{42.0 + i},,{i}" for i in range(3)]
        Path(output_dir).joinpath("performance.csv").write_text(
            "\n".join(rows) + "\n", encoding="gb18030"
        )
        return {
            "success": True,
            "state": "stopped",
            "sessionId": session_id,
            "artifacts": {"performance": {"localDir": str(output_dir)}},
        }


class FakeProcess:
    def __init__(self):
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def healthy_device_call():
    def call(args, timeout_s=15.0):
        if args == ["devices"]:
            stdout = "List of devices attached\n749622c9\tdevice\n"
        elif "accessibility" in args:
            stdout = (
                "ACCESSIBILITY MANAGER SERVICES (dumpsys accessibility)\n"
                "  Enabled services:\n"
                "    Service[com.alipay.hulu/com.alipay.hulu.shared.event."
                "accessibility.AccessibilityServiceImpl]\n"
                "  Bound services:\n"
                "    Service[{com.alipay.hulu/com.alipay.hulu.shared.event."
                "accessibility.AccessibilityServiceImpl}]\n"
            )
        elif "battery" in args:
            stdout = "AC powered: false\nUSB powered: false\nstatus: 4\n"
        elif "activity" in args:
            stdout = (
                "  mResumedActivity: ActivityRecord{1 u0 "
                "tv.danmaku.bili/.ui.MainActivity t42}\n"
            )
        elif "date" in args:
            stdout = str(int(time.time()))
        else:
            stdout = ""
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    return call


def make_kit(tmp_path):
    adb = tmp_path / "adb.exe"
    adb.write_bytes(b"x")
    base = healthy_device_call()
    calls = []

    def device_call(args, timeout_s=15.0):
        calls.append(list(args))
        return base(args, timeout_s)

    device_call.calls = calls
    return PerfKit(
        adb_path=str(adb),
        serial="749622c9",
        runs_dir=tmp_path / "runs",
        backend=FakeHarnessBackend(),
        clock=lambda: time.monotonic(),
        sleep=lambda _s: None,
        store=PerfStore(tmp_path / "data" / "mobile_perf.db"),
        gate=FileLockGate(tmp_path / "gate-leases"),
        device_call=device_call,
        doctor_fn=lambda: SimpleNamespace(
            success=True, data={"failedChecks": [], "missingPermissions": []}
        ),
        dialog_probe=lambda: CheckResult("dialog_probe", "skipped", {"reason": "stub"}),
    )


def _entry():
    return {
        "startedDateTime": "2026-11-07T00:00:00.000Z",
        "time": 1.0,
        "request": {"method": "GET", "url": "http://127.0.0.1:18080/a"},
        "response": {"status": 200},
        "timings": {"send": 0.0, "wait": 1.0, "receive": 0.0},
    }


def _patch_profiler(monkeypatch, proxy_lines, *, entries=1, profilers=None):
    real = netprof.NetProfiler

    def factory(*args, **kwargs):
        profiler = real(*args, **kwargs)
        profiler.popen_calls = []
        if profilers is not None:
            profilers.append(profiler)
        profiler._availability = netprof.ProxyAvailability(
            available=True, executable="/fake/mitmdump", version="12.3.0"
        )
        profiler._which = lambda _n: None
        profiler._sleep = lambda _s: None
        state = {"listening": False}
        profiler._in_use = lambda _p: state["listening"]
        original_start = profiler.start

        def start(profile, *, work_dir):
            work_dir = Path(work_dir)
            work_dir.mkdir(parents=True, exist_ok=True)
            profiler._log_path = str(work_dir / netprof.LOG_FILENAME)
            if entries:
                (work_dir / "app.har.entries.jsonl").write_text(
                    "".join(
                        json.dumps(_entry(), ensure_ascii=False) + "\n"
                        for _ in range(entries)
                    ),
                    encoding="utf-8",
                )

            def popen(_args, **kwargs):
                profiler.popen_calls.append((_args, kwargs))
                Path(profiler._log_path).write_text(
                    "".join(
                        json.dumps(line, ensure_ascii=False) + "\n" for line in proxy_lines
                    ),
                    encoding="utf-8",
                )
                state["listening"] = True
                return FakeProcess()

            profiler._popen = popen
            return original_start(profile, work_dir=work_dir)

        profiler.start = start
        return profiler

    monkeypatch.setattr(netprof, "NetProfiler", factory)


TRAFFIC = [
    {"t": 1.0, "ev": "trust_probe", "ok": True, "host": "api.test"},
    {"t": 1.1, "ev": "http_request", "fid": "f1", "host": "api.test", "path": "/a"},
]


def _collect(kit, tmp_path, run_id, **kwargs):
    return kit.run_collect(
        target="tv.danmaku.bili",
        items=["CPU"],
        duration_s=5.0,
        host_run_id=run_id,
        runs_dir=tmp_path / "runs",
        supplement=False,
        **kwargs,
    )


def test_capture_only_writes_har_but_keeps_net_profile_none(tmp_path, monkeypatch):
    _patch_profiler(monkeypatch, TRAFFIC)
    kit = make_kit(tmp_path)

    # 未显式传 net_tier：库 API 与 CLI 一致，抓包自动走 decrypt
    report = _collect(kit, tmp_path, "run-har-1", capture_har=True)

    assert report["network"]["tier"] == "decrypt"
    assert report["network"]["captureOnly"] is True
    assert report["network"]["profile"] is None
    assert report["network"]["har"]["status"] == "complete"
    assert report["network"]["har"]["entries"] == 1
    assert kit.store.fetch_session("run-har-1")["net_profile"] is None
    events = [e for e in report["events"] if e["kind"] == "capture_har"]
    assert events and events[0]["detail"]["status"] == "complete"
    html = (tmp_path / "runs" / "run-har-1" / "report.html").read_text(encoding="utf-8")
    assert "network/app.har" in html


def test_capture_with_explicit_passthrough_raises_early(tmp_path):
    kit = make_kit(tmp_path)

    with pytest.raises(ValueError, match="net_tier='decrypt'"):
        kit.collect(
            target="tv.danmaku.bili",
            items=["CPU"],
            capture_har=True,
            net_tier="passthrough",
        )


def test_capture_uses_explicit_net_profile_for_comparability(tmp_path, monkeypatch):
    _patch_profiler(monkeypatch, TRAFFIC)
    kit = make_kit(tmp_path)

    report = _collect(
        kit,
        tmp_path,
        "run-har-2",
        capture_har=True,
        net_tier="decrypt",
        net_profile="weak",
    )

    assert "captureOnly" not in report["network"]
    assert report["network"]["profile"]["name"] == "weak"
    row = kit.store.fetch_session("run-har-2")
    assert row["net_profile"]["name"] == "weak"


def test_capture_har_body_options_reach_proxy_environment(tmp_path, monkeypatch):
    profilers = []
    _patch_profiler(monkeypatch, TRAFFIC, profilers=profilers)
    kit = make_kit(tmp_path)

    _collect(
        kit,
        tmp_path,
        "run-har-3",
        capture_har=True,
        net_tier="decrypt",
        capture_options=CaptureOptions(
            enabled=True, capture_bodies=True, body_limit_bytes=777
        ),
    )

    assert profilers
    env = profilers[0].popen_calls[0][1]["env"]
    config = json.loads(env["MOBILE_PERF_HAR"])
    assert config["captureBodies"] is True
    assert config["bodyLimitBytes"] == 777
    assert config["bodyMode"] == "full"


def test_default_off_has_no_network_block_event_or_har_file(tmp_path):
    kit = make_kit(tmp_path)

    report = _collect(kit, tmp_path, "run-har-off")

    assert "network" not in report
    assert not [e for e in report["events"] if e["kind"] == "capture_har"]
    assert not list((tmp_path / "runs" / "run-har-off").rglob("app.har"))


def test_exception_in_scope_still_restores_device(tmp_path, monkeypatch):
    _patch_profiler(monkeypatch, TRAFFIC)
    kit = make_kit(tmp_path)

    with pytest.raises(RuntimeError, match="UI 失败"):
        with kit.collect(
            target="tv.danmaku.bili",
            items=["CPU"],
            host_run_id="run-har-4",
            runs_dir=tmp_path / "runs",
            supplement=False,
            capture_har=True,
            net_tier="decrypt",
        ):
            raise RuntimeError("UI 失败")

    commands = [" ".join(call) for call in kit._device_call.calls]
    assert any(call.endswith("settings put global http_proxy :0") for call in commands)
    assert any(call.endswith("reverse --remove tcp:8080") for call in commands)
    report = json.loads(
        (tmp_path / "runs" / "run-har-4" / "meta.json").read_text(encoding="utf-8")
    )
    assert report["network"]["restored"]["deviceProxy"] is True
    assert report["network"]["restored"]["reverse"] is True
