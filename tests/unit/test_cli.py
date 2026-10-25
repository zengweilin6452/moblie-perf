"""CLI 的单元测试：参数解析、serial 解析顺序、collect 端到端（注入假 kit）。"""

import json

from mobile_perf import adb, cli


def fake_adb(tmp_path):
    adb_path = tmp_path / "adb.exe"
    adb_path.write_bytes(b"x")
    return str(adb_path)


class StubKit:
    """替身 kit：记录 run_collect 调用并返回固定报告。"""

    def __init__(self, adb_path=None, serial=None, runs_dir=None):
        self.adb_path = adb_path
        self.serial = serial
        self.runs_dir = runs_dir
        self.calls = []

    def run_collect(self, *, target, items, duration_s, host_run_id=None, runs_dir=None):
        self.calls.append(
            {
                "target": target,
                "items": items,
                "duration_s": duration_s,
                "host_run_id": host_run_id,
                "runs_dir": runs_dir,
            }
        )
        return {"runId": host_run_id or "run-stub", "target": target}


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
