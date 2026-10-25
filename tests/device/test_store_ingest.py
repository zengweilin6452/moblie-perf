"""真机入库验证（`-m device` 门控；设备 749622c9 不在线则跳过且不计为通过）。

两轮 collect 验证 sessions/metrics 聚合行与设备端会话目录清理；
行数不一致告警路径通过 wc 探针注入偏差触发（真实 ls/rm 走 adb）。
"""

import os
import subprocess
from pathlib import Path

import pytest

from mobile_perf.adb import bundled_adb_path, list_devices
from mobile_perf.kit import PerfKit
from mobile_perf.store import PerfStore

pytestmark = pytest.mark.device

DEVICE_SERIAL = os.environ.get("MOBILE_PERF_DEVICE_SERIAL", "749622c9")
TARGET_PACKAGE = os.environ.get("MOBILE_PERF_DEVICE_TARGET", "tv.danmaku.bili")
DURATION_S = 10.0


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


def _device_dir_exists(serial, remote_dir) -> bool:
    adb_path = str(bundled_adb_path())
    proc = subprocess.run(
        [adb_path, "-s", serial, "shell", "test", "-d", remote_dir],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return proc.returncode == 0


def _make_kit(device_available, tmp_path, **kwargs):
    kwargs.setdefault("runs_dir", tmp_path / "runs")
    kwargs.setdefault("store", PerfStore(tmp_path / "data" / "mobile_perf.db"))
    return PerfKit(serial=device_available, **kwargs)


def test_collect_two_rounds_ingest_and_device_cleanup(device_available, tmp_path):
    kit = _make_kit(device_available, tmp_path)
    store = kit._store

    run_ids = []
    for _round in range(2):
        report = kit.run_collect(
            target=TARGET_PACKAGE,
            items=["CPU", "Memory"],
            duration_s=DURATION_S,
            runs_dir=tmp_path / "runs",
        )
        run_ids.append(report["runId"])

        assert report["integrity"]["deviceRowCheck"] == "passed"
        assert report["integrity"]["deviceDirRemoved"] is True
        assert report["metricsSummary"], "聚合摘要不应为空"

    sessions = store.fetch_sessions()
    assert set(run_ids) <= {s["run_id"] for s in sessions}

    metrics_rows = store.fetch_metrics(run_ids[0])
    metric_keys = {row["metric_key"] for row in metrics_rows}
    assert {"cpu_process_pct", "cpu_global_pct", "mem_pss_mb"} <= metric_keys
    for row in metrics_rows:
        assert row["sample_count"] > 0
        assert row["min"] <= row["median"] <= row["max"]

    # 设备端会话目录已被删除（每个 run 的 meta.integrity 均已确认）
    for session in sessions:
        meta_path = Path(session["run_dir"]) / "meta.json"
        if meta_path.exists():
            import json

            integrity = json.loads(meta_path.read_text(encoding="utf-8")).get("integrity", {})
            assert integrity.get("deviceDirRemoved") is True


class WcLyingRunner:
    """wc -l 返回虚增行数，其余命令透传真实 adb（验证行数不一致告警路径）。"""

    def __init__(self):
        self.removed = []

    def __call__(self, cmd, timeout_s):
        if "wc" in cmd:

            class Result:
                stdout = f"  999999 {cmd[-1]}\n"
                returncode = 0

            return Result
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
        if "rm" in cmd:
            self.removed.append(cmd[-1])
        return proc


def test_row_count_mismatch_keeps_device_dir_and_alarms(device_available, tmp_path):
    kit = _make_kit(device_available, tmp_path, adb_runner=WcLyingRunner())
    store = kit._store

    report = kit.run_collect(
        target=TARGET_PACKAGE,
        items=["CPU", "Memory"],
        duration_s=DURATION_S,
        runs_dir=tmp_path / "runs",
    )

    assert report["integrity"]["deviceRowCheck"] == "failed"
    assert report["integrity"]["deviceDirRemoved"] is False

    events = store.fetch_events(report["runId"])
    assert events and events[0]["kind"] == "integrity_warning"
    assert events[0]["detail"]["deviceDirKept"] is True

    # 设备端目录确实还在；测试末尾手工清理，不给设备留垃圾
    remote_dir = events[0]["detail"].get("deviceDir")
    assert remote_dir and _device_dir_exists(device_available, remote_dir)
    subprocess.run(
        [str(bundled_adb_path()), "-s", device_available, "shell", "rm", "-rf", remote_dir],
        capture_output=True,
        timeout=60,
        check=False,
    )
