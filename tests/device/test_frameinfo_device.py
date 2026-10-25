"""逐帧采集真机门控（ticket 15）：gfxinfo framestats 在真机上确实可用。

无设备自动跳过（`-m device` 不计为通过）。验证三件事：
① 真机窗口内有帧且耗时量级合理（解析口径正确）；② 逐帧统计可入库；
③ 不存在的包返回空而不是抛错或造假。目标包默认 com.sinosoft.trainapp，
可用 MOBILE_PERF_DEVICE_TARGET 覆盖。
"""

import os

import pytest

from mobile_perf import frameinfo
from mobile_perf.adb import bundled_adb_path, default_call_device, list_devices

pytestmark = pytest.mark.device

DEVICE_SERIAL = os.environ.get("MOBILE_PERF_DEVICE_SERIAL", "749622c9")
TARGET_PACKAGE = os.environ.get("MOBILE_PERF_DEVICE_TARGET", "com.sinosoft.trainapp")


def _device_online() -> bool:
    adb_path = bundled_adb_path()
    if not adb_path.is_file():
        return False
    return DEVICE_SERIAL in list_devices(str(adb_path))


pytestmark = [pytest.mark.device, pytest.mark.skipif(not _device_online(), reason="设备不在线")]


@pytest.fixture(scope="module")
def caller():
    return default_call_device(str(bundled_adb_path()))


def _shell(caller, *args):
    return caller(["-s", DEVICE_SERIAL, "shell", *args], 20.0)


def test_framestats_available_and_frames_have_plausible_durations(caller):
    _shell(caller, "am", "start", "-n", f"{TARGET_PACKAGE}/.MainActivity")
    _shell(caller, "dumpsys", "gfxinfo", TARGET_PACKAGE, "reset")
    for _ in range(3):
        _shell(caller, "input", "swipe", "540", "1600", "540", "500", "300")
    result = _shell(caller, "dumpsys", "gfxinfo", TARGET_PACKAGE, "framestats")

    samples = frameinfo.parse_framestats(result.stdout or "")
    if not samples:
        pytest.skip(f"{TARGET_PACKAGE} 本窗口无帧（屏幕熄灭或已退后台）")

    stats = frameinfo.frame_stats(samples)
    assert stats["count"] == len(samples)
    assert all(0 <= sample.duration_ms < 5000 for sample in samples)
    assert stats["p50"] > 0
    assert 0 <= stats["jankRatioPct"] <= 100


def test_framestats_aggregates_are_ingestible(caller):
    _shell(caller, "dumpsys", "gfxinfo", TARGET_PACKAGE, "reset")
    _shell(caller, "input", "swipe", "540", "1600", "540", "500", "300")
    result = _shell(caller, "dumpsys", "gfxinfo", TARGET_PACKAGE, "framestats")

    samples = frameinfo.parse_framestats(result.stdout or "")
    if not samples:
        pytest.skip(f"{TARGET_PACKAGE} 本窗口无帧")
    rows = frameinfo.aggregate_from_samples(samples)

    assert [row["metric_key"] for row in rows] == list(frameinfo.STAT_METRIC_KEYS)
    assert all(row["unit"] for row in rows)
    assert all(row["sample_count"] == 1 for row in rows)


def test_framestats_absent_package_returns_empty(caller):
    result = _shell(caller, "dumpsys", "gfxinfo", "com.mobileperf.absent.pkg", "framestats")

    assert frameinfo.parse_framestats(result.stdout or "") == []
