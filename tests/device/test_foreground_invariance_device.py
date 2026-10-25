"""前台不变性专项（Q16，ticket 11）：采集全链路执行前后被测应用前台不变。

探明路径：Harness perf-start 经 SoloPi Scheme 控制面启动录制
（solopi_ai.py invoke_scheme 的 am start -W），实测是否抢占前台由本测试
记录为证据；组件侧前台守卫（kit foreground_restore）在漂移时还原。
验收断言：ensure_ready 与 run_collect（含 start/stop/pull）之后，
被测应用前台包名与采集前一致；还原事件全部 restored=true。
"""

import time

import pytest

from mobile_perf.adb import bundled_adb_path, list_devices
from mobile_perf.keeper import parse_foreground_component, parse_foreground_package
from mobile_perf.kit import PerfKit
from mobile_perf.supplement import default_call_device

pytestmark = pytest.mark.device

DEVICE_SERIAL = "749622c9"
TARGET_PACKAGE = "tv.danmaku.bili"
COLLECT_DURATION_S = 10.0


def _device_online() -> bool:
    adb_path = bundled_adb_path()
    return adb_path.is_file() and DEVICE_SERIAL in list_devices(str(adb_path))


def _shell(adb_path, args, timeout_s=15.0):
    return default_call_device(str(adb_path))(
        ["-s", DEVICE_SERIAL, *args], timeout_s
    )


def _foreground(adb_path) -> tuple[str | None, str | None]:
    result = _shell(adb_path, ["shell", "dumpsys", "activity", "activities"])
    stdout = getattr(result, "stdout", "") or ""
    return parse_foreground_package(stdout), parse_foreground_component(stdout)


def _wait_foreground(adb_path, package, timeout_s=20.0) -> str | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        pkg, _ = _foreground(adb_path)
        if pkg == package:
            return pkg
        time.sleep(1.0)
    return None


@pytest.fixture(scope="module")
def device_available():
    if not _device_online():
        pytest.skip(f"真机 {DEVICE_SERIAL} 不在线，前台专项未做")
    return bundled_adb_path()


def test_collect_keeps_target_foreground(device_available, tmp_path):
    adb_path = device_available

    # 1. 把被测应用带到前台并等待稳定
    _shell(adb_path, ["shell", "monkey", "-p", TARGET_PACKAGE,
                      "-c", "android.intent.category.LAUNCHER", "1"], 30.0)
    assert _wait_foreground(adb_path, TARGET_PACKAGE) == TARGET_PACKAGE, (
        "前置失败：未能把被测应用带到前台"
    )
    before_pkg, before_component = _foreground(adb_path)

    kit = PerfKit(
        serial=DEVICE_SERIAL,
        runs_dir=tmp_path / "runs",
        db_path=tmp_path / "data" / "mobile_perf.db",
    )

    # 2. ensure_ready（doctor/keeper 巡检）不改变前台
    kit.ensure_ready()
    ready_pkg, _ = _foreground(adb_path)

    # 3. 完整采集闭环（start → 计时 → stop → pull）
    report = kit.run_collect(
        target=TARGET_PACKAGE,
        items=["CPU", "Memory"],
        duration_s=COLLECT_DURATION_S,
        host_run_id="fg-special-1",
        runs_dir=tmp_path / "runs",
    )

    # 4. 验收：前台状态不变
    after_pkg, after_component = _foreground(adb_path)
    fg_events = [e for e in report["events"] if e["kind"] == "foreground"]

    print(f"\n[前台专项] before={before_pkg} ({before_component})")
    print(f"[前台专项] after_ensure_ready={ready_pkg}")
    print(f"[前台专项] after_collect={after_pkg} ({after_component})")
    for event in fg_events:
        print(f"[前台专项] 守卫事件: {event['detail']}")

    assert ready_pkg == before_pkg, f"ensure_ready 改变了前台: {before_pkg} -> {ready_pkg}"
    assert all(
        event["detail"].get("restored") is True for event in fg_events
    ), f"存在还原失败的前台守卫事件: {fg_events}"
    assert after_pkg == before_pkg, (
        f"采集命令执行后前台改变: {before_pkg} -> {after_pkg}; "
        f"守卫事件: {[e['detail'] for e in fg_events]}"
    )
