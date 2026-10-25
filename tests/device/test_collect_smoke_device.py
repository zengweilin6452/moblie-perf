"""真机 collect 冒烟（`-m device` 门控；无设备跳过且不计为通过）。

以真实 Harness 子进程跑完整闭环：perf-start → 计时 → perf-stop --output →
runs/<runId>/ 产物落盘。items 选 CPU,Memory：FPS 每次启动都弹告知框，
无人值守会卡 start 确认，不做冒烟默认项。
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
SMOKE_DURATION_S = 10.0


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


def test_collect_smoke_end_to_end(device_available, tmp_path):
    runs_dir = tmp_path / "runs"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "mobile_perf",
            "collect",
            TARGET_PACKAGE,
            "--items",
            "CPU,Memory",
            "--duration",
            str(int(SMOKE_DURATION_S)),
            "--serial",
            device_available,
            "--runs-dir",
            str(runs_dir),
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
    run_dir = runs_dir / report["runId"]

    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["target"] == TARGET_PACKAGE
    assert meta["status"] == "stopped"
    assert meta["durationS"] >= SMOKE_DURATION_S - 1.0
    assert meta["soloSessionId"]

    artifacts = list((run_dir / "artifacts").rglob("*.csv"))
    assert artifacts, "pull 后应存在性能 CSV"
    assert all(path.stat().st_size > 0 for path in artifacts)

    persisted_report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    assert persisted_report["runId"] == report["runId"]
    assert persisted_report["soloSessionId"] == meta["soloSessionId"]
