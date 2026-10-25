"""真机 doctor 巡检（`-m device` 门控；无设备跳过且不计为通过）。

验证 kit.ensure_ready() 在真实设备上的完整巡检闭环与幂等性：
健康设备两次巡检零自愈副作用。不主动制造断线——tcpip 自愈的恢复
路径由单元测试脚本化覆盖，真机上不做破坏性断连。
"""

import json
import os

import pytest

from mobile_perf.adb import bundled_adb_path, list_devices
from mobile_perf.keeper import default_call_device
from mobile_perf.kit import PerfKit

pytestmark = pytest.mark.device

DEVICE_SERIAL = os.environ.get("MOBILE_PERF_DEVICE_SERIAL", "749622c9")


def _device_online() -> bool:
    adb_path = bundled_adb_path()
    if not adb_path.is_file():
        return False
    return DEVICE_SERIAL in list_devices(str(adb_path))


@pytest.fixture(scope="module")
def device_available():
    if not _device_online():
        pytest.skip(f"真机 {DEVICE_SERIAL} 不在线，device 验证未做")
    return DEVICE_SERIAL


def test_ensure_ready_full_inspection_on_real_device(device_available):
    kit = PerfKit(
        adb_path=str(bundled_adb_path()),
        serial=device_available,
    )

    report = kit.ensure_ready()

    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    assert report.serial == device_available
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
    assert report.clock_offset_s is not None
    if report.ok:
        assert report.needs_attention is False
        assert report.healed is False


def test_ensure_ready_twice_is_idempotent_no_heal(device_available):
    adb_path = str(bundled_adb_path())
    recorded: list[list[str]] = []

    def recording_call(args, timeout_s=15.0):
        recorded.append(list(args))
        return default_call_device(adb_path)(args, timeout_s)

    kit = PerfKit(adb_path=adb_path, serial=device_available, device_call=recording_call)

    first = kit.ensure_ready()
    if not first.ok:
        pytest.skip(f"设备首巡未就绪（{first.as_dict()}），幂等验证未做")
    commands_after_first = list(recorded)
    second = kit.ensure_ready()

    assert second.ok is True
    assert second.healed is False
    assert recorded[len(commands_after_first):] == commands_after_first
