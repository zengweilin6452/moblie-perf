"""真机补采验证（`-m device` 门控；无设备跳过且不计为通过）。

覆盖票 10 验收：
- 真机 dumpsys 补采一轮：temp_battery_c 采样入库、统一时间轴与 SoloPi 曲线对齐；
- GPU：本机型 dumpsys gpu/gfxinfo 无利用率输出、kgsl 无权限 → 断言如实降级
  （supplement_degraded 事件 + not_tested），不硬造数据。
"""

import json
import os
import time

import pytest

from mobile_perf.adb import _adb_runner, bundled_adb_path, list_devices
from mobile_perf.kit import PerfKit
from mobile_perf.store import PerfStore
from mobile_perf.supplement import SupplementPoller, parse_device_epoch

pytestmark = pytest.mark.device

DEVICE_SERIAL = os.environ.get("MOBILE_PERF_DEVICE_SERIAL", "749622c9")
TARGET_PACKAGE = os.environ.get("MOBILE_PERF_DEVICE_TARGET", "tv.danmaku.bili")


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


def _device_epoch(adb_path: str) -> float:
    run = _adb_runner([adb_path, "-s", DEVICE_SERIAL, "shell", "date", "+%s"], 10.0)
    epoch = parse_device_epoch(run.stdout or "")
    assert epoch is not None
    return epoch


def test_supplement_poller_round_on_real_device(device_available, tmp_path):
    adb_path = str(bundled_adb_path())
    offset = time.time() - _device_epoch(adb_path)
    poller = SupplementPoller(
        adb_path=adb_path,
        serial=device_available,
        interval_s=1.0,
        clock_offset_s=offset,
        collect_temperature=True,
        collect_gpu=True,
    )
    poller.start()
    time.sleep(3.0)
    result = poller.stop()

    assert result.rounds >= 2 and result.errors == 0
    temps = [s["temp_battery_c"] for s in result.samples if s["temp_battery_c"] is not None]
    assert temps, "真机 dumpsys battery 应能解析出电池温度"
    assert all(0.0 < value < 80.0 for value in temps)
    # 统一时间轴：采样时刻相对会话起点单调且落在会话窗口内
    elapsed = [s["elapsed_s"] for s in result.samples]
    assert elapsed == sorted(elapsed)
    assert all(-5.0 <= value <= 10.0 for value in elapsed)

    gpu_keys = {entry["key"] for entry in result.degraded}
    gpu_samples = [s["gpu_util_pct"] for s in result.samples if s["gpu_util_pct"] is not None]
    assert gpu_samples or gpu_keys == {"gpu_util_pct"}  # 采到或如实降级，二者其一


def test_supplement_ingestion_full_loop_on_real_device(device_available, tmp_path):
    adb_path = str(bundled_adb_path())
    store = PerfStore(tmp_path / "data" / "mobile_perf.db")
    kit = PerfKit(
        adb_path=adb_path,
        serial=device_available,
        runs_dir=tmp_path / "runs",
        store=store,
    )
    duration_s = 8.0
    report = kit.run_collect(
        target=TARGET_PACKAGE,
        items=["CPU", "Memory"],
        duration_s=duration_s,
        runs_dir=tmp_path / "runs",
        supplement_interval_s=1.0,
    )

    rows = {row["metric_key"]: row for row in store.fetch_metrics(report["runId"])}
    assert rows["temp_battery_c"]["source"] == "pc_supplement"
    assert rows["temp_battery_c"]["sample_count"] >= 3
    assert rows["cpu_global_pct"]["source"] == "solo_pi"  # SoloPi 聚合并存

    # 三源同图数据基础：series 带 source，补采与 SoloPi 曲线同时间轴范围
    assert report["series"]["temp_battery_c"]["source"] == "pc_supplement"
    temp_x = [p[0] for p in report["series"]["temp_battery_c"]["points"]]
    cpu_x = [p[0] for p in report["series"]["cpu_global_pct"]["points"]]
    assert temp_x and cpu_x
    assert max(temp_x) <= duration_s + 2.0
    assert max(cpu_x) <= duration_s + 5.0  # 两条曲线落在同一条时间轴上

    meta = json.loads(
        (tmp_path / "runs" / report["runId"] / "meta.json").read_text(encoding="utf-8")
    )
    assert meta["supplement"]["enabled"] is True
    degraded_keys = {entry["key"] for entry in meta["supplement"]["degraded"]}
    # 本机型 dumpsys gpu 无利用率输出 → GPU not_tested 如实降级
    assert degraded_keys == {"gpu_util_pct"}
    events = store.fetch_events(report["runId"])
    assert any(event["kind"] == "supplement_degraded" for event in events)
