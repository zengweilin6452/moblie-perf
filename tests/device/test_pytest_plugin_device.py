"""pytest 钩子真机冒烟（`-m device` + `--mobile-perf` 双门控）。

验证默认 kit（随包 adb + bundled Harness 路径）下 perf.session 全链路
可用：真机采集 → finalize → 报告。需真机在线并显式传
--mobile-perf --mobile-perf-serial <serial>（建议同时传
--mobile-perf-runs-dir 指向临时目录），否则跳过、不计为通过。
"""

import pytest

from mobile_perf.adb import bundled_adb_path, list_devices

pytestmark = pytest.mark.device

DEVICE_SERIAL = "749622c9"
TARGET_PACKAGE = "tv.danmaku.bili"


def test_perf_fixture_collects_on_device(request, perf):
    if not request.config.getoption("--mobile-perf"):
        pytest.skip("未开 --mobile-perf，钩子真机冒烟未做")
    if DEVICE_SERIAL not in list_devices(str(bundled_adb_path())):
        pytest.skip(f"真机 {DEVICE_SERIAL} 不在线，钩子真机冒烟未做")

    with perf.session(
        TARGET_PACKAGE, items=["CPU", "Memory"], host_run_id="plugin-smoke-1"
    ) as scope:
        scope.mark("smoke_window")

    assert scope.report["runId"] == "plugin-smoke-1"
    assert scope.report["metricsSummary"], "真机采集应产出指标聚合"
    assert scope.report["marks"][-1]["label"] == "smoke_window"
