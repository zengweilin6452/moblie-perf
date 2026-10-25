"""dialog.py（权限弹窗代点降级备选，默认关）的单元测试：纯决策函数 + 注入通道。"""

from types import SimpleNamespace

from mobile_perf import dialog

WINDOW_DUMP_WITH_DIALOG = (
    "  mCurrentFocus: Window{4b0c3f u0 com.alipay.hulu/"
    "com.alipay.hulu.common.scheme.PermissionDialogActivity}\n"
)
WINDOW_DUMP_NORMAL = (
    "  mCurrentFocus: Window{4b0c3f u0 tv.danmaku.bili/tv.danmaku.bili.ui.MainActivity}\n"
)


def test_permission_dialog_detection_is_pure_text_matching():
    assert dialog.is_permission_dialog_in_foreground(WINDOW_DUMP_WITH_DIALOG) is True
    assert dialog.is_permission_dialog_in_foreground(WINDOW_DUMP_NORMAL) is False
    assert dialog.is_permission_dialog_in_foreground("") is False
    assert dialog.is_permission_dialog_in_foreground(None) is False


def test_decide_dismiss_never_taps_without_coords():
    """坐标未配置（默认）→ 不启用：即使弹窗在前台也不产生动作。"""
    assert dialog.decide_dismiss(WINDOW_DUMP_WITH_DIALOG, coords=None) == {
        "enabled": False,
        "present": True,
        "tapped": False,
    }
    assert dialog.decide_dismiss(WINDOW_DUMP_NORMAL, coords=(500, 1200)) == {
        "enabled": True,
        "present": False,
        "tapped": False,
    }
    assert dialog.decide_dismiss(WINDOW_DUMP_WITH_DIALOG, coords=(500, 1200)) == {
        "enabled": True,
        "present": True,
        "tapped": True,
    }


def test_build_tap_args_targets_known_button_position():
    assert dialog.build_tap_args("749622c9", (540, 1210)) == [
        "-s",
        "749622c9",
        "shell",
        "input",
        "tap",
        "540",
        "1210",
    ]


def test_probe_disabled_by_default_coords_never_touches_device():
    calls = []

    def call(args, timeout_s=15.0):
        calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout=WINDOW_DUMP_WITH_DIALOG, stderr="")

    probe = dialog.make_dialog_dismiss_probe(call, "749622c9", coords=None)
    verdict = probe()

    assert verdict["enabled"] is False
    assert calls == []  # 默认关：不发起任何设备命令


def test_probe_taps_known_button_when_dialog_present_and_enabled():
    calls = []

    def call(args, timeout_s=15.0):
        calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout=WINDOW_DUMP_WITH_DIALOG, stderr="")

    probe = dialog.make_dialog_dismiss_probe(call, "749622c9", coords=(540, 1210))
    verdict = probe()

    assert verdict == {
        "enabled": True,
        "present": True,
        "tapped": True,
        "coords": [540, 1210],
    }
    assert calls == [
        ["-s", "749622c9", "shell", "dumpsys", "window"],
        ["-s", "749622c9", "shell", "input", "tap", "540", "1210"],
    ]


def test_probe_reports_present_without_tap_when_no_dialog():
    calls = []

    def call(args, timeout_s=15.0):
        calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout=WINDOW_DUMP_NORMAL, stderr="")

    probe = dialog.make_dialog_dismiss_probe(call, "749622c9", coords=(540, 1210))
    verdict = probe()

    assert verdict["tapped"] is False
    assert calls == [["-s", "749622c9", "shell", "dumpsys", "window"]]


def test_kit_attaches_dialog_dismiss_event_only_when_explicitly_passed(tmp_path):
    """默认关：不传不产生事件；显式传入才挂上 ensure_ready 巡检并记事件。"""
    from tests.unit.test_kit import FakeHarnessBackend, make_kit

    kit = make_kit(tmp_path, FakeHarnessBackend())
    report = kit.ensure_ready()
    assert not any(e["kind"] == "dialog_dismiss" for e in report.events)

    dismiss_record = {"enabled": True, "present": True, "tapped": True}
    kit2 = make_kit(
        tmp_path,
        FakeHarnessBackend(),
        dialog_dismiss=lambda: dismiss_record,
    )
    report2 = kit2.ensure_ready()
    (event,) = [e for e in report2.events if e["kind"] == "dialog_dismiss"]
    assert event["detail"]["tapped"] is True
