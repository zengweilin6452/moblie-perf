"""网络剖面编排（ticket 17）：档位纯函数、代理侧证据判定、生命周期恢复与 doctor。

三条实测约束的回归保护：
① 档位校验宁可早失败，不静默用错误参数跑采集；
② 代理是否生效**只看代理侧日志**——不能拿"页面能否打开"当证据；
③ 异常路径后设备代理与 reverse 必被恢复，doctor 能发现并复验残留。
"""

import json
import os
import socket
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from mobile_perf import netprof

# ---------- 档位纯函数 ----------


def test_build_profile_returns_preset_parameters():
    profile = netprof.build_profile("weak")

    assert profile.name == "weak"
    assert profile.latency_ms == 400.0
    assert profile.down_kbps == 400.0
    assert profile.up_kbps == 200.0
    assert profile.loss_pct == 2.0
    assert profile.is_passthrough is False


def test_build_profile_none_is_passthrough():
    assert netprof.build_profile("none").is_passthrough is True


def test_build_profile_preset_override_only_changes_given_keys():
    profile = netprof.build_profile("3g", {"latency_ms": 500})

    assert profile.latency_ms == 500.0
    assert profile.down_kbps == 780.0  # 其余保持预设


def test_build_profile_custom_requires_all_parameters():
    with pytest.raises(ValueError, match="缺少参数.*down_kbps"):
        netprof.build_profile("custom", {"latency_ms": 100, "up_kbps": 100, "loss_pct": 0})


def test_build_profile_custom_accepts_full_parameters():
    profile = netprof.build_profile(
        "custom", {"latency_ms": 120, "down_kbps": 900, "up_kbps": 300, "loss_pct": 5}
    )

    assert profile.as_dict() == {
        "name": "custom",
        "latencyMs": 120.0,
        "downKbps": 900.0,
        "upKbps": 300.0,
        "lossPct": 5.0,
    }


def test_build_profile_rejects_unknown_preset():
    with pytest.raises(ValueError, match="未知网络档位"):
        netprof.build_profile("2g")


def test_build_profile_rejects_unknown_parameter():
    with pytest.raises(ValueError, match="未知网络参数"):
        netprof.build_profile("4g", {"jitter_ms": 10})


def test_build_profile_rejects_negative_and_oversized_loss():
    with pytest.raises(ValueError, match="不能为负"):
        netprof.build_profile("4g", {"latency_ms": -1})
    with pytest.raises(ValueError, match="丢包率不能超过 100%"):
        netprof.build_profile("custom", {"latency_ms": 1, "down_kbps": 1, "up_kbps": 1,
                                         "loss_pct": 150})


def test_profiles_are_comparable_requires_identical_parameters():
    weak = netprof.build_profile("weak").as_dict()
    weak_override = netprof.build_profile("weak", {"latency_ms": 401}).as_dict()

    assert netprof.profiles_are_comparable(weak, dict(weak)) is True
    assert netprof.profiles_are_comparable(weak, weak_override) is False
    # 未记录档位的历史 run 与任何档位都不可比（与设备档案同构语义）
    assert netprof.profiles_are_comparable(None, weak) is False
    assert netprof.profiles_are_comparable(None, None) is True


# ---------- mitmproxy 探测 ----------


def test_find_mitmproxy_reports_missing_with_actionable_hint():
    result = netprof.find_mitmproxy(candidates=[], which=lambda _n: None, probe=lambda _p: None,
                                    env={})

    assert result.available is False
    assert "3.12" in result.reason
    assert "MOBILE_PERF_MITMDUMP" in result.hint


def test_find_mitmproxy_prefers_explicit_candidate_over_env_and_path():
    probed = []

    def probe(path):
        probed.append(path)
        return "12.3.0"

    result = netprof.find_mitmproxy(
        candidates=["/explicit/mitmdump"],
        which=lambda _n: "/path/mitmdump",
        probe=probe,
        env={"MOBILE_PERF_MITMDUMP": "/env/mitmdump"},
    )

    assert result.available is True
    assert result.executable == "/explicit/mitmdump"
    assert probed == ["/explicit/mitmdump"]  # 命中即停，不继续探测


def test_find_mitmproxy_falls_back_to_env_when_explicit_fails():
    versions = {"/env/mitmdump": "12.3.0"}

    result = netprof.find_mitmproxy(
        candidates=["/broken/mitmdump"],
        which=lambda _n: None,
        probe=lambda path: versions.get(path),
        env={"MOBILE_PERF_MITMDUMP": "/env/mitmdump"},
    )

    assert result.executable == "/env/mitmdump"


# ---------- 代理侧日志：生效的唯一证据 ----------


def _lines(*records) -> list[str]:
    return [json.dumps(record) if isinstance(record, dict) else record for record in records]


def test_analyze_log_counts_connections_and_first_byte_duration():
    lines = _lines(
        {"t": 100.0, "ev": "http_connect", "host": "api.example.com", "port": 443},
        {"t": 100.5, "ev": "tcp_start", "fid": "f1", "host": "api.example.com", "port": 443},
        {"t": 101.0, "ev": "tcp_message", "fid": "f1", "from_client": True, "size": 300},
        {"t": 101.2, "ev": "tcp_message", "fid": "f1", "from_client": True, "size": 90},
        {"t": 102.0, "ev": "tcp_message", "fid": "f1", "from_client": False, "size": 5000},
        {"t": 103.0, "ev": "tcp_end", "fid": "f1"},
    )

    summary = netprof.analyze_proxy_log(lines)

    assert summary.connections == 1
    assert summary.conn_durations_ms == [pytest.approx(500.0)]  # 100.5 → 101.0
    assert summary.hosts == ["api.example.com"]  # CONNECT 与 tcp_start 同 host 只留一次
    assert summary.connect_requests == 1
    assert summary.bytes_up == 390
    assert summary.bytes_down == 5000
    assert summary.evidence == netprof.EVIDENCE_OBSERVED


def test_analyze_log_without_connections_is_not_evidence():
    """代理起来了但日志里没有连接 → 没有证据，不能算生效。"""
    summary = netprof.analyze_proxy_log(_lines({"t": 1.0, "ev": "params", "latencyMs": 400}))

    assert summary.connections == 0
    assert summary.evidence == netprof.EVIDENCE_NONE
    assert summary.params == {"latencyMs": 400}


def test_analyze_log_skips_malformed_lines_and_counts_errors():
    lines = _lines(
        "not json at all",
        "",
        {"t": 1.0, "ev": "tcp_start", "fid": "f1", "host": "a.com", "port": 443},
        {"t": 2.0, "ev": "tcp_error", "fid": "f1", "error": "connection reset"},
        {"t": 3.0, "ev": "server_connect_error", "cid": "c1", "error": "refused"},
    )

    summary = netprof.analyze_proxy_log(lines)

    assert summary.connections == 1
    assert summary.errors == 2
    assert summary.conn_durations_ms == []  # 无首字节的流不计耗时


def test_analyze_log_counts_connection_without_bytes_as_connection_only():
    summary = netprof.analyze_proxy_log(
        _lines({"t": 1.0, "ev": "tcp_start", "fid": "f9", "host": "a.com", "port": 443})
    )

    assert summary.connections == 1
    assert summary.conn_durations_ms == []


def test_read_proxy_log_missing_file_returns_empty_summary(tmp_path):
    summary = netprof.read_proxy_log(tmp_path / "absent.jsonl")

    assert summary.connections == 0
    assert summary.evidence == netprof.EVIDENCE_NONE


def test_conn_duration_rows_aggregates_and_skips_empty():
    summary = netprof.analyze_proxy_log(
        _lines(
            {"t": 0.0, "ev": "tcp_start", "fid": "a", "host": "h", "port": 443},
            {"t": 0.1, "ev": "tcp_message", "fid": "a", "from_client": True, "size": 1},
            {"t": 1.0, "ev": "tcp_start", "fid": "b", "host": "h", "port": 443},
            {"t": 1.5, "ev": "tcp_message", "fid": "b", "from_client": True, "size": 1},
        )
    )

    rows = netprof.conn_duration_rows(summary)

    assert [row["metric_key"] for row in rows] == ["net_conn_duration_ms"]
    assert rows[0]["median"] == pytest.approx(300.0)  # 100ms 与 500ms 的中位
    assert rows[0]["sample_count"] == 2
    assert rows[0]["source"] == "pc_supplement"
    assert netprof.conn_duration_rows(netprof.ProxyLogSummary()) == []


# ---------- 生命周期 ----------


class FakeDevice:
    """记录 adb 调用；按需让某些命令失败。stdout_map 按子串匹配命令。"""

    def __init__(self, failing=(), stdout_map=None):
        self.calls: list[list[str]] = []
        self.failing = set(failing)
        self.stdout_map = stdout_map or {}

    def __call__(self, args, timeout_s=15.0):
        self.calls.append(list(args))
        joined = " ".join(args)
        code = 1 if any(token in joined for token in self.failing) else 0
        stdout = ""
        for needle, value in self.stdout_map.items():
            if needle in joined:
                stdout = value
                break
        return SimpleNamespace(stdout=stdout, returncode=code, stderr="")

    def commands(self) -> list[str]:
        return [" ".join(call) for call in self.calls]


class FakeProcess:
    def __init__(self):
        self.terminated = False
        self.killed = False

    def poll(self):
        return None  # 一直活着

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


def _profiler(device, *, available=True, port_busy=False, process=None, tier=None):
    """构造一个不真起进程的 profiler：端口探测按 port_busy 回答。"""
    state = {"busy": port_busy}
    popen_calls = []

    def popen(args, **kwargs):
        popen_calls.append((args, kwargs))
        state["busy"] = True  # 起进程后端口变忙
        return process or FakeProcess()

    extra = {"tier": tier} if tier is not None else {}
    profiler = netprof.NetProfiler(
        adb_path="adb",
        serial="s1",
        mitmdump="/fake/mitmdump",
        call_device=device,
        popen=popen,
        sleep=lambda _s: None,
        now=lambda: 0.0,
        probe=lambda _p: "12.3.0" if available else None,
        which=lambda _n: None,
        in_use=lambda _port: state["busy"],
        **extra,
    )
    profiler.popen_calls = popen_calls
    return profiler


def test_start_sets_reverse_then_proxy_and_stop_restores_in_reverse_order(tmp_path):
    device = FakeDevice()
    profiler = _profiler(device)

    session = profiler.start(netprof.build_profile("weak"), work_dir=tmp_path)
    assert session.applied is True
    commands = device.commands()
    assert commands[0].endswith("reverse tcp:8080 tcp:8080")
    assert commands[1].endswith("settings put global http_proxy 127.0.0.1:8080")

    stopped = profiler.stop()

    # 恢复顺序与设置相反：先清系统代理，再拆 reverse，最后停进程
    assert device.commands()[2].endswith("settings put global http_proxy :0")
    assert device.commands()[3].endswith("reverse --remove tcp:8080")
    assert stopped.restored["deviceProxy"] is True
    assert stopped.restored["reverse"] is True
    assert stopped.restored["proxyProcess"] is True
    assert stopped.proxy_set is False and stopped.reverse_set is False


def test_start_none_profile_still_goes_through_proxy_for_comparability(tmp_path):
    """对照组也走代理：两档共用一条测量路径，net_conn_duration_ms 才可比。"""
    device = FakeDevice()
    profiler = _profiler(device)

    profiler.start(netprof.build_profile("none"), work_dir=tmp_path)
    session = profiler.stop()

    assert session.applied is True
    assert profiler.popen_calls  # 代理确实起来了
    assert any(command.endswith("reverse tcp:8080 tcp:8080") for command in device.commands())
    params = json.loads(profiler.popen_calls[0][1]["env"]["MOBILE_PERF_NETPARAMS"])
    assert params == {"latency_ms": 0.0, "down_kbps": 0.0, "up_kbps": 0.0, "loss_pct": 0.0}


def test_start_none_profile_without_mitmproxy_still_collects(tmp_path):
    """none 档不整形，代理缺失不阻断采集：如实记 note，不报错。"""
    device = FakeDevice()
    profiler = _profiler(device, available=False)

    session = profiler.start(netprof.build_profile("none"), work_dir=tmp_path)

    assert session.applied is True
    assert session.setup_errors == []
    assert "net_conn_duration_ms" in (session.note or "")
    assert device.calls == []  # 未碰设备
    assert session.not_tested_reason is None


def test_start_without_mitmproxy_degrades_with_hint_and_no_device_changes(tmp_path):
    device = FakeDevice()
    profiler = _profiler(device, available=False)

    session = profiler.start(netprof.build_profile("weak"), work_dir=tmp_path)

    assert session.applied is False
    assert session.effective is False
    assert "mitmproxy" in session.setup_errors[0]
    assert "MOBILE_PERF_MITMDUMP" in session.hint
    assert device.calls == []
    assert "not_tested" not in session.setup_errors[0]  # 原因本身不夹带状态词


def test_start_fails_fast_when_port_busy(tmp_path):
    device = FakeDevice()
    profiler = _profiler(device, port_busy=True)

    session = profiler.start(netprof.build_profile("weak"), work_dir=tmp_path)

    assert session.applied is False
    assert "已被占用" in session.setup_errors[0]
    assert profiler.popen_calls == []


def test_start_rolls_back_proxy_when_reverse_fails(tmp_path):
    device = FakeDevice(failing=("reverse tcp:8080 tcp:8080",))
    profiler = _profiler(device)

    session = profiler.start(netprof.build_profile("weak"), work_dir=tmp_path)

    assert session.applied is False
    assert session.reverse_set is False
    assert session.proxy_set is False
    assert profiler.popen_calls  # 进程起过
    assert session.restored == {}  # 回滚不留待恢复项


def test_start_rolls_back_reverse_and_proxy_when_proxy_setting_fails(tmp_path):
    device = FakeDevice(failing=("http_proxy 127.0.0.1:8080",))
    profiler = _profiler(device)

    session = profiler.start(netprof.build_profile("weak"), work_dir=tmp_path)

    assert session.applied is False
    assert session.proxy_set is False
    commands = device.commands()
    assert any("reverse --remove tcp:8080" in command for command in commands)  # 已回滚


def test_context_manager_restores_even_when_body_raises(tmp_path):
    device = FakeDevice()
    profiler = _profiler(device)

    with pytest.raises(RuntimeError, match="采集失败"):
        with profiler:
            profiler.start(netprof.build_profile("weak"), work_dir=tmp_path)
            raise RuntimeError("采集失败")

    commands = device.commands()
    assert any(command.endswith("settings put global http_proxy :0") for command in commands)
    assert any(command.endswith("reverse --remove tcp:8080") for command in commands)


def test_stop_is_idempotent(tmp_path):
    device = FakeDevice()
    profiler = _profiler(device)
    profiler.start(netprof.build_profile("weak"), work_dir=tmp_path)

    first = profiler.stop()
    calls_after_first = len(device.calls)
    second = profiler.stop()

    assert second is first
    assert len(device.calls) == calls_after_first  # 不再重复发恢复命令


def test_stop_without_start_is_safe():
    profiler = _profiler(FakeDevice())

    session = profiler.stop()

    assert session.applied is False


def test_double_start_raises(tmp_path):
    profiler = _profiler(FakeDevice())
    profiler.start(netprof.build_profile("weak"), work_dir=tmp_path)

    with pytest.raises(netprof.NetProfileError, match="已在运行"):
        profiler.start(netprof.build_profile("weak"), work_dir=tmp_path)


def test_session_evidence_comes_from_proxy_log_not_from_app_behaviour(tmp_path):
    """关键回归：环境设置成功但代理侧零连接 → effective=False 且原因如实。

    这正是"页面能打开不等于代理生效"的落点——判定只看代理侧日志。
    """
    device = FakeDevice()
    profiler = _profiler(device)

    profiler.start(netprof.build_profile("weak"), work_dir=tmp_path)
    log_path = tmp_path / netprof.LOG_FILENAME
    log_path.write_text(
        json.dumps({"t": 1.0, "ev": "params", "latencyMs": 400}) + "\n", encoding="utf-8"
    )
    session = profiler.stop()

    assert session.applied is True
    assert session.evidence == netprof.EVIDENCE_NONE
    assert session.effective is False
    assert "代理侧日志未观察到任何连接" in session.not_tested_reason


def test_session_evidence_observed_when_proxy_log_has_traffic(tmp_path):
    device = FakeDevice()
    profiler = _profiler(device)

    profiler.start(netprof.build_profile("weak"), work_dir=tmp_path)
    (tmp_path / netprof.LOG_FILENAME).write_text(
        "\n".join(
            _lines(
                {"t": 0.0, "ev": "tcp_start", "fid": "f1", "host": "api.test", "port": 443},
                {"t": 0.45, "ev": "tcp_message", "fid": "f1", "from_client": True, "size": 512},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    session = profiler.stop()

    assert session.effective is True
    assert session.not_tested_reason is None
    assert netprof.conn_duration_rows(session.log_summary)[0]["median"] == pytest.approx(450.0)


def test_addon_params_and_log_path_reach_subprocess_env(tmp_path):
    profiler = _profiler(FakeDevice())

    profiler.start(netprof.build_profile("weak"), work_dir=tmp_path)

    args, kwargs = profiler.popen_calls[0]
    params = json.loads(kwargs["env"]["MOBILE_PERF_NETPARAMS"])
    assert params == {"latency_ms": 400.0, "down_kbps": 400.0, "up_kbps": 200.0,
                      "loss_pct": 2.0}
    assert kwargs["env"]["MOBILE_PERF_NETLOG"] == str(tmp_path / netprof.LOG_FILENAME)
    assert args[-1].endswith(netprof.ADDON_FILENAME)
    assert "--listen-port" in args


def test_spawn_failure_is_reported_and_not_retried(tmp_path):
    def popen(_args, **_kwargs):
        raise OSError("no such executable")

    profiler = netprof.NetProfiler(
        adb_path="adb", serial="s1", mitmdump="/fake/mitmdump", call_device=FakeDevice(),
        popen=popen, sleep=lambda _s: None, now=lambda: 0.0, probe=lambda _p: "12.3.0",
        which=lambda _n: None, in_use=lambda _p: False,
    )

    session = profiler.start(netprof.build_profile("weak"), work_dir=tmp_path)

    assert session.applied is False
    assert "代理启动失败" in session.setup_errors[0]


def test_process_exiting_during_startup_is_reported(tmp_path):
    class DeadProcess(FakeProcess):
        def poll(self):
            return 1  # 起来就退出

    profiler = netprof.NetProfiler(
        adb_path="adb", serial="s1", mitmdump="/fake/mitmdump", call_device=FakeDevice(),
        popen=lambda _a, **_k: DeadProcess(), sleep=lambda _s: None, now=lambda: 0.0,
        probe=lambda _p: "12.3.0", which=lambda _n: None, in_use=lambda _p: False,
    )

    session = profiler.start(netprof.build_profile("weak"), work_dir=tmp_path)

    assert session.applied is False
    assert "提前退出" in session.setup_errors[0]


def test_addon_file_is_present_next_to_module():
    from pathlib import Path

    import mobile_perf

    addon = Path(mobile_perf.__file__).parent / netprof.ADDON_FILENAME
    assert addon.is_file()


# ---------- doctor ----------


def test_doctor_cleans_stale_proxy_and_verifies_cleanup():
    device = FakeDevice(
        stdout_map={
            "s1 shell settings get global http_proxy": "127.0.0.1:8080\n",
            "s1 reverse --list": "host-1 tcp:8080 tcp:8080\n",
        }
    )

    # 清理后复读：proxy 读回 :0（settings put 生效），reverse 已空
    def call(args, timeout_s=15.0):
        result = device(args, timeout_s)
        joined = " ".join(args)
        if len(device.calls) > 4:  # 前 4 条是「读+读+清+清」，之后是复读
            if joined.endswith("settings get global http_proxy"):
                result.stdout = ":0\n"
            elif joined.endswith("reverse --list"):
                result.stdout = ""
        return result

    report = netprof.doctor(call, "s1")

    assert report["staleProxy"] is True
    assert report["staleReverse"] is True
    assert report["cleaned"] is True
    assert report["proxyClean"] is True
    assert report["reverseClean"] is True
    commands = device.commands()
    assert any(command.endswith("settings put global http_proxy :0") for command in commands)
    assert any(command.endswith("reverse --remove tcp:8080") for command in commands)


def test_doctor_on_clean_device_changes_nothing():
    device = FakeDevice(
        stdout_map={
            "s1 shell settings get global http_proxy": ":0\n",
            "s1 reverse --list": "",
        }
    )

    report = netprof.doctor(device, "s1")

    assert report["cleaned"] is False
    assert report["proxyClean"] is True
    assert report["reverseClean"] is True
    assert len(device.calls) == 2  # 只读两次，无写操作


def test_doctor_reports_when_cleanup_did_not_take_effect():
    """清理命令返回 0 不等于设备真的干净——复读仍脏必须如实报告。"""
    device = FakeDevice(
        stdout_map={
            "s1 shell settings get global http_proxy": "127.0.0.1:8080\n",
            "s1 reverse --list": "",
        }
    )

    report = netprof.doctor(device, "s1")

    assert report["cleaned"] is True
    assert report["proxyClean"] is False  # 复读仍是旧值


def test_doctor_ignores_reverse_entries_for_other_ports():
    device = FakeDevice(
        stdout_map={
            "s1 shell settings get global http_proxy": ":0\n",
            "s1 reverse --list": "host-1 tcp:9999 tcp:9999\n",
        }
    )

    report = netprof.doctor(device, "s1")

    assert report["staleReverse"] is False
    assert report["cleaned"] is False


# ---------- 解密档与接口级指标（工单 18） ----------


def test_analyze_log_trust_probe_records_first_conclusion_only():
    """信任探测首个结论即定论：后续握手不得改写（addon 侧也只记一次）。"""
    ok = netprof.analyze_proxy_log(
        _lines({"t": 1.0, "ev": "trust_probe", "ok": True, "host": "api.test"})
    )
    rejected = netprof.analyze_proxy_log(
        _lines(
            {
                "t": 1.0,
                "ev": "trust_probe",
                "ok": False,
                "error": "tlsv1 alert unknown ca",
                "host": "api.test",
            },
            {"t": 2.0, "ev": "trust_probe", "ok": True, "host": "api.test"},
        )
    )

    assert ok.trust == netprof.TRUST_OK and ok.trust_error is None
    assert rejected.trust == netprof.TRUST_REJECTED
    assert rejected.trust_error == "tlsv1 alert unknown ca"
    assert rejected.trust_host == "api.test"
    # 没有探测事件 = 没有结论，不能当成"信任"
    assert netprof.ProxyLogSummary().trust == netprof.TRUST_UNKNOWN


def test_analyze_log_collects_api_records_and_connect_failures():
    summary = netprof.analyze_proxy_log(
        _lines(
            {"ev": "http_connect", "host": "api.test", "port": 443},
            {
                "ev": "api_request",
                "fid": "f1",
                "host": "api.test",
                "route": "GET /a",
                "status": 200,
                "durationMs": 120.5,
                "sizeBytes": 2048,
            },
            {
                "ev": "api_request",
                "fid": "f2",
                "host": "cdn.test",
                "route": "POST /b",
                "status": 503,
                "durationMs": 30.0,
                "sizeBytes": 0,
            },
            {"ev": "api_error", "host": "api.test", "error": "refused"},
        )
    )

    assert [record.route for record in summary.api_records] == ["GET /a", "POST /b"]
    assert summary.api_records[0].duration_ms == pytest.approx(120.5)
    assert summary.api_records[0].size_bytes == 2048
    assert summary.api_errors == 1
    assert summary.hosts == ["api.test", "cdn.test"]  # 接口请求也贡献域名
    assert summary.as_dict()["apiRequests"] == 2
    assert summary.as_dict()["apiErrors"] == 1


def test_analyze_log_api_traffic_counts_as_evidence():
    """解密档没有 tcp_start（不解密才走 TCPLayer），明文请求本身即生效证据。"""
    summary = netprof.analyze_proxy_log(
        _lines(
            {"ev": "http_connect", "host": "api.test", "port": 443},
            {"ev": "http_request", "fid": "f1", "host": "api.test", "path": "/a"},
            {
                "ev": "api_request",
                "fid": "f1",
                "host": "api.test",
                "route": "GET /a",
                "status": 200,
                "durationMs": 10.0,
                "sizeBytes": 1,
            },
        )
    )

    assert summary.connections == 0
    assert summary.evidence == netprof.EVIDENCE_OBSERVED


def _api_summary() -> netprof.ProxyLogSummary:
    return netprof.analyze_proxy_log(
        _lines(
            {"ev": "http_request", "fid": "f1", "host": "api.test", "path": "/a"},
            *[
                {
                    "ev": "api_request",
                    "fid": fid,
                    "host": host,
                    "route": route,
                    "status": status,
                    "durationMs": duration,
                    "sizeBytes": size,
                }
                for fid, host, route, status, duration, size in (
                    ("f1", "api.test", "GET /a", 200, 100.0, 1024),
                    ("f2", "api.test", "GET /a", 200, 300.0, 2048),
                    ("f3", "api.test", "GET /b", 500, 500.0, 0),
                )
            ],
            {"ev": "api_error", "host": "api.test", "error": "refused"},
        )
    )


def test_api_aggregates_uses_per_request_distribution():
    """口径：逐请求样本（同 URL 多次各算一次），不是"每 URL 平均再取中位"。"""
    rows = netprof.api_aggregates(_api_summary())
    by_key = {row["metric_key"]: row for row in rows}

    assert set(by_key) == set(netprof.API_METRIC_KEYS)
    duration = by_key[netprof.METRIC_API_DURATION]
    assert duration["sample_count"] == 3  # 三个请求，而非两个路由
    assert duration["median"] == pytest.approx(300.0)
    assert duration["p90"] == pytest.approx(460.0)  # 100/300/500 线性插值 0.9 位
    assert duration["unit"] == "ms"
    assert duration["source"] == "pc_supplement"
    payload = by_key[netprof.METRIC_API_PAYLOAD]
    assert payload["median"] == pytest.approx(1.0)  # 1KB / 2KB / 0KB
    assert payload["unit"] == "KB"


def test_api_aggregates_error_rate_counts_status_and_connect_failures():
    """错误率分子 = 状态码 ≥400 的请求 + 连接失败（后者没有状态码，单独计数）。"""
    by_key = {row["metric_key"]: row for row in netprof.api_aggregates(_api_summary())}

    count = by_key[netprof.METRIC_API_COUNT]
    assert count["median"] == pytest.approx(4.0)  # 3 次成功到达 + 1 次连接失败
    assert count["sample_count"] == 1  # 会话级单样本
    rate = by_key[netprof.METRIC_API_ERROR_RATE]
    assert rate["median"] == pytest.approx(50.0)  # (1 个 500 + 1 个连接失败) / 4
    assert rate["sample_count"] == 1


def test_api_aggregates_empty_when_no_records():
    """没有接口记录就不产出行——不拿 0 冒充"没有错误"或"没有流量"。"""
    assert netprof.api_aggregates(netprof.ProxyLogSummary()) == []
    only_errors = netprof.analyze_proxy_log(
        _lines({"ev": "api_error", "host": "api.test", "error": "refused"})
    )
    assert netprof.api_aggregates(only_errors) == []


def test_api_breakdown_sorts_by_p90_and_groups_by_host():
    breakdown = netprof.api_breakdown(_api_summary())

    assert breakdown["totalRoutes"] == 2
    assert [item["route"] for item in breakdown["top"]] == ["GET /b", "GET /a"]  # P90 降序
    slowest = breakdown["top"][0]
    assert slowest["host"] == "api.test"
    assert slowest["count"] == 1 and slowest["errors"] == 1
    fast = breakdown["top"][1]
    assert fast["count"] == 2  # 同一路由两次请求合成一组
    assert fast["medianMs"] == pytest.approx(200.0)
    assert fast["p90Ms"] == pytest.approx(280.0)  # 100/300 线性插值 0.9 位
    assert fast["maxMs"] == pytest.approx(300.0)
    assert fast["payloadKb"] == pytest.approx(3.0)
    assert breakdown["byHost"] == {"api.test": 3}
    # 错误总数 = 连接失败 1 + 状态码错误 1
    assert breakdown["errors"] == 2


def test_api_breakdown_respects_top_n():
    summary = netprof.analyze_proxy_log(
        _lines(
            *[
                {
                    "ev": "api_request",
                    "host": "api.test",
                    "route": f"GET /p{i}",
                    "status": 200,
                    "durationMs": float(i),
                    "sizeBytes": 0,
                }
                for i in range(1, 6)
            ],
        )
    )

    breakdown = netprof.api_breakdown(summary, top_n=2)

    assert len(breakdown["top"]) == 2
    assert breakdown["totalRoutes"] == 5
    assert [item["route"] for item in breakdown["top"]] == ["GET /p5", "GET /p4"]


def _session(**kwargs) -> netprof.NetSession:
    defaults = {
        "tier": netprof.TIER_DECRYPT,
        "applied": True,
        "evidence": netprof.EVIDENCE_OBSERVED,
    }
    return netprof.NetSession(**{**defaults, **kwargs})


def test_api_effective_requires_decrypt_tier_and_trusted_probe():
    assert _session(trust=netprof.TRUST_OK).api_effective is True
    assert _session(tier=netprof.TIER_PASSTHROUGH, trust=netprof.TRUST_OK).api_effective is False
    assert _session(trust=netprof.TRUST_REJECTED).api_effective is False
    assert _session(trust=netprof.TRUST_UNKNOWN).api_effective is False
    assert _session(trust=netprof.TRUST_PENDING).api_effective is False


def test_api_not_tested_reason_covers_each_branch_with_next_step():
    passthrough = _session(tier=netprof.TIER_PASSTHROUGH, trust=netprof.TRUST_UNKNOWN)
    rejected = _session(trust=netprof.TRUST_REJECTED, trust_error="tlsv1 alert unknown ca")
    pending = _session(trust=netprof.TRUST_PENDING)
    not_applied = _session(applied=False, setup_errors=["mitmproxy 不可用"])
    no_traffic = _session(evidence=netprof.EVIDENCE_NONE)

    assert _session(trust=netprof.TRUST_OK).api_not_tested_reason is None
    assert "直通档不解密 TLS" in passthrough.api_not_tested_reason
    assert "--net-tier decrypt" in passthrough.api_not_tested_reason
    assert "不信任代理 CA" in rejected.api_not_tested_reason
    assert "tlsv1 alert unknown ca" in rejected.api_not_tested_reason
    assert "不重试" in rejected.api_not_tested_reason
    assert "未取得结论" in pending.api_not_tested_reason
    assert not_applied.api_not_tested_reason == "mitmproxy 不可用"
    assert "代理侧日志未观察到任何连接" in no_traffic.api_not_tested_reason


def test_trust_hint_lists_executable_next_steps():
    """not_tested 原因之外还要给出可执行的下一步（换包 / root / 模拟器）。"""
    assert "debug" in netprof.TRUST_HINT
    assert "root" in netprof.TRUST_HINT
    assert "writable-system" in netprof.TRUST_HINT


def test_profiler_rejects_unknown_tier():
    with pytest.raises(ValueError, match="未知弱网档位模式"):
        netprof.NetProfiler(adb_path="adb", serial="s1", tier="mitm")


def test_decrypt_tier_passes_tier_to_addon_environment(tmp_path):
    profiler = _profiler(FakeDevice(), tier=netprof.TIER_DECRYPT)

    profiler.start(netprof.build_profile("weak"), work_dir=tmp_path)

    assert profiler.popen_calls[0][1]["env"]["MOBILE_PERF_NETTIER"] == "decrypt"


def test_passthrough_tier_is_the_default_addon_environment(tmp_path):
    profiler = _profiler(FakeDevice())

    profiler.start(netprof.build_profile("weak"), work_dir=tmp_path)

    assert profiler.popen_calls[0][1]["env"]["MOBILE_PERF_NETTIER"] == "passthrough"


def test_decrypt_tier_none_profile_still_requires_proxy(tmp_path):
    """解密档即使 none 档位也不能省代理——否则接口级指标没有数据来源。"""
    device = FakeDevice()
    profiler = _profiler(device, available=False, tier=netprof.TIER_DECRYPT)

    session = profiler.start(netprof.build_profile("none"), work_dir=tmp_path)

    assert session.applied is False
    assert "mitmproxy" in session.setup_errors[0]
    assert device.calls == []


def _write_log(tmp_path, records) -> None:
    (tmp_path / netprof.LOG_FILENAME).write_text(
        "\n".join(_lines(*records)) + "\n", encoding="utf-8"
    )


def test_decrypt_session_trusted_yields_api_rows_and_breakdown(tmp_path):
    profiler = _profiler(FakeDevice(), tier=netprof.TIER_DECRYPT)
    profiler.start(netprof.build_profile("weak"), work_dir=tmp_path)
    _write_log(
        tmp_path,
        [
            {"ev": "trust_probe", "ok": True, "host": "api.test"},
            {"ev": "http_request", "fid": "f1", "host": "api.test", "path": "/a"},
            {
                "ev": "api_request",
                "fid": "f1",
                "host": "api.test",
                "route": "GET /a",
                "status": 200,
                "durationMs": 250.0,
                "sizeBytes": 4096,
            },
        ],
    )

    session = profiler.stop()

    assert session.trust == netprof.TRUST_OK
    assert session.api_effective is True
    assert session.as_dict()["apiEffective"] is True
    assert [row["metric_key"] for row in profiler.api_aggregates()] == list(
        netprof.API_METRIC_KEYS
    )
    breakdown = profiler.api_breakdown()
    assert breakdown["top"][0]["route"] == "GET /a"
    assert breakdown["top"][0]["p90Ms"] == pytest.approx(250.0)


def test_decrypt_session_rejected_yields_no_api_values(tmp_path):
    """证书不信任：不重试、不产出任何 api_* 数值，但连接级指标仍可用。"""
    profiler = _profiler(FakeDevice(), tier=netprof.TIER_DECRYPT)
    profiler.start(netprof.build_profile("weak"), work_dir=tmp_path)
    _write_log(
        tmp_path,
        [
            {
                "ev": "trust_probe",
                "ok": False,
                "error": "tlsv1 alert unknown ca",
                "host": "api.test",
            },
            # 判定之后 addon 降为直通，连接级证据照旧
            {"ev": "tcp_start", "fid": "f9", "host": "api.test", "port": 443},
            {"ev": "tcp_message", "fid": "f9", "from_client": True, "size": 10},
        ],
    )

    session = profiler.stop()

    assert session.trust == netprof.TRUST_REJECTED
    assert session.api_effective is False
    assert "不信任代理 CA" in session.api_not_tested_reason
    assert profiler.api_aggregates() == []
    assert profiler.api_breakdown() is None
    # 连接级指标不受影响
    assert session.effective is True
    assert netprof.conn_duration_rows(session.log_summary)


def test_decrypt_session_rejected_without_any_tunnel_reports_cert_cause(tmp_path):
    """回归：首个 CONNECT 就被证书拒绝时 addon 拿不到 tcp_start，evidence 是 none。

    此时原因必须说"证书被拒绝"，不能说成"App 可能走缓存/未发起请求"——
    后者会把最明确的原因说错，把排查引向错误方向。实测（本机 https 反证）
    正是这个形状：trust=rejected、apiRecords=0、connections=0。
    """
    profiler = _profiler(FakeDevice(), tier=netprof.TIER_DECRYPT)
    profiler.start(netprof.build_profile("weak"), work_dir=tmp_path)
    _write_log(
        tmp_path,
        [
            {
                "ev": "trust_probe",
                "ok": False,
                "error": "The client does not trust the proxy's certificate for "
                "example.com (tlsv1 alert unknown ca)",
                "host": "example.com",
            }
        ],
    )

    session = profiler.stop()

    assert session.evidence == netprof.EVIDENCE_NONE
    assert session.effective is False
    assert session.api_effective is False
    assert "不信任代理 CA" in session.api_not_tested_reason
    assert "unknown ca" in session.api_not_tested_reason
    assert "未观察到任何连接" not in session.api_not_tested_reason
    # 连接级指标的原因同样要指向证书，而不是"App 可能走缓存"
    assert "证书拒绝" in session.not_tested_reason
    assert "App 可能走缓存" not in session.not_tested_reason


def test_passthrough_session_never_claims_trust(tmp_path):
    profiler = _profiler(FakeDevice())
    profiler.start(netprof.build_profile("weak"), work_dir=tmp_path)
    _write_log(
        tmp_path,
        [
            {"ev": "trust_probe", "ok": True, "host": "api.test"},  # 直通档不该有这条
            {"ev": "tcp_start", "fid": "f1", "host": "api.test", "port": 443},
        ],
    )

    session = profiler.stop()

    assert session.trust == netprof.TRUST_UNKNOWN
    assert session.api_effective is False
    assert profiler.api_breakdown() is None


# ---------- 真实 mitmproxy 集成（可选：未装则跳过） ----------


def _real_mitmdump() -> str | None:
    """本机是否真有 mitmdump（读真实环境：PATH 与 MOBILE_PERF_MITMDUMP）。"""
    availability = netprof.find_mitmproxy()
    return availability.executable if availability.available else None


MITMDUMP = _real_mitmdump()


def _start_echo_server() -> tuple[int, object]:
    """本地 TCP echo 服务：作为 CONNECT 隧道上游，不依赖外网。"""
    import threading

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]

    def serve():
        while True:
            try:
                conn, _addr = listener.accept()
            except OSError:
                return  # listener 已关闭
            with conn:
                data = conn.recv(4096)
                if data:
                    conn.sendall(data)

    threading.Thread(target=serve, daemon=True).start()
    return port, listener


def _tunnel_request(proxy_port: int, target_port: int, payload: bytes) -> bytes:
    """经代理建 CONNECT 隧道并收发字节（走 addon 的整形路径）。"""
    with socket.create_connection(("127.0.0.1", proxy_port), timeout=30) as sock:
        sock.sendall(
            f"CONNECT 127.0.0.1:{target_port} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{target_port}\r\n\r\n".encode()
        )
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                raise AssertionError("代理未回 CONNECT 响应")
            head += chunk
        assert b"200" in head.split(b"\r\n", 1)[0]
        sock.sendall(payload)
        return sock.recv(4096)


@pytest.mark.skipif(MITMDUMP is None, reason="本机无可用 mitmdump（Python ≥3.12 独立 venv）")
def test_real_proxy_tunnels_and_writes_evidence(tmp_path):
    """真起 mitmproxy：CONNECT 隧道可用、代理侧日志给出证据、日志首行带整形参数。

    走本地 echo 服务而非外网：只验证"隧道通、日志真、参数真"，不测外网可达性。
    整形延迟的正确性由 addon 的 ``_delay_for`` 单测覆盖（真机弱网验证另在
    tests/device 与 scripts 里做），此处不把网络抖动当成断言依据。
    """
    upstream_port, listener = _start_echo_server()
    profiler = netprof.NetProfiler(
        adb_path="adb", serial="s1", call_device=FakeDevice(), mitmdump=MITMDUMP,
        which=lambda _n: None,
    )
    profiler.port = _free_port()
    try:
        session = profiler.start(
            netprof.build_profile(
                "custom",
                {"latency_ms": 200, "down_kbps": 0, "up_kbps": 0, "loss_pct": 0},
            ),
            work_dir=tmp_path,
        )
        assert session.applied is True, session.setup_errors

        echoed = _tunnel_request(profiler.port, upstream_port, b"ping-through-tunnel")
        stopped = profiler.stop()
    finally:
        listener.close()
        profiler.stop()

    assert echoed == b"ping-through-tunnel"  # 隧道真的通了（未解密、原样转发）
    summary = stopped.log_summary
    assert summary.connections >= 1
    assert summary.conn_durations_ms  # 有首字节即得一条连接耗时
    assert stopped.effective is True
    assert summary.params is not None and summary.params["latencyMs"] == 200.0


def test_delay_for_adds_latency_bandwidth_and_loss_stall():
    """整形口径：延迟恒加一次，带宽按字节追加，丢包按概率追加 2× 延迟停顿。

    addon 依赖 mitmproxy（Python ≥3.12），主进程环境装不了它，故用 mitmdump 同目录
    的解释器在子进程里算——这也正是它在生产里的运行方式。
    """
    base = {"latency_ms": 100, "down_kbps": 8, "up_kbps": 8, "loss_pct": 0}
    delays = _addon_delays(base, [(0, True), (2000, False), (1000, True)])

    assert delays[0] == pytest.approx(0.1)  # 仅固定延迟
    assert delays[1] == pytest.approx(2.1)  # 8 kbps=1000 B/s → 2000 字节下行 2 s
    assert delays[2] == pytest.approx(1.1)  # 上行用 up_kbps，与下行独立

    lossy = _addon_delays(
        {"latency_ms": 100, "down_kbps": 0, "up_kbps": 0, "loss_pct": 100},
        [(10, True)],
    )
    assert lossy[0] == pytest.approx(0.3)  # 100ms + 2× 重传停顿


def _addon_python() -> str | None:
    """mitmdump 同目录的解释器（addon 只能跑在 ≥3.12 的那个环境里）。"""
    if MITMDUMP is None:
        return None
    base = Path(MITMDUMP).resolve().parent
    for name in ("python.exe", "python3.exe", "python", "python3"):
        candidate = base / name
        if candidate.is_file():
            return str(candidate)
    return None


def _addon_delays(params: dict, cases: list[tuple[int, bool]]) -> list[float]:
    """在子进程里用真实 addon 计算给定消息的整形延迟（秒）。"""
    python = _addon_python()
    if python is None:
        pytest.skip("找不到 mitmdump 同目录的解释器")
    addon = Path(netprof.__file__).resolve().parent / netprof.ADDON_FILENAME
    code = (
        "import importlib.util, json, sys\n"
        "spec = importlib.util.spec_from_file_location('a', sys.argv[1])\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "cases = json.loads(sys.argv[2])\n"
        "print(json.dumps([mod._delay_for(n, c) for n, c in cases]))\n"
    )
    environment = {
        **os.environ,
        "MOBILE_PERF_NETPARAMS": json.dumps(params),
        "MOBILE_PERF_NETLOG": "",
    }
    result = subprocess.run(
        [python, "-c", code, str(addon), json.dumps(cases)],
        capture_output=True,
        text=True,
        env=environment,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


_ADDON_PREAMBLE = """
import importlib.util, json, sys, types
spec = importlib.util.spec_from_file_location('addon', sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

class Marker:
    def __init__(self, context):
        self.context = context

mod.TCPLayer = Marker  # 不真建 mitmproxy 层，只看"是否被改成裸 TCP"

def ctx(cid='c1', host='api.test'):
    server = types.SimpleNamespace(address=(host, 443) if host else None)
    client = types.SimpleNamespace(id=cid)
    return types.SimpleNamespace(
        server=server, client=client, layers=[], options=types.SimpleNamespace()
    )

def layer_name(layer):
    return None if layer is None else type(layer).__name__

out = {}
"""


def _addon_probe(body: str, *, tier: str, params: dict | None = None) -> dict:
    """在 mitmdump 的解释器里加载真实 addon 并执行给定脚本体，返回其 ``out``。

    走子进程是唯一可行路径：addon 顶部 import mitmproxy，而主进程环境是
    Python 3.10（mitmproxy 要求 ≥3.12）。这也正是它在生产里的运行方式。
    """
    python = _addon_python()
    if python is None:
        pytest.skip("找不到 mitmdump 同目录的解释器")
    addon = Path(netprof.__file__).resolve().parent / netprof.ADDON_FILENAME
    code = _ADDON_PREAMBLE + body + "\nprint(json.dumps(out))\n"
    with tempfile.TemporaryDirectory() as work:
        log = Path(work) / "net_proxy.jsonl"
        log.touch()  # 一条事件都没产生时 addon 不会建文件，先占位便于统一读取
        environment = {
            **os.environ,
            "MOBILE_PERF_NETPARAMS": json.dumps(params or {}),
            "MOBILE_PERF_NETTIER": tier,
            "MOBILE_PERF_NETLOG": str(log),
        }
        result = subprocess.run(
            [python, "-c", code, str(addon)],
            capture_output=True,
            text=True,
            env=environment,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        out = json.loads(result.stdout.strip().splitlines()[-1])
        out["log"] = [
            json.loads(line)
            for line in log.read_text(encoding="utf-8").splitlines()
            if line
        ]
    return out


@pytest.mark.skipif(MITMDUMP is None, reason="本机无可用 mitmdump（Python ≥3.12 独立 venv）")
def test_addon_decrypt_tier_keeps_tls_layer_when_handshake_succeeds():
    out = _addon_probe(
        """
data = types.SimpleNamespace(context=ctx(), layer=None)
mod.tls_established_client(data)
mod._TUNNEL_CLIENTS.add('c1')
mod.next_layer(data)
out['layer'] = layer_name(data.layer)
out['decryptOk'] = mod._DECRYPT_OK
""",
        tier="decrypt",
    )

    assert out["decryptOk"] is True
    assert out["layer"] is None  # 放行给 mitmproxy 的 TLS 层（不解密就没有接口级数据）
    probes = [r for r in out["log"] if r["ev"] == "trust_probe"]
    assert len(probes) == 1 and probes[0]["ok"] is True
    assert probes[0]["host"] == "api.test"


@pytest.mark.skipif(MITMDUMP is None, reason="本机无可用 mitmdump（Python ≥3.12 独立 venv）")
def test_addon_cert_rejection_locks_tier_and_falls_back_to_passthrough():
    """证书不信任 → 判定一次即锁定，后续 CONNECT 降为裸 TCP（保住连接级指标）。"""
    out = _addon_probe(
        """
mod._TUNNEL_CLIENTS.add('c1')
first = types.SimpleNamespace(
    context=ctx(),
    conn=types.SimpleNamespace(error='tlsv1 alert unknown ca'),
)
mod.tls_failed_client(first)
mod.tls_failed_client(first)  # 再来一次握手：不得重复判定
out['decryptOk'] = mod._DECRYPT_OK
data = types.SimpleNamespace(context=ctx(), layer=None)
mod.next_layer(data)
out['layer'] = layer_name(data.layer)
""",
        tier="decrypt",
    )

    assert out["decryptOk"] is False
    assert out["layer"] == "Marker"  # 已降为裸 TCP
    probes = [r for r in out["log"] if r["ev"] == "trust_probe"]
    assert len(probes) == 1  # 只判定一次，不重试
    assert probes[0]["ok"] is False
    assert probes[0]["certRejected"] is True
    assert "unknown ca" in probes[0]["error"]


@pytest.mark.skipif(MITMDUMP is None, reason="本机无可用 mitmdump（Python ≥3.12 独立 venv）")
def test_addon_non_cert_tls_failure_does_not_lock_tier():
    """非证书类失败不是"App 不信任"的证据，不得锁定本 run 的解密能力。"""
    out = _addon_probe(
        """
mod._TUNNEL_CLIENTS.add('c1')
data = types.SimpleNamespace(
    context=ctx(),
    conn=types.SimpleNamespace(error='connection reset by peer'),
)
mod.tls_failed_client(data)
out['decryptOk'] = mod._DECRYPT_OK
layer = types.SimpleNamespace(context=ctx(), layer=None)
mod.next_layer(layer)
out['layer'] = layer_name(layer.layer)
""",
        tier="decrypt",
    )

    assert out["decryptOk"] is None  # 仍未判定，下次握手还能试
    assert out["layer"] is None  # 仍然走解密
    assert not [r for r in out["log"] if r["ev"] == "trust_probe"]
    errors = [r for r in out["log"] if r["ev"] == "tls_client_error"]
    assert errors and "connection reset" in errors[0]["error"]


@pytest.mark.skipif(MITMDUMP is None, reason="本机无可用 mitmdump（Python ≥3.12 独立 venv）")
def test_addon_passthrough_tier_never_decrypts():
    out = _addon_probe(
        """
mod._TUNNEL_CLIENTS.add('c1')
data = types.SimpleNamespace(context=ctx(), layer=None)
mod.next_layer(data)
out['layer'] = layer_name(data.layer)
mod.tls_established_client(types.SimpleNamespace(context=ctx()))
out['decryptOk'] = mod._DECRYPT_OK
""",
        tier="passthrough",
    )

    assert out["layer"] == "Marker"  # 强制裸 TCP
    assert out["decryptOk"] is None  # 直通档不探测信任
    assert not [r for r in out["log"] if r["ev"] == "trust_probe"]


@pytest.mark.skipif(MITMDUMP is None, reason="本机无可用 mitmdump（Python ≥3.12 独立 venv）")
def test_addon_response_emits_api_record_without_query_string():
    """接口明细：路由去掉查询串（否则 id/时间戳会把同一接口打散）。"""
    out = _addon_probe(
        """
request = types.SimpleNamespace(
    host='api.test', method='GET', path='/v1/list?page=2&ts=123', timestamp_start=100.0
)
resp = types.SimpleNamespace(
    status_code=200, timestamp_end=100.25, raw_content=b'x' * 1024
)
mod.response(types.SimpleNamespace(request=request, response=resp, id='f1'))
""",
        tier="decrypt",
    )

    records = [r for r in out["log"] if r["ev"] == "api_request"]
    assert len(records) == 1
    assert records[0]["route"] == "GET /v1/list"
    assert records[0]["status"] == 200
    assert records[0]["durationMs"] == pytest.approx(250.0)
    assert records[0]["sizeBytes"] == 1024


@pytest.mark.skipif(MITMDUMP is None, reason="本机无可用 mitmdump（Python ≥3.12 独立 venv）")
def test_addon_passthrough_tier_emits_no_api_records():
    out = _addon_probe(
        """
request = types.SimpleNamespace(
    host='api.test', method='GET', path='/a', timestamp_start=100.0
)
resp = types.SimpleNamespace(status_code=200, timestamp_end=100.1, raw_content=b'')
mod.response(types.SimpleNamespace(request=request, response=resp, id='f1'))
""",
        tier="passthrough",
    )

    assert not [r for r in out["log"] if r["ev"] == "api_request"]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_port_in_use_detects_listening_socket():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        assert netprof.port_in_use(port) is True

    assert netprof.port_in_use(port) is False


def test_dump_json_is_stable():
    payload = {"b": 1, "a": [1, 2]}

    assert json.loads(netprof.dump_json(payload)) == payload


def test_netprof_does_not_import_mitmproxy_in_process():
    """进程边界：主进程不 import mitmproxy（它要求 Python ≥3.12，本项目 ≥3.10）。

    addon 由 mitmdump 子进程加载，故 ``netprof.py`` 源码里不得出现 import mitmproxy；
    反过来，addon 必须真的 import mitmproxy——它只在该子进程里跑。
    """
    package = Path(netprof.__file__).resolve().parent
    orchestrator = (package / "netprof.py").read_text(encoding="utf-8")
    addon = (package / netprof.ADDON_FILENAME).read_text(encoding="utf-8")

    assert "import mitmproxy" not in orchestrator
    assert "from mitmproxy" in addon
