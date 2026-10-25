"""巡检（Keeper）单元测试：决策纯函数吃 stdout 样本、Keeper.run 走假 adb 调用、不碰真机。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from mobile_perf import keeper as keeper_module
from mobile_perf.gate import FileLockGate
from mobile_perf.keeper import (
    BATTERY_DEGRADED_ITEMS,
    CheckResult,
    Keeper,
    ReadinessReport,
    default_dialog_probe,
    evaluate_clock_offset,
    parse_accessibility_bound,
    parse_battery_charging,
    parse_config_get_response,
    parse_devices_stdout,
    parse_foreground_package,
    parse_wlan_ip,
)
from mobile_perf.kit import PerfKit
from mobile_perf.store import PerfStore

ACCESSIBILITY_SERVICE = "event.accessibility.AccessibilityServiceImpl"

ACCESSIBILITY_BOUND_SAMPLE = """ACCESSIBILITY MANAGER (dumpsys accessibility)

User state:{attributes: {flags}, feedbackTypes:8191, ...}
  bound services:
    Service{label='SoloPi', id={0 ... com.alipay.hulu/""" + ACCESSIBILITY_SERVICE + """}
"""

# 真机 749622c9（Android 12）实测格式：Bound 条目只有 label 无组件名，
# 组件名出现在 Enabled services 行，段名首字母大写。
ACCESSIBILITY_BOUND_REAL_DEVICE_SAMPLE = (
    "USER STATE 0 ...\n"
    "     Bound services:{Service[label=SoloPi, feedbackType[FEEDBACK_SPOKEN],"
    " capabilities=11, ...]}\n"
    "     Enabled services:{{com.alipay.hulu/com.alipay.hulu.shared."
    + ACCESSIBILITY_SERVICE
    + "}\n"
    "     Binding services:{}\n"
    "     Crashed services:{}\n"
)

ACCESSIBILITY_FAKE_ENABLED_SAMPLE = """ACCESSIBILITY MANAGER (dumpsys accessibility)

User state:{attributes: {flags}, feedbackTypes:8191, ...}
  bound services:
"""

# 真机假开启特征：Bound services 为空、Enabled services 仍列着 SoloPi 服务。
ACCESSIBILITY_FAKE_ENABLED_REAL_DEVICE_SAMPLE = """USER STATE 0 ...
     Bound services:{}
     Enabled services:{{com.alipay.hulu/com.alipay.hulu.shared.""" + ACCESSIBILITY_SERVICE + """}
     Binding services:{}
"""

ACCESSIBILITY_OTHER_SERVICE_SAMPLE = """ACCESSIBILITY MANAGER (dumpsys accessibility)

User state:{attributes: ...}
  bound services:
    Service{label='TalkBack', id={0 ... com.google.android.marvin.talkback/...TalkBackService}}
"""

BATTERY_DISCHARGING_SAMPLE = """Current Battery Service state:
  AC powered: false
  USB powered: false
  Wireless powered: false
  status: 4
  level: 87
  temperature: 285
"""

BATTERY_CHARGING_SAMPLE = """Current Battery Service state:
  AC powered: false
  USB powered: true
  Wireless powered: false
  status: 2
  level: 60
  temperature: 301
"""

FOREGROUND_SAMPLE = """ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)
  mResumedActivity: ActivityRecord{123456 u0 tv.danmaku.bili/.ui.MainActivity t42}
"""

WLAN_ROUTE_SAMPLE = """10.0.0.0/24 dev wlan0 proto kernel scope link src 10.0.0.23
default via 10.0.0.1 dev wlan0
"""


def make_result(stdout: str, returncode: int = 0):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


# ---------- 纯决策函数：给 stdout 样本断言判定 ----------


def test_parse_battery_charging_detects_usb_powered():
    assert parse_battery_charging(BATTERY_CHARGING_SAMPLE) is True


def test_parse_battery_charging_discharging_returns_false():
    assert parse_battery_charging(BATTERY_DISCHARGING_SAMPLE) is False


def test_parse_battery_charging_unparseable_returns_none():
    assert parse_battery_charging("error: device offline") is None


def test_parse_accessibility_bound_returns_true():
    assert parse_accessibility_bound(ACCESSIBILITY_BOUND_SAMPLE, ACCESSIBILITY_SERVICE) is True


def test_parse_accessibility_fake_enabled_returns_false():
    """Bound 段存在但为空：MIUI 假开启特征。"""
    assert (
        parse_accessibility_bound(ACCESSIBILITY_FAKE_ENABLED_SAMPLE, ACCESSIBILITY_SERVICE)
        is False
    )


def test_parse_accessibility_other_service_bound_returns_false():
    assert (
        parse_accessibility_bound(ACCESSIBILITY_OTHER_SERVICE_SAMPLE, ACCESSIBILITY_SERVICE)
        is False
    )


def test_parse_accessibility_unparseable_returns_none():
    assert parse_accessibility_bound("", ACCESSIBILITY_SERVICE) is None


def test_parse_accessibility_real_device_bound_format_returns_true():
    assert (
        parse_accessibility_bound(ACCESSIBILITY_BOUND_REAL_DEVICE_SAMPLE, ACCESSIBILITY_SERVICE)
        is True
    )


def test_parse_accessibility_real_device_fake_enabled_returns_false():
    assert (
        parse_accessibility_bound(
            ACCESSIBILITY_FAKE_ENABLED_REAL_DEVICE_SAMPLE, ACCESSIBILITY_SERVICE
        )
        is False
    )


def test_parse_foreground_package_extracts_resumed_activity():
    assert parse_foreground_package(FOREGROUND_SAMPLE) == "tv.danmaku.bili"


def test_parse_foreground_package_unreadable_returns_none():
    assert parse_foreground_package("error: unknown command") is None


def test_parse_wlan_ip_extracts_src_address():
    assert parse_wlan_ip(WLAN_ROUTE_SAMPLE) == "10.0.0.23"


def test_parse_wlan_ip_absent_returns_none():
    assert parse_wlan_ip("default via 10.0.0.1 dev rmnet0") is None


def test_parse_devices_stdout_lists_online_serials():
    stdout = "List of devices attached\n749622c9\tdevice\nemulator-5554\toffline\n"
    assert parse_devices_stdout(stdout) == ["749622c9"]


def test_evaluate_clock_offset_warns_over_two_seconds():
    verdict = evaluate_clock_offset(device_epoch_s=1000_000.0, host_epoch_s=1000_005.0)
    assert verdict["offsetS"] == pytest.approx(5.0)
    assert verdict["warn"] is True


def test_evaluate_clock_offset_within_threshold_not_warn():
    verdict = evaluate_clock_offset(device_epoch_s=1000_000.0, host_epoch_s=1000_001.5)
    assert verdict["warn"] is False


# ---------- Keeper.run：假 adb 调用脚本化巡检流程 ----------


class FakeDevice:
    """脚本化 adb 调用替身：记录全部命令，按当前状态返回应答。"""

    def __init__(
        self,
        *,
        online_serials,
        doctor_success=True,
        battery=BATTERY_DISCHARGING_SAMPLE,
        accessibility=ACCESSIBILITY_BOUND_SAMPLE,
        serial="749622c9",
        heal_restores=True,
    ):
        self.serial = serial
        self.heal_restores = heal_restores
        self.online_serials = online_serials
        self.doctor_success = doctor_success
        self.battery = battery
        self.accessibility = accessibility
        self.commands: list[list[str]] = []
        self.doctor_calls = 0

    def call(self, args, timeout_s=15.0):
        self.commands.append(list(args))
        joined = " ".join(args)
        if joined.startswith("devices"):
            online = "".join(f"{s}\tdevice\n" for s in self.online_serials)
            return make_result(f"List of devices attached\n{online}")
        if joined.startswith("connect") or " tcpip " in f" {joined} ":
            # 自愈补建成功：设备重新上线，doctor 也随之恢复（可注入覆盖为失败）
            if self.heal_restores:
                self.online_serials = [self.serial]
                self.doctor_success = True
            return make_result("", returncode=0)
        if "accessibility" in joined:
            return make_result(self.accessibility)
        if "battery" in joined:
            return make_result(self.battery)
        if "activity" in joined:
            return make_result(FOREGROUND_SAMPLE)
        if "date" in joined:
            return make_result("1760000000\n")
        if "ip route" in joined:
            return make_result(WLAN_ROUTE_SAMPLE)
        return make_result("", returncode=0)

    def doctor(self):
        self.doctor_calls += 1
        if self.doctor_success:
            return SimpleNamespace(success=True, data={"executionProfile": "host"})
        return SimpleNamespace(success=False, data={"failedChecks": ["ready"]})

    @property
    def healed_commands(self) -> list[list[str]]:
        return [c for c in self.commands if "tcpip" in c or "connect" in c]


def make_keeper(fake: FakeDevice, **kwargs) -> Keeper:
    defaults = dict(
        adb_path="adb-fake",
        serial="749622c9",
        call_device=fake.call,
        run_doctor=fake.doctor,
        now=lambda: 1760000001.0,
    )
    defaults.update(kwargs)
    return Keeper(**defaults)


def test_healthy_device_no_heal_and_report_ok():
    fake = FakeDevice(online_serials=["749622c9"])
    report = make_keeper(fake).run()

    assert report.ok is True
    assert report.needs_attention is False
    assert report.healed is False
    assert fake.healed_commands == []
    assert fake.doctor_calls == 1
    names = {c.name for c in report.checks}
    assert {
        "connection",
        "harness_doctor",
        "accessibility",
        "battery",
        "foreground",
        "clock_offset",
        "dialog_probe",
    } <= names
    dialog = next(c for c in report.checks if c.name == "dialog_probe")
    assert dialog.status == "skipped"


def test_healthy_device_rerun_is_idempotent():
    fake = FakeDevice(online_serials=["749622c9"])
    keeper_instance = make_keeper(fake)

    first = keeper_instance.run()
    commands_after_first = list(fake.commands)
    second = keeper_instance.run()

    assert first.ok and second.ok
    assert fake.healed_commands == []
    # 两次巡检的命令序列一致：重复执行不引入额外副作用
    assert fake.commands[len(commands_after_first):] == commands_after_first


def test_doctor_failure_triggers_tcpip_heal_then_retry():
    fake = FakeDevice(online_serials=[], doctor_success=False)
    report = make_keeper(fake).run()

    assert report.healed is True
    assert report.ok is True
    joined = [" ".join(c) for c in fake.healed_commands]
    assert "-s 749622c9 tcpip 5555" in joined
    assert "connect 10.0.0.23:5555" in joined
    assert fake.doctor_calls == 2  # 自愈后重试一次
    assert any(e["kind"] == "tcpip_heal" for e in report.events)


def test_heal_failure_reports_not_ok_honestly():
    fake = FakeDevice(
        online_serials=[], doctor_success=False, heal_restores=False
    )
    # 自愈命令发出但设备始终不在线：如实记录失败，报告 not ok
    report = make_keeper(fake).run()

    assert report.ok is False
    assert any(c.name == "connection" and c.status == "fail" for c in report.checks)
    heal_events = [e for e in report.events if e["kind"] == "tcpip_heal"]
    assert heal_events and heal_events[0]["detail"]["restored"] is False


def test_tcpip_serial_reconnects_without_tcpip_command():
    fake = FakeDevice(online_serials=[], serial="192.168.1.8:5555")
    report = make_keeper(fake, serial="192.168.1.8:5555").run()

    joined = [" ".join(c) for c in fake.healed_commands]
    assert joined == ["connect 192.168.1.8:5555"]
    assert report.ok is True


def test_accessibility_fake_open_marks_needs_attention_and_event():
    fake = FakeDevice(
        online_serials=["749622c9"], accessibility=ACCESSIBILITY_FAKE_ENABLED_SAMPLE
    )
    report = make_keeper(fake).run()

    assert report.ok is False
    assert report.needs_attention is True
    events = [e for e in report.events if e["kind"] == "accessibility_needs_attention"]
    assert events and events[0]["detail"]["service"] == ACCESSIBILITY_SERVICE


def test_charging_degrades_battery_items_only():
    fake = FakeDevice(online_serials=["749622c9"], battery=BATTERY_CHARGING_SAMPLE)
    report = make_keeper(fake).run()

    assert report.ok is True  # 采集仍可进行，仅 Battery/温度降级
    assert report.degraded_items == BATTERY_DEGRADED_ITEMS
    battery = next(c for c in report.checks if c.name == "battery")
    assert battery.status == "warn"
    assert battery.detail["charging"] is True


def test_clock_offset_over_threshold_emits_warning_event():
    fake = FakeDevice(online_serials=["749622c9"])
    report = make_keeper(fake, now=lambda: 1760000003.0).run()

    assert report.clock_offset_s == pytest.approx(3.0)
    clock = next(c for c in report.checks if c.name == "clock_offset")
    assert clock.status == "warn"
    assert any(e["kind"] == "clock_offset" for e in report.events)


def test_report_as_dict_shape():
    fake = FakeDevice(online_serials=["749622c9"])
    report = make_keeper(fake).run()
    data = report.as_dict()

    assert data["serial"] == "749622c9"
    assert data["ok"] is True
    assert data["needsAttention"] is False
    assert data["healed"] is False
    assert data["degradedItems"] == []
    assert data["foregroundPackage"] == "tv.danmaku.bili"
    assert isinstance(data["checks"], list) and data["checks"]


# ---------- PerfKit.ensure_ready：短事务借用 + 幂等可重入 ----------


class RecordingGate(FileLockGate):
    """记录借用目的的 gate（沿用 Windows 文件锁协议）。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.borrows: list[tuple[str, str]] = []

    def borrow(self, serial, *, purpose, timeout_s=10.0):
        self.borrows.append((serial, purpose))
        return super().borrow(serial, purpose=purpose, timeout_s=timeout_s)


def fake_adb_file(tmp_path: Path) -> str:
    adb = tmp_path / "adb.exe"
    adb.write_bytes(b"x")
    return str(adb)


def make_kit(tmp_path, fake: FakeDevice, **kwargs):
    defaults = dict(
        adb_path=fake_adb_file(tmp_path),
        serial="749622c9",
        runs_dir=tmp_path / "runs",
        store=PerfStore(tmp_path / "data" / "mobile_perf.db"),
        gate=RecordingGate(tmp_path / "gate-leases"),
        device_call=fake.call,
        doctor_fn=fake.doctor,
        # 桩探针：既有 kit 用例不触真实控制面 HTTP；默认接线见 default-wiring 用例
        dialog_probe=lambda: CheckResult("dialog_probe", "skipped", {"reason": "unit"}),
    )
    defaults.update(kwargs)
    return PerfKit(**defaults)


def test_ensure_ready_borrows_gate_per_action_and_releases(tmp_path):
    fake = FakeDevice(online_serials=["749622c9"])
    kit = make_kit(tmp_path, fake)

    report = kit.ensure_ready()

    assert isinstance(report, ReadinessReport)
    assert report.ok is True
    assert kit.last_readiness is report
    gate = kit.gate
    assert gate.held is False  # 短事务：巡检结束设备权已归还
    assert gate.borrows and all(p == "mobile_perf.keeper" for _, p in gate.borrows)


def test_ensure_ready_reentrant_calls_are_safe(tmp_path):
    fake = FakeDevice(online_serials=["749622c9"])
    kit = make_kit(tmp_path, fake)

    first = kit.ensure_ready()
    second = kit.ensure_ready()

    assert first.ok and second.ok
    assert fake.healed_commands == []  # 重入不触发自愈副作用


def test_ensure_ready_injects_dialog_probe_result(tmp_path):
    fake = FakeDevice(online_serials=["749622c9"])
    kit = make_kit(
        tmp_path,
        fake,
        dialog_probe=lambda: CheckResult(
            name="dialog_probe", status="ok", detail={"suppressed": True}
        ),
    )

    report = kit.ensure_ready()

    dialog = next(c for c in report.checks if c.name == "dialog_probe")
    assert dialog.status == "ok"
    assert dialog.detail["suppressed"] is True


def test_ensure_ready_unparseable_accessibility_is_fail(tmp_path):
    """dumpsys 无输出（设备异常）：不猜，如实 fail。"""
    fake = FakeDevice(online_serials=["749622c9"], accessibility="")
    kit = make_kit(tmp_path, fake)

    report = kit.ensure_ready()

    accessibility = next(c for c in report.checks if c.name == "accessibility")
    assert accessibility.status == "fail"
    assert report.ok is False


# ---------- 弹窗开关探针：控制面读回（票 06 自编译 App 通道） ----------

CONFIG_GET_TRUE = (
    '{"success":true,"config":{"key":"KEY_SUPPRESS_INFO_DIALOG","type":"boolean",'
    '"description":"告知型权限弹窗本机不再提示","default":false,"writable":true,'
    '"sensitive":false,"redacted":false,"value":true}}'
)


def test_parse_config_get_response_true():
    assert parse_config_get_response(CONFIG_GET_TRUE) is True


def test_parse_config_get_response_false():
    body = CONFIG_GET_TRUE.replace('"value":true', '"value":false')
    assert parse_config_get_response(body) is False


def test_parse_config_get_response_unparseable_returns_none():
    assert parse_config_get_response("not json") is None
    assert parse_config_get_response('{"success":false,"errorCode":"unsupported_config"}') is None
    assert parse_config_get_response("") is None


def test_default_dialog_probe_ok_when_suppressed():
    commands = []

    def call(args, timeout_s=15.0):
        commands.append(list(args))
        return make_result("", returncode=0)

    probe = default_dialog_probe(call, "749622c9", fetch=lambda url, t: CONFIG_GET_TRUE)

    result = probe()

    assert result.status == "ok"
    assert result.detail == {"suppressed": True, "port": 23342}
    assert commands == [["-s", "749622c9", "forward", "tcp:23342", "tcp:23342"]]


def test_default_dialog_probe_warns_when_switch_off():
    probe = default_dialog_probe(
        lambda args, timeout_s=15.0: make_result(""),
        "749622c9",
        fetch=lambda url, t: CONFIG_GET_TRUE.replace('"value":true', '"value":false'),
    )

    result = probe()

    assert result.status == "warn"
    assert result.detail["suppressed"] is False


def test_default_dialog_probe_fail_when_http_unreachable():
    def fetch(url, t):
        raise OSError("connection refused")

    probe = default_dialog_probe(
        lambda args, timeout_s=15.0: make_result(""), "749622c9", fetch=fetch
    )

    result = probe()

    assert result.status == "fail"
    assert "控制面 HTTP 不可达" in result.detail["error"]


def test_default_dialog_probe_fail_when_forward_fails():
    probe = default_dialog_probe(
        lambda args, timeout_s=15.0: make_result("error: device offline", returncode=1),
        "749622c9",
        fetch=lambda url, t: CONFIG_GET_TRUE,
    )

    result = probe()

    assert result.status == "fail"
    assert result.detail["error"] == "adb forward 失败"


def test_kit_default_wiring_uses_control_channel(tmp_path, monkeypatch):
    """kit 未注入探针时默认接线：forward 经 gate 借用，HTTP 读回走 keeper 模块。"""
    fake = FakeDevice(online_serials=["749622c9"])
    seen_urls: list[str] = []

    def fake_fetch(url, timeout_s):
        seen_urls.append(url)
        return CONFIG_GET_TRUE

    monkeypatch.setattr(keeper_module, "_urllib_get", fake_fetch)
    kit = make_kit(tmp_path, fake)
    kit._dialog_probe = None  # 摘掉桩，走默认接线

    report = kit.ensure_ready()

    dialog = next(c for c in report.checks if c.name == "dialog_probe")
    assert dialog.status == "ok"
    assert dialog.detail["suppressed"] is True
    assert seen_urls == [
        "http://127.0.0.1:23342/scheme/config?action=get&key=KEY_SUPPRESS_INFO_DIALOG"
    ]
    assert ["-s", "749622c9", "forward", "tcp:23342", "tcp:23342"] in fake.commands
