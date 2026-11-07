"""collect 的 --capture-* 参数：早失败、限额校验与透传语义。"""

from mobile_perf import cli
from mobile_perf.har_writer import CaptureOptions


def fake_adb(tmp_path):
    path = tmp_path / "adb.exe"
    path.write_bytes(b"x")
    return str(path)


class CaptureStubKit:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []

    def run_collect(self, **kwargs):
        self.calls.append(kwargs)
        return {"runId": kwargs.get("host_run_id") or "run-stub"}


def _run(tmp_path, *extra):
    kits = []

    def factory(**kwargs):
        kit = CaptureStubKit(**kwargs)
        kits.append(kit)
        return kit

    code = cli.main(
        [
            "collect",
            "tv.danmaku.bili",
            "--items",
            "CPU",
            "--serial",
            "749622c9",
            "--adb",
            fake_adb(tmp_path),
            *extra,
        ],
        kit_factory=factory,
    )
    return code, kits


def test_capture_har_alone_uses_decrypt_without_claiming_net_profile(tmp_path):
    code, kits = _run(tmp_path, "--capture-har")

    assert code == 0
    call = kits[0].calls[0]
    assert call["capture_har"] is True
    assert isinstance(call["capture_options"], CaptureOptions)
    assert call["capture_options"].enabled is True
    assert call["net_profile"] is None
    assert call["net_tier"] == "decrypt"


def test_capture_options_are_forwarded(tmp_path):
    code, kits = _run(
        tmp_path,
        "--capture-har",
        "--capture-har-bodies",
        "--capture-body-limit-bytes",
        "1234",
        "--capture-max-entries",
        "9",
        "--capture-max-bytes",
        "4096",
        "--capture-har-no-redact",
    )

    assert code == 0
    options = kits[0].calls[0]["capture_options"]
    assert options.capture_bodies is True
    assert options.body_limit_bytes == 1234
    assert options.max_entries == 9
    assert options.max_bytes == 4096
    assert options.redact is False


def test_capture_with_net_profile_keeps_explicit_profile(tmp_path):
    code, kits = _run(tmp_path, "--capture-har", "--net-profile", "weak")

    assert code == 0
    call = kits[0].calls[0]
    assert call["net_profile"] == "weak"
    assert call["net_tier"] == "decrypt"


def test_capture_detail_without_main_switch_is_rejected(tmp_path):
    code, kits = _run(tmp_path, "--capture-har-bodies")

    assert code == 1
    assert kits == []


def test_capture_explicit_default_value_without_switch_is_rejected(tmp_path):
    """即使显式给了与默认相同的值，细项也不能脱离主开关单独出现。"""
    code, kits = _run(tmp_path, "--capture-max-entries", "5000")

    assert code == 1
    assert kits == []


def test_capture_limit_must_be_positive(tmp_path):
    code, kits = _run(tmp_path, "--capture-har", "--capture-max-entries", "0")

    assert code == 1
    assert kits == []


def test_capture_with_explicit_passthrough_fails_early(tmp_path):
    code, kits = _run(tmp_path, "--capture-har", "--net-tier", "passthrough")

    assert code == 1
    assert kits == []


def test_without_capture_no_capture_kwargs_are_sent(tmp_path):
    """默认关闭：旧 kit 的 run_collect 签名不受影响（不传新增关键字）。"""
    code, kits = _run(tmp_path)

    assert code == 0
    call = kits[0].calls[0]
    assert "capture_har" not in call
    assert "capture_options" not in call
    assert call["net_tier"] == "passthrough"
