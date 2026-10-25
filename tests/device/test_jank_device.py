"""真机卡顿分级采集（`-m device` 门控；无设备跳过且不计为通过）。

对目标 App 跑一轮含 FPS 的采集闭环（ticket 14）：断言三个分级 canonical key
入库、数值合理（计数为非负整数且不超过采样窗口数），报告含分级小节与阈值。
注意：FPS 采集会弹 SoloPi 告知框，需在采集开始后人工点确认；本用例给足超时，
超时失败会如实报错而不是静默通过。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mobile_perf.adb import bundled_adb_path, list_devices

pytestmark = pytest.mark.device

DEVICE_SERIAL = os.environ.get("MOBILE_PERF_DEVICE_SERIAL", "749622c9")
TARGET_PACKAGE = os.environ.get("MOBILE_PERF_DEVICE_TARGET", "tv.danmaku.bili")
DURATION_S = 20.0
JANK_KEYS = ("app_jank_light_count", "app_jank_heavy_count", "app_jank_severe_count")


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


def test_collect_records_jank_grades_from_real_device(device_available, tmp_path):
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "mobile_perf",
            "collect",
            TARGET_PACKAGE,
            "--items",
            "CPU,FPS,Memory",
            "--duration",
            str(int(DURATION_S)),
            "--serial",
            device_available,
            "--runs-dir",
            str(tmp_path / "runs"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert proc.returncode == 0, (
        f"collect 失败(exit={proc.returncode})\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    report = json.loads(proc.stdout)
    summary = report["metricsSummary"]

    window_count = summary["frame_delay_max_ms"]["sampleCount"]
    assert window_count > 0
    counts = {}
    for key in JANK_KEYS:
        entry = summary[key]
        assert entry["unit"] == "次"
        assert entry["source"] == "solo_pi"
        assert entry["sampleCount"] == 1
        value = entry["max"]
        assert value >= 0
        assert value == int(value)
        assert value <= window_count
        counts[key] = int(value)
    # 分桶互斥：三档之和不超过采样窗口总数
    assert sum(counts.values()) <= window_count

    assert report["jank"]["sourceMetric"] == "frame_delay_max_ms"
    assert report["jank"]["granularity"] == "per_sampling_window"
    assert report["jank"]["thresholdsMs"] == {"light": 100.0, "heavy": 300.0, "severe": 700.0}

    run_dir = tmp_path / "runs" / report["runId"]
    persisted = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    assert persisted["metricsSummary"]["app_jank_severe_count"]["max"] == counts[
        "app_jank_severe_count"
    ]
    assert "卡顿分级 / Jank grading" in (run_dir / "report.html").read_text(encoding="utf-8")
