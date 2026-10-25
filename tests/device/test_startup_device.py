"""真机启动时间采集（`-m device` 门控；无设备跳过且不计为通过）。

对目标 App 跑 cold/warm 采集闭环（ticket 12）：断言启动毫秒数入库、
`am start -W` 与 logcat Displayed 双路对照随事件记录、启动期资源曲线覆盖
启动时刻、报告双产物正常。目标包默认 com.sinosoft.trainapp，可用
MOBILE_PERF_DEVICE_TARGET 覆盖。
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
TARGET_PACKAGE = os.environ.get("MOBILE_PERF_DEVICE_TARGET", "com.sinosoft.trainapp")
DURATION_S = 12.0


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


def _collect(start_mode: str, tmp_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "mobile_perf",
            "collect",
            TARGET_PACKAGE,
            "--items",
            "CPU,Memory",
            "--duration",
            str(int(DURATION_S)),
            "--start-mode",
            start_mode,
            "--serial",
            DEVICE_SERIAL,
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


def _assert_startup_run(proc, tmp_path, mode, metric_key):
    assert proc.returncode == 0, (
        f"collect({mode}) 失败(exit={proc.returncode})\nstdout:\n{proc.stdout}\n"
        f"stderr:\n{proc.stderr}"
    )
    report = json.loads(proc.stdout)

    summary = report["metricsSummary"][metric_key]
    assert summary["unit"] == "ms"
    assert summary["source"] == "pc_orchestration"
    assert summary["sampleCount"] == 1
    assert summary["median"] > 0

    (event,) = [e for e in report["events"] if e["kind"] == "startup"]
    detail = event["detail"]
    assert detail["mode"] == mode
    assert detail["metricKey"] == metric_key
    assert detail["ms"] == summary["median"]
    # 双路互证如实记录：Displayed 可得时有偏差，不可得时字段仍存在（None）
    assert "displayedMs" in detail
    assert "deviationMs" in detail
    # warm 拉起任务仅被带到前台时无 TotalTime，回退 WaitTime 并如实标注
    assert detail["msSource"] in ("total_time", "wait_time")

    # 曲线覆盖启动时刻：至少一条指标曲线在启动点前后都有样本
    elapsed = detail["elapsedS"]
    covered = [
        key
        for key, curve in report["series"].items()
        if curve.get("points")
        and any(p[0] <= elapsed for p in curve["points"])
        and any(p[0] >= elapsed for p in curve["points"])
    ]
    assert covered, f"没有任何曲线覆盖启动点 elapsedS={elapsed}"

    run_dir = tmp_path / "runs" / report["runId"]
    assert (run_dir / "report.json").is_file()
    assert (run_dir / "report.html").is_file()
    persisted = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    assert persisted["startup"]["metricKey"] == metric_key
    assert (run_dir / "report.html").read_text(encoding="utf-8").count("启动时间 / Startup") >= 1
    return report, detail


def test_collect_cold_start_records_startup_metric(device_available, tmp_path):
    proc = _collect("cold", tmp_path)

    report, detail = _assert_startup_run(proc, tmp_path, "cold", "app_cold_start_ms")
    assert detail["launchState"] in ("COLD", None)  # 旧 Android 可能无 LaunchState
    assert detail["error"] is None


def test_collect_warm_start_records_startup_metric(device_available, tmp_path):
    proc = _collect("warm", tmp_path)

    report, detail = _assert_startup_run(proc, tmp_path, "warm", "app_warm_start_ms")
    assert detail["error"] is None
