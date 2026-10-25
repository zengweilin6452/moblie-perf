"""CLI 的单元测试：参数解析、serial 解析顺序、collect 端到端（注入假 kit）、report/trend。"""

import json

import pytest

from mobile_perf import adb, cli


def fake_adb(tmp_path):
    adb_path = tmp_path / "adb.exe"
    adb_path.write_bytes(b"x")
    return str(adb_path)


class StubKit:
    """替身 kit：记录 run_collect 调用并返回固定报告。"""

    def __init__(self, adb_path=None, serial=None, runs_dir=None, db_path=None,
                 device_profile=None):
        self.adb_path = adb_path
        self.serial = serial
        self.runs_dir = runs_dir
        self.db_path = db_path
        self.device_profile = device_profile
        self.calls = []

    def run_collect(self, *, target, items, duration_s, host_run_id=None, runs_dir=None,
                    start_mode=None, jank_thresholds_ms=None):
        self.calls.append(
            {
                "target": target,
                "items": items,
                "duration_s": duration_s,
                "host_run_id": host_run_id,
                "runs_dir": runs_dir,
                "start_mode": start_mode,
                "jank_thresholds_ms": jank_thresholds_ms,
            }
        )
        return {"runId": host_run_id or "run-stub", "target": target}


def test_collect_passes_device_profile(tmp_path):
    kits = []

    cli.main(
        [
            "collect",
            "tv.danmaku.bili",
            "--serial",
            "749622c9",
            "--adb",
            fake_adb(tmp_path),
            "--device-profile",
            "PICO4-Android12",
        ],
        kit_factory=lambda **kwargs: kits.append(StubKit(**kwargs)) or kits[0],
    )

    assert kits[0].device_profile == "PICO4-Android12"


def test_collect_parses_package_items_duration_and_options(tmp_path):
    kits = []

    exit_code = cli.main(
        [
            "collect",
            "tv.danmaku.bili",
            "--items",
            "CPU,FPS,Memory",
            "--duration",
            "60",
            "--serial",
            "749622c9",
            "--adb",
            fake_adb(tmp_path),
            "--runs-dir",
            str(tmp_path / "runs"),
            "--run-id",
            "run-cli-1",
        ],
        kit_factory=lambda **kwargs: kits.append(StubKit(**kwargs)) or kits[0],
    )

    assert exit_code == 0
    call = kits[0].calls[0]
    assert call["target"] == "tv.danmaku.bili"
    assert call["items"] == ["CPU", "FPS", "Memory"]
    assert call["duration_s"] == 60.0
    assert call["host_run_id"] == "run-cli-1"
    assert call["runs_dir"] == str(tmp_path / "runs")
    assert kits[0].serial == "749622c9"


def test_collect_serial_falls_back_to_env_then_single_online_device(tmp_path, monkeypatch):
    monkeypatch.delenv("MOBILE_PERF_SERIAL", raising=False)
    captured = {}

    def fake_first_online(adb_path):
        captured["adb"] = adb_path
        return "749622c9"

    monkeypatch.setattr(cli, "first_online_device", fake_first_online)

    cli.main(
        [
            "collect",
            "tv.danmaku.bili",
            "--items",
            "CPU",
            "--duration",
            "5",
            "--adb",
            fake_adb(tmp_path),
            "--runs-dir",
            str(tmp_path / "runs"),
        ],
        kit_factory=lambda **kwargs: captured.setdefault("kit", StubKit(**kwargs)),
    )

    assert captured["kit"].serial == "749622c9"


def test_collect_env_serial_wins_over_auto_detect(tmp_path, monkeypatch):
    monkeypatch.setenv("MOBILE_PERF_SERIAL", "from-env")
    captured = {}

    cli.main(
        [
            "collect",
            "tv.danmaku.bili",
            "--items",
            "CPU",
            "--duration",
            "5",
            "--adb",
            fake_adb(tmp_path),
            "--runs-dir",
            str(tmp_path / "runs"),
        ],
        kit_factory=lambda **kwargs: captured.setdefault("kit", StubKit(**kwargs)),
    )

    assert captured["kit"].serial == "from-env"


def test_collect_without_resolvable_serial_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("MOBILE_PERF_SERIAL", raising=False)
    monkeypatch.setattr(cli, "first_online_device", lambda adb_path: None)

    exit_code = cli.main(
        [
            "collect",
            "tv.danmaku.bili",
            "--items",
            "CPU",
            "--duration",
            "5",
            "--adb",
            fake_adb(tmp_path),
            "--runs-dir",
            str(tmp_path / "runs"),
        ],
        kit_factory=lambda **kwargs: StubKit(**kwargs),
    )

    assert exit_code == 1


def test_collect_collect_error_returns_nonzero(tmp_path):
    class FailingKit(StubKit):
        def run_collect(self, **kwargs):
            raise cli.CollectError("停止采集失败")

    exit_code = cli.main(
        [
            "collect",
            "tv.danmaku.bili",
            "--items",
            "CPU",
            "--duration",
            "5",
            "--serial",
            "749622c9",
            "--adb",
            fake_adb(tmp_path),
            "--runs-dir",
            str(tmp_path / "runs"),
        ],
        kit_factory=lambda **kwargs: FailingKit(**kwargs),
    )

    assert exit_code == 1


def test_list_devices_parses_adb_devices_output():
    sample = (
        "List of devices attached\n"
        "749622c9\tdevice\n"
        "emulator-5554\toffline\n"
        "abc123\tunauthorized\n"
        "\n"
    )

    class FakeRunner:
        def __call__(self, cmd, timeout_s):
            class R:
                returncode = 0
                stdout = sample
                stderr = ""

            return R()

    devices = adb.list_devices("adb.exe", runner=FakeRunner())

    assert devices == ["749622c9"]


def test_cli_module_entry_point_exists():
    from mobile_perf import __main__

    assert __main__.main is cli.main


def test_report_command_writes_dual_artifacts_from_store(tmp_path, capsys):
    """mobile-perf report <runId>：从 runs/ 与 store 组装 JSON+HTML 双产物。"""
    from mobile_perf.store import PerfStore

    run_dir = tmp_path / "runs" / "run-cli-rep"
    (run_dir / "artifacts").mkdir(parents=True)
    (run_dir / "artifacts" / "performance.csv").write_text(
        "RecordTime,PSS-main-1(MB),SimpleTime\n1000,10.0,0\n2000,20.0,1\n", encoding="gb18030"
    )
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "runId": "run-cli-rep",
                "target": "tv.danmaku.bili",
                "items": ["Memory"],
                "serial": "749622c9",
                "startedAt": "2026-10-25T10:00:00",
                "stoppedAt": "2026-10-25T10:00:30",
                "durationS": 30.0,
            }
        ),
        encoding="utf-8",
    )
    db = tmp_path / "data" / "mobile_perf.db"
    store = PerfStore(db)
    store.record_session(
        {"run_id": "run-cli-rep", "target": "tv.danmaku.bili", "run_dir": str(run_dir)}
    )
    store.close()

    exit_code = cli.main(
        [
            "report",
            "run-cli-rep",
            "--runs-dir",
            str(tmp_path / "runs"),
            "--db",
            str(db),
        ]
    )

    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["runId"] == "run-cli-rep"
    report_data = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    assert report_data["series"]["mem_pss_mb"]["points"] == [[0.0, 10.0], [1.0, 20.0]]
    html = (run_dir / "report.html").read_text(encoding="utf-8")
    assert "vendor:echarts BEGIN" in html
    embedded = json.loads(_embedded_json(html))
    assert embedded == report_data


def _embedded_json(html: str) -> str:
    begin = html.index("window.__REPORT__ =") + len("window.__REPORT__ =")
    end = html.index(";</script>", begin)
    return html[begin:end].replace("<\\/", "</")


def test_report_command_unknown_run_returns_nonzero(tmp_path, capsys):
    from mobile_perf.store import PerfStore

    db = tmp_path / "data" / "mobile_perf.db"
    PerfStore(db).close()

    exit_code = cli.main(["report", "run-none", "--runs-dir", str(tmp_path / "runs"),
                          "--db", str(db)])

    assert exit_code == 1
    assert "run-none" in capsys.readouterr().err


def test_trend_command_prints_trend_json(tmp_path, capsys):
    trend_payload = {"pkg": "tv.danmaku.bili", "metric": "mem_pss_mb", "runs": []}
    captured = {}

    class TrendStubKit(StubKit):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            captured["db_path"] = kwargs.get("db_path")

        def trend(self, pkg, metric, profile=None):
            captured["args"] = (pkg, metric, profile)
            return trend_payload

    exit_code = cli.main(
        [
            "trend",
            "tv.danmaku.bili",
            "--metric",
            "mem_pss_mb",
            "--serial",
            "749622c9",
            "--adb",
            fake_adb(tmp_path),
            "--db",
            str(tmp_path / "d.db"),
        ],
        kit_factory=lambda **kwargs: TrendStubKit(**kwargs),
    )

    assert exit_code == 0
    assert captured["args"] == ("tv.danmaku.bili", "mem_pss_mb", None)
    assert captured["db_path"] == str(tmp_path / "d.db")
    assert json.loads(capsys.readouterr().out) == trend_payload


def test_trend_command_passes_explicit_profile_and_error_returns_nonzero(tmp_path, capsys):
    class ProfileKit(StubKit):
        def trend(self, pkg, metric, profile=None):
            raise cli.ProfileMismatchError("跨档案拒绝")

    exit_code = cli.main(
        [
            "trend",
            "tv.danmaku.bili",
            "--metric",
            "mem_pss_mb",
            "--profile",
            "PICO4-Android12",
            "--serial",
            "749622c9",
            "--adb",
            fake_adb(tmp_path),
        ],
        kit_factory=lambda **kwargs: ProfileKit(**kwargs),
    )

    assert exit_code == 1
    assert "跨档案拒绝" in capsys.readouterr().err


class DoctorStubKit:
    """替身 kit：ensure_ready 返回注入的报告（或抛注入的异常）。"""

    def __init__(self, adb_path=None, serial=None, report=None, error=None):
        self.adb_path = adb_path
        self.serial = serial
        self._report = report
        self._error = error

    def ensure_ready(self):
        if self._error is not None:
            raise self._error
        return self._report


def make_readiness_report(**overrides):
    from mobile_perf.keeper import CheckResult, ReadinessReport

    defaults = dict(
        serial="749622c9",
        ok=True,
        needs_attention=False,
        healed=False,
        checks=(CheckResult("connection", "ok", {"serial": "749622c9"}),),
        events=(),
        degraded_items=(),
        foreground_package="tv.danmaku.bili",
        clock_offset_s=0.5,
    )
    defaults.update(overrides)
    return ReadinessReport(**defaults)


def test_doctor_prints_readiness_report_and_exits_zero(tmp_path, capsys):
    kits = []
    exit_code = cli.main(
        ["doctor", "--serial", "749622c9", "--adb", fake_adb(tmp_path)],
        kit_factory=lambda **kwargs: kits.append(
            DoctorStubKit(**kwargs, report=make_readiness_report())
        )
        or kits[0],
    )

    assert exit_code == 0
    assert kits[0].serial == "749622c9"
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["serial"] == "749622c9"


def test_doctor_not_ready_exits_nonzero(tmp_path, capsys):
    report = make_readiness_report(ok=False, needs_attention=True)
    exit_code = cli.main(
        ["doctor", "--serial", "749622c9", "--adb", fake_adb(tmp_path)],
        kit_factory=lambda **kwargs: DoctorStubKit(**kwargs, report=report),
    )

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out)["needsAttention"] is True


def test_doctor_gate_busy_fails_fast_with_nonzero_exit(tmp_path):
    from mobile_perf.gate import GateBusyError

    exit_code = cli.main(
        ["doctor", "--serial", "749622c9", "--adb", fake_adb(tmp_path)],
        kit_factory=lambda **kwargs: DoctorStubKit(
            **kwargs, error=GateBusyError("设备 749622c9 正被其他进程占用")
        ),
    )

    assert exit_code == 1


def test_collect_gate_busy_fails_fast_with_nonzero_exit(tmp_path, monkeypatch):
    from mobile_perf.gate import GateBusyError

    class BusyKit(StubKit):
        def run_collect(self, **kwargs):
            raise GateBusyError("设备 749622c9 正被其他进程占用")

    exit_code = cli.main(
        [
            "collect",
            "tv.danmaku.bili",
            "--items",
            "CPU",
            "--duration",
            "5",
            "--serial",
            "749622c9",
            "--adb",
            fake_adb(tmp_path),
            "--runs-dir",
            str(tmp_path / "runs"),
        ],
        kit_factory=lambda **kwargs: BusyKit(**kwargs),
    )

    assert exit_code == 1


# ---- ticket 09：report --baseline 门禁接线 ----

def _make_report_fixture(tmp_path, run_id="run-cli-judge"):
    """落一个含 Memory 聚合的最小 run：meta.json + store 会话与指标。"""
    from mobile_perf.store import PerfStore

    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "runId": run_id,
                "target": "tv.danmaku.bili",
                "items": ["Memory"],
                "serial": "749622c9",
                "deviceProfile": "PICO4-Android12",
                "startedAt": "2026-10-25T10:00:00",
                "stoppedAt": "2026-10-25T10:00:30",
                "durationS": 30.0,
            }
        ),
        encoding="utf-8",
    )
    db = tmp_path / "data" / "mobile_perf.db"
    store = PerfStore(db)
    store.record_session(
        {
            "run_id": run_id,
            "target": "tv.danmaku.bili",
            "serial": "749622c9",
            "device_profile": "PICO4-Android12",
            "run_dir": str(run_dir),
        }
    )
    store.record_metrics(
        run_id,
        [
            {
                "metric_key": "mem_pss_mb",
                "unit": "MB",
                "source": "solo_pi",
                "min": 100.0,
                "median": 150.0,
                "mean": 150.0,
                "max": 200.0,
                "p90": 190.0,
                "sample_count": 30,
            }
        ],
    )
    return db, store


def test_report_with_baseline_attaches_verdict_to_artifacts(tmp_path, capsys):
    db, store = _make_report_fixture(tmp_path)
    store.record_baseline(
        name="bili-home-v3",
        target_pkg="tv.danmaku.bili",
        device_profile="PICO4-Android12",
        metric_key="mem_pss_mb",
        value=180.0,
        source_run_id="run-baseline-src",
    )
    store.close()

    exit_code = cli.main(
        [
            "report",
            "run-cli-judge",
            "--runs-dir",
            str(tmp_path / "runs"),
            "--db",
            str(db),
            "--baseline",
            "bili-home-v3",
            "--threshold",
            "mem_pss_mb:lt:220:fail",
            "--adb",
            fake_adb(tmp_path),
        ]
    )

    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"]["verdict"] == "passed"
    report_data = json.loads(
        (tmp_path / "runs" / "run-cli-judge" / "report.json").read_text(encoding="utf-8")
    )
    (check,) = report_data["verdict"]["checks"]
    assert check["metricKey"] == "mem_pss_mb"
    assert check["median"] == 150.0
    assert check["threshold"] == 220.0
    assert check["baselineValue"] == 180.0
    assert check["sourceRunId"] == "run-cli-judge"


def test_report_with_baseline_threshold_violation_fails_verdict(tmp_path, capsys):
    db, store = _make_report_fixture(tmp_path)
    store.record_baseline(
        name="bili-home-v3", metric_key="mem_pss_mb", value=180.0
    )
    store.close()

    exit_code = cli.main(
        [
            "report",
            "run-cli-judge",
            "--runs-dir",
            str(tmp_path / "runs"),
            "--db",
            str(db),
            "--baseline",
            "bili-home-v3",
            "--threshold",
            "mem_pss_mb:lt:100:fail",
            "--adb",
            fake_adb(tmp_path),
        ]
    )

    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"]["verdict"] == "failed"


def test_report_baseline_bad_threshold_spec_returns_nonzero(tmp_path, capsys):
    db, store = _make_report_fixture(tmp_path)
    store.close()

    exit_code = cli.main(
        [
            "report",
            "run-cli-judge",
            "--runs-dir",
            str(tmp_path / "runs"),
            "--db",
            str(db),
            "--baseline",
            "bili-home-v3",
            "--threshold",
            "mem_pss_mb:notanop:220:fail",
            "--adb",
            fake_adb(tmp_path),
        ]
    )

    assert exit_code == 1
    assert "threshold" in capsys.readouterr().err.lower()


def test_serve_command_is_reserved_placeholder(capsys):
    """§6.6：serve HTTP 模式仅预留接口不实现，占位退出码 2。"""
    exit_code = cli.main(["serve"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "预留" in captured.out + captured.err


def test_trend_command_without_serial_reads_store_only(tmp_path, capsys):
    """B4：trend 只读库不强制 serial——无 --serial 也应可用。"""
    trend_payload = {"pkg": "tv.danmaku.bili", "metric": "mem_pss_mb", "runs": []}
    captured = {}

    class TrendStubKit(StubKit):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            captured["serial"] = kwargs.get("serial")

        def trend(self, pkg, metric, profile=None):
            return trend_payload

    exit_code = cli.main(
        ["trend", "tv.danmaku.bili", "--metric", "mem_pss_mb", "--adb", fake_adb(tmp_path)],
        kit_factory=lambda **kwargs: TrendStubKit(**kwargs),
    )

    assert exit_code == 0
    assert captured["serial"] is None  # 只读命令不再注入/探测设备 serial
    assert json.loads(capsys.readouterr().out) == trend_payload


def test_report_baseline_without_serial_judges_from_session_record(tmp_path, capsys):
    """B4：report --baseline 不再强制 serial；真实 PerfKit(serial=None) 仅判库。"""
    db, store = _make_report_fixture(tmp_path)
    store.record_baseline(
        name="bili-home-v3", metric_key="mem_pss_mb", value=180.0
    )
    store.close()

    exit_code = cli.main(
        [
            "report",
            "run-cli-judge",
            "--runs-dir",
            str(tmp_path / "runs"),
            "--db",
            str(db),
            "--baseline",
            "bili-home-v3",
            "--threshold",
            "mem_pss_mb:lt:220:fail",
            "--adb",
            fake_adb(tmp_path),
        ]
    )

    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"]["verdict"] == "passed"


# ---- ticket 12：collect --start-mode cold|warm ----

def test_collect_passes_start_mode_to_run_collect(tmp_path):
    kits = []

    exit_code = cli.main(
        [
            "collect",
            "com.sinosoft.trainapp",
            "--items",
            "CPU,Memory",
            "--start-mode",
            "cold",
            "--serial",
            "749622c9",
            "--adb",
            fake_adb(tmp_path),
        ],
        kit_factory=lambda **kwargs: kits.append(StubKit(**kwargs)) or kits[0],
    )

    assert exit_code == 0
    assert kits[0].calls[0]["start_mode"] == "cold"


def test_collect_without_start_mode_passes_none(tmp_path):
    kits = []

    cli.main(
        ["collect", "com.sinosoft.trainapp", "--serial", "749622c9", "--adb", fake_adb(tmp_path)],
        kit_factory=lambda **kwargs: kits.append(StubKit(**kwargs)) or kits[0],
    )

    assert kits[0].calls[0]["start_mode"] is None


def test_collect_rejects_unknown_start_mode(tmp_path):
    with pytest.raises(SystemExit):
        cli.main(
            [
                "collect",
                "com.sinosoft.trainapp",
                "--start-mode",
                "hot",
                "--serial",
                "749622c9",
                "--adb",
                fake_adb(tmp_path),
            ],
            kit_factory=lambda **kwargs: StubKit(**kwargs),
        )


# ---- ticket 14：collect --jank-threshold light:100,heavy:300,severe:700 ----

def test_collect_passes_jank_thresholds_to_run_collect(tmp_path):
    kits = []

    exit_code = cli.main(
        [
            "collect",
            "tv.danmaku.bili",
            "--items",
            "CPU,FPS",
            "--jank-threshold",
            "light:50,heavy:200,severe:500",
            "--serial",
            "749622c9",
            "--adb",
            fake_adb(tmp_path),
        ],
        kit_factory=lambda **kwargs: kits.append(StubKit(**kwargs)) or kits[0],
    )

    assert exit_code == 0
    assert kits[0].calls[0]["jank_thresholds_ms"] == {
        "light": 50.0,
        "heavy": 200.0,
        "severe": 500.0,
    }


def test_collect_without_jank_threshold_passes_none(tmp_path):
    kits = []

    cli.main(
        ["collect", "tv.danmaku.bili", "--serial", "749622c9", "--adb", fake_adb(tmp_path)],
        kit_factory=lambda **kwargs: kits.append(StubKit(**kwargs)) or kits[0],
    )

    assert kits[0].calls[0]["jank_thresholds_ms"] is None


def test_collect_rejects_malformed_jank_threshold(tmp_path):
    exit_code = cli.main(
        [
            "collect",
            "tv.danmaku.bili",
            "--jank-threshold",
            "light:50,heavy:20,severe:500",
            "--serial",
            "749622c9",
            "--adb",
            fake_adb(tmp_path),
        ],
        kit_factory=lambda **kwargs: StubKit(**kwargs),
    )

    assert exit_code == 1
