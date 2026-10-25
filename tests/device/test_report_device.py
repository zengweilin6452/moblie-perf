"""真机报告与趋势验证（`-m device` 门控；设备 749622c9 不在线则跳过且不计为通过）。

连续两轮 collect 后：run 目录产出 report.json + report.html 双产物且数据同源；
HTML 无外部资源引用（离线 ECharts）；trend 输出两条 run 的对比。
"""

import json
import os

import pytest

from mobile_perf.adb import bundled_adb_path, list_devices
from mobile_perf.kit import PerfKit
from mobile_perf.store import PerfStore

pytestmark = pytest.mark.device

DEVICE_SERIAL = os.environ.get("MOBILE_PERF_DEVICE_SERIAL", "749622c9")
TARGET_PACKAGE = os.environ.get("MOBILE_PERF_DEVICE_TARGET", "tv.danmaku.bili")
DURATION_S = 10.0
DEVICE_PROFILE = os.environ.get("MOBILE_PERF_DEVICE_PROFILE", "749622c9-device")


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


def _strip_vendor(html: str) -> str:
    begin = html.index("<!-- vendor:echarts BEGIN -->")
    end = html.index("<!-- vendor:echarts END -->")
    return html[:begin] + html[end:]


@pytest.fixture(scope="module")
def two_runs(device_available, tmp_path_factory):
    """连跑两轮采集，产出两个 run（真实端侧 SoloPi 采集）。"""
    workspace = tmp_path_factory.mktemp("perf-device")
    kit = PerfKit(
        serial=device_available,
        runs_dir=workspace / "runs",
        store=PerfStore(workspace / "data" / "mobile_perf.db"),
        device_profile=DEVICE_PROFILE,
    )
    reports = [
        kit.run_collect(
            target=TARGET_PACKAGE,
            items=["CPU", "Memory"],
            duration_s=DURATION_S,
            runs_dir=workspace / "runs",
        )
        for _ in range(2)
    ]
    return kit, workspace, reports


def test_collect_produces_offline_dual_artifacts(two_runs):
    kit, workspace, reports = two_runs

    for report in reports:
        run_dir = workspace / "runs" / report["runId"]
        on_disk = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
        assert on_disk == report
        assert on_disk["metricsSummary"], "真机采集应有聚合指标"
        html = (run_dir / "report.html").read_text(encoding="utf-8")
        assert "vendor:echarts BEGIN" in html
        outside_vendor = _strip_vendor(html)
        for forbidden in ("http://", "https://", "<script src", "<link "):
            assert forbidden not in outside_vendor, forbidden


def test_trend_across_two_device_runs(two_runs):
    kit, _, reports = two_runs

    trend = kit.trend(TARGET_PACKAGE, "cpu_global_pct")

    assert trend["profile"] == DEVICE_PROFILE
    assert trend["profileMismatch"] is False
    assert [r["runId"] for r in trend["runs"]] == [r["runId"] for r in reports]
    assert all(r["median"] is not None for r in trend["runs"])
