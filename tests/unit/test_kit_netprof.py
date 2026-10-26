"""网络剖面在采集链路里的接线（ticket 17）：kit.collect/run_collect 前后编排。

用 monkeypatch 替换 ``netprof.NetProfiler`` 的子进程与端口探测，不真起代理；
真实代理行为由 ``test_netprof.py`` 的集成用例覆盖。
"""

import json
from pathlib import Path

import pytest
from tests.unit.test_kit import FakeHarnessBackend, healthy_keeper_device_call, make_kit

from mobile_perf import netprof


class _FakeNetProcess:
    """代理子进程替身：把预置的代理侧日志写到指定路径。"""

    def __init__(self, log_path: Path, lines: list[dict]):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines),
            encoding="utf-8",
        )

    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def _device_call():
    """设备替身：巡检答案照旧，reverse/http_proxy 命令恒成功。"""
    from types import SimpleNamespace

    base = healthy_keeper_device_call()
    calls: list[list[str]] = []

    def call(args, timeout_s=15.0):
        calls.append(list(args))
        if "reverse" in args:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "http_proxy" in args:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return base(args, timeout_s)

    call.calls = calls
    return call


def _patch_profiler(monkeypatch, proxy_lines, *, available=True):
    """把 NetProfiler 的子进程/端口/探测换成替身（保留真实编排逻辑）。"""
    real = netprof.NetProfiler

    def factory(*args, **kwargs):
        profiler = real(*args, **kwargs)
        profiler._availability = netprof.ProxyAvailability(
            available=available,
            executable="/fake/mitmdump" if available else None,
            version="12.3.0" if available else None,
            reason=None if available else "mitmproxy 不可用",
            hint=None if available else "把 MOBILE_PERF_MITMDUMP 指向该 venv 的 mitmdump",
        )
        profiler._which = lambda _n: None
        profiler._sleep = lambda _s: None
        # 端口探测按状态回答：起进程前空闲、起之后"已在监听"（起代理的等待循环据此退出）
        state = {"listening": False}
        profiler._in_use = lambda _p: state["listening"]
        original_start = profiler.start

        def start(profile, *, work_dir):
            profiler._log_path = str(Path(work_dir) / netprof.LOG_FILENAME)

            def popen(_args, **_kwargs):
                state["listening"] = True
                return _FakeNetProcess(Path(profiler._log_path), proxy_lines)

            profiler._popen = popen
            return original_start(profile, work_dir=work_dir)

        profiler.start = start
        return profiler

    monkeypatch.setattr(netprof, "NetProfiler", factory)


TRAFFIC = [
    {"t": 1.0, "ev": "tcp_start", "fid": "f1", "host": "api.test", "port": 443},
    {"t": 1.6, "ev": "tcp_message", "fid": "f1", "from_client": True, "size": 200},
    {"t": 2.0, "ev": "tcp_start", "fid": "f2", "host": "api.test", "port": 443},
    {"t": 2.4, "ev": "tcp_message", "fid": "f2", "from_client": True, "size": 200},
]


def _collect(kit, tmp_path, run_id, **kwargs):
    return kit.run_collect(
        target="tv.danmaku.bili",
        items=["CPU"],
        duration_s=30.0,
        host_run_id=run_id,
        runs_dir=tmp_path / "runs",
        supplement=False,
        **kwargs,
    )


def test_collect_with_net_profile_ingests_conn_duration(tmp_path, monkeypatch):
    """weak 档且代理侧有连接 → net_conn_duration_ms 入库、报告含 network 段。"""
    _patch_profiler(monkeypatch, TRAFFIC)
    device = _device_call()
    kit = make_kit(tmp_path, FakeHarnessBackend(), device_call=device)

    report = _collect(kit, tmp_path, "run-net-1", net_profile="weak")

    summary = report["metricsSummary"]
    assert summary["net_conn_duration_ms"]["median"] == pytest.approx(500.0)  # 600/400 中位
    assert summary["net_conn_duration_ms"]["sampleCount"] == 2
    assert report["network"]["profile"]["name"] == "weak"
    assert report["network"]["profile"]["latencyMs"] == 400.0
    assert report["network"]["effective"] is True
    assert report["network"]["tier"] == "passthrough"
    assert report["network"]["proxy"]["connections"] == 2
    assert report["network"]["restored"]["deviceProxy"] is True
    events = [e for e in report["events"] if e["kind"] == "net_profile"]
    assert events and events[0]["detail"]["effective"] is True
    # 环境恢复确实下发到了设备
    commands = [" ".join(call) for call in device.calls]
    assert any(c.endswith("settings put global http_proxy :0") for c in commands)
    assert any(c.endswith("reverse --remove tcp:8080") for c in commands)


def test_collect_without_net_profile_has_no_network_keys(tmp_path):
    kit = make_kit(tmp_path, FakeHarnessBackend())

    report = _collect(kit, tmp_path, "run-net-2")

    assert "net_conn_duration_ms" not in report["metricsSummary"]
    assert "network" not in report


def test_collect_net_profile_zero_traffic_is_not_tested_not_zero(tmp_path, monkeypatch):
    """环境设好了但代理侧零连接 → not_tested + 原因，绝不产出 0 值指标。"""
    _patch_profiler(monkeypatch, [])
    kit = make_kit(tmp_path, FakeHarnessBackend(), device_call=_device_call())

    report = _collect(kit, tmp_path, "run-net-3", net_profile="weak")

    entry = report["metricsSummary"]["net_conn_duration_ms"]
    assert entry["notTested"] is True
    assert "代理侧日志未观察到任何连接" in entry["notTestedReason"]
    assert report["network"]["effective"] is False
    events = [e for e in report["events"] if e["kind"] == "net_profile_degraded"]
    assert events and events[0]["detail"]["evidence"] == "none"


def test_collect_net_profile_without_mitmproxy_degrades_and_still_collects(
    tmp_path, monkeypatch
):
    """mitmproxy 缺失：弱网指标 not_tested + 安装提示，其余采集照常完成。"""
    _patch_profiler(monkeypatch, [], available=False)
    kit = make_kit(tmp_path, FakeHarnessBackend())

    report = _collect(kit, tmp_path, "run-net-4", net_profile="weak")

    entry = report["metricsSummary"]["net_conn_duration_ms"]
    assert entry["notTested"] is True
    assert "MOBILE_PERF_MITMDUMP" in entry["hint"]
    assert report["metricsSummary"]["mem_pss_mb"]["median"] == 43.0  # 其余采集照常
    assert report["durationS"] == 30.0  # 采集闭环完整跑完


def test_collect_net_profile_none_without_mitmproxy_still_collects(tmp_path, monkeypatch):
    """none 档不整形：代理缺失也不阻断，且不算失败（如实记 note）。"""
    _patch_profiler(monkeypatch, [], available=False)
    kit = make_kit(tmp_path, FakeHarnessBackend())

    report = _collect(kit, tmp_path, "run-net-5", net_profile="none")

    assert report["network"]["applied"] is True
    assert report["network"]["effective"] is True
    assert "net_conn_duration_ms" in (report["network"].get("note") or "")


def test_collect_net_profile_restores_device_even_when_body_raises(tmp_path, monkeypatch):
    """采集窗口内抛错：设备代理与 reverse 仍必须被恢复（退出必恢复）。"""
    _patch_profiler(monkeypatch, TRAFFIC)
    device = _device_call()
    kit = make_kit(tmp_path, FakeHarnessBackend(), device_call=device)

    with pytest.raises(RuntimeError, match="UI 失败"):
        with kit.collect(
            target="tv.danmaku.bili",
            items=["CPU"],
            host_run_id="run-net-6",
            runs_dir=tmp_path / "runs",
            supplement=False,
            net_profile="weak",
        ):
            raise RuntimeError("UI 失败")

    commands = [" ".join(call) for call in device.calls]
    assert any(c.endswith("settings put global http_proxy :0") for c in commands)
    assert any(c.endswith("reverse --remove tcp:8080") for c in commands)


DECRYPT_TRAFFIC = [
    {"t": 1.0, "ev": "trust_probe", "ok": True, "host": "api.test"},
    {"t": 1.1, "ev": "http_request", "fid": "f1", "host": "api.test", "path": "/a"},
    {
        "t": 1.2,
        "ev": "api_request",
        "fid": "f1",
        "host": "api.test",
        "route": "GET /a",
        "status": 200,
        "durationMs": 180.0,
        "sizeBytes": 2048,
    },
    {
        "t": 1.3,
        "ev": "api_request",
        "fid": "f2",
        "host": "api.test",
        "route": "GET /a",
        "status": 500,
        "durationMs": 620.0,
        "sizeBytes": 0,
    },
]

REJECTED_TRAFFIC = [
    {
        "t": 1.0,
        "ev": "trust_probe",
        "ok": False,
        "error": "tlsv1 alert unknown ca",
        "host": "api.test",
    },
    {"t": 1.5, "ev": "tcp_start", "fid": "f9", "host": "api.test", "port": 443},
    {"t": 1.9, "ev": "tcp_message", "fid": "f9", "from_client": True, "size": 64},
]


def test_collect_decrypt_tier_ingests_api_metrics_and_breakdown(tmp_path, monkeypatch):
    """信任探测成功 → 4 个 api_* 进 metricsSummary、报告分栏、事件记 available。"""
    _patch_profiler(monkeypatch, DECRYPT_TRAFFIC)
    kit = make_kit(tmp_path, FakeHarnessBackend(), device_call=_device_call())

    report = _collect(kit, tmp_path, "run-api-1", net_profile="weak", net_tier="decrypt")

    summary = report["metricsSummary"]
    assert summary["api_req_duration_ms"]["median"] == pytest.approx(400.0)  # 180/620 中位
    assert summary["api_req_duration_ms"]["sampleCount"] == 2
    assert summary["api_req_count"]["median"] == pytest.approx(2.0)
    assert summary["api_error_rate_pct"]["median"] == pytest.approx(50.0)
    assert summary["api_payload_kb"]["median"] == pytest.approx(1.0)
    assert report["network"]["tier"] == "decrypt"
    assert report["network"]["trust"] == "trusted"
    assert report["network"]["apiEffective"] is True
    assert report["network"]["apiSamples"] == 2
    # 接口级明细单独一栏，与连接级分栏呈现
    assert report["api"]["source"] == "mitmproxy_decrypt"
    assert report["api"]["top"][0]["route"] == "GET /a"
    assert report["api"]["top"][0]["count"] == 2
    events = [e for e in report["events"] if e["kind"] == "net_profile_decrypt_available"]
    assert events and events[0]["detail"]["trust"] == "trusted"


def test_collect_decrypt_tier_rejected_marks_api_not_tested_with_next_step(
    tmp_path, monkeypatch
):
    """证书不信任：4 个 api_* 全 not_tested + 可执行下一步，连接级指标照旧有值。"""
    _patch_profiler(monkeypatch, REJECTED_TRAFFIC)
    kit = make_kit(tmp_path, FakeHarnessBackend(), device_call=_device_call())

    report = _collect(kit, tmp_path, "run-api-2", net_profile="weak", net_tier="decrypt")

    summary = report["metricsSummary"]
    for key in ("api_req_duration_ms", "api_req_count", "api_error_rate_pct", "api_payload_kb"):
        assert summary[key]["notTested"] is True
        assert "不信任代理 CA" in summary[key]["notTestedReason"]
    assert "root" in summary["api_req_duration_ms"]["hint"]
    assert "debug" in summary["api_req_duration_ms"]["hint"]
    assert "api" not in report  # 报告不渲染接口瀑布图
    # 连接级指标不受影响：降为直通后隧道仍被观测到
    assert summary["net_conn_duration_ms"]["median"] == pytest.approx(400.0)
    events = [e for e in report["events"] if e["kind"] == "net_profile_decrypt_unavailable"]
    assert events and events[0]["detail"]["trust"] == "rejected"
    assert "unknown ca" in events[0]["detail"]["trustError"]


def test_collect_passthrough_tier_marks_api_not_tested_without_hint(
    tmp_path, monkeypatch
):
    """直通档默认标 not_tested，原因指向 --net-tier decrypt，不给证书安装提示。"""
    _patch_profiler(monkeypatch, TRAFFIC)
    kit = make_kit(tmp_path, FakeHarnessBackend(), device_call=_device_call())

    report = _collect(kit, tmp_path, "run-api-3", net_profile="weak")

    entry = report["metricsSummary"]["api_req_duration_ms"]
    assert entry["notTested"] is True
    assert "--net-tier decrypt" in entry["notTestedReason"]
    assert "hint" not in entry  # 直通档不是信任问题，不误导去装 CA
    assert "api" not in report
    assert not [e for e in report["events"] if e["kind"].startswith("net_profile_decrypt")]


def test_api_metrics_do_not_pollute_solopi_network_metrics(tmp_path, monkeypatch):
    """两套口径独立入库：api_* 不改变 SoloPi 端侧网络指标，也不混算连接耗时。"""
    _patch_profiler(monkeypatch, DECRYPT_TRAFFIC)
    kit = make_kit(tmp_path, FakeHarnessBackend(), device_call=_device_call())

    report = _collect(kit, tmp_path, "run-api-4", net_profile="weak", net_tier="decrypt")

    summary = report["metricsSummary"]
    # 解密档没有 tcp_start，连接级耗时不得由接口级明细伪造出来
    assert "net_conn_duration_ms" not in summary
    # SoloPi 端侧网络指标来自 CSV，与本 run 的接口级明细无关
    assert not any(key.startswith("net_rx") or key.startswith("net_tx") for key in summary)


def test_collect_rejects_unknown_net_profile_early(tmp_path):
    """档位非法在 collect 前就失败——不带着错误网络条件去跑采集。"""
    kit = make_kit(tmp_path, FakeHarnessBackend())

    with pytest.raises(ValueError, match="未知网络档位"):
        kit.collect(target="tv.danmaku.bili", items=["CPU"], net_profile="2g")


def test_collect_accepts_prebuilt_profile_with_overrides(tmp_path, monkeypatch):
    """宿主可直接传 NetProfile（含覆盖参数），报告如实记录实际参数。"""
    _patch_profiler(monkeypatch, TRAFFIC)
    kit = make_kit(tmp_path, FakeHarnessBackend(), device_call=_device_call())
    profile = netprof.build_profile("3g", {"latency_ms": 500})

    report = _collect(kit, tmp_path, "run-net-7", net_profile=profile)

    assert report["network"]["profile"]["name"] == "3g"
    assert report["network"]["profile"]["latencyMs"] == 500.0
    assert report["network"]["profile"]["downKbps"] == 780.0


def test_collect_records_net_profile_into_sessions(tmp_path, monkeypatch):
    """工单 20：本 run 的档位（名+参数）入 sessions 表，作为趋势可比性依据。"""
    _patch_profiler(monkeypatch, TRAFFIC)
    kit = make_kit(tmp_path, FakeHarnessBackend(), device_call=_device_call())

    report = _collect(kit, tmp_path, "run-net-tier-1", net_profile="weak")

    row = kit.store.fetch_session(report["runId"])
    assert row["net_profile"]["name"] == "weak"
    assert row["net_profile"]["latencyMs"] == 400.0


def test_collect_without_net_profile_records_no_tier(tmp_path):
    kit = make_kit(tmp_path, FakeHarnessBackend())

    report = _collect(kit, tmp_path, "run-net-tier-2")

    assert kit.store.fetch_session(report["runId"])["net_profile"] is None
