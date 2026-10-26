"""真机补采验证（`-m device` 门控；无设备跳过且不计为通过）。

覆盖票 10 / 票 16 验收：
- 真机补采一轮：temp_battery_c 采样入库、统一时间轴与 SoloPi 曲线对齐；
- GPU 双源（票 16）：Adreno 机型 kgsl `gpubusy` 可读 → `gpu_util_pct` 有非零值，
  与 M5 的「永远 not_tested」形成对比；`gpu_mem_mb` 按目标包名 pid 取到显存。
  非 Adreno / 无权限机型上如实降级（supplement_degraded + 逐级原因），不硬造数据。
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
TARGET_PACKAGE = os.environ.get("MOBILE_PERF_DEVICE_TARGET", "com.sinosoft.trainapp")


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


def _device_charging(adb_path: str, serial: str) -> bool | None:
    """从 dumpsys battery 判真机是否充电中（A1 温度补采跳过的前提）。"""
    from mobile_perf.keeper import parse_battery_charging

    run = _adb_runner([adb_path, "-s", serial, "shell", "dumpsys", "battery"], 10.0)
    return parse_battery_charging(run.stdout or "")


def _device_epoch(adb_path: str) -> float:
    run = _adb_runner([adb_path, "-s", DEVICE_SERIAL, "shell", "date", "+%s"], 10.0)
    epoch = parse_device_epoch(run.stdout or "")
    assert epoch is not None
    return epoch


def _kgsl_readable(adb_path: str, serial: str) -> bool:
    """本机 kgsl `gpubusy` 是否可读（Adreno + 无需 root 的前提）。"""
    run = _adb_runner(
        [adb_path, "-s", serial, "shell", "cat", "/sys/class/kgsl/kgsl-3d0/gpubusy"], 10.0
    )
    return bool((run.stdout or "").strip()) and run.stdout.split()[0].isdigit()


def _target_pid(adb_path: str, serial: str, pkg: str) -> int | None:
    run = _adb_runner([adb_path, "-s", serial, "shell", "pidof", pkg], 10.0)
    pids = (run.stdout or "").split()
    return int(pids[0]) if len(pids) == 1 and pids[0].isdigit() else None


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
        collect_gpu_mem=True,
        target_pkg=TARGET_PACKAGE,
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

    gpu_samples = [s["gpu_util_pct"] for s in result.samples if s["gpu_util_pct"] is not None]
    if _kgsl_readable(adb_path, device_available):
        # 票 16 的核心验收：kgsl 可读的机型上 GPU 利用率必须有值（不再是 not_tested）
        assert gpu_samples, f"kgsl 可读却没取到利用率: {result.degraded}"
        assert all(0.0 <= value <= 100.0 for value in gpu_samples)
        assert result.gpu_source == "kgsl_gpubusy"
    else:
        # 非 Adreno / 无权限：如实降级，且降级事件带逐级原因
        entry = next(item for item in result.degraded if item["key"] == "gpu_util_pct")
        assert entry["stages"]


def test_gpu_mem_tracks_target_process_on_real_device(device_available):
    """应用级显存：按目标包名 pid 取 `Proc <pid> total`，量级须像显存（MB 级）。"""
    adb_path = str(bundled_adb_path())
    _adb_runner(
        [adb_path, "-s", device_available, "shell", "monkey", "-p", TARGET_PACKAGE, "1"], 30.0
    )
    time.sleep(4.0)  # 等进程真正起来并完成首屏渲染

    pid = _target_pid(adb_path, device_available, TARGET_PACKAGE)
    if pid is None:
        pytest.skip(f"{TARGET_PACKAGE} 未运行或存在多个 pid，无法确定目标进程")

    poller = SupplementPoller(
        adb_path=adb_path,
        serial=device_available,
        interval_s=1.0,
        clock_offset_s=time.time() - _device_epoch(adb_path),
        collect_temperature=False,
        collect_gpu=False,
        collect_gpu_mem=True,
        target_pkg=TARGET_PACKAGE,
    )
    poller.start()
    time.sleep(2.5)
    result = poller.stop()

    values = [s["gpu_mem_mb"] for s in result.samples if s["gpu_mem_mb"] is not None]
    if not values:
        # 部分机型 dumpsys gpu 无 Proc 段：如实降级而不是编数
        entry = next(item for item in result.degraded if item["key"] == "gpu_mem_mb")
        assert entry["stages"] == ["proc_not_found"]
        pytest.skip(f"本机 dumpsys gpu 无 Proc {pid} 显存行，如实降级")
    assert all(0.0 < value < 4096.0 for value in values)


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
    assert rows["cpu_global_pct"]["source"] == "solo_pi"  # SoloPi 聚合并存

    # A1 充电降级：充电时温度补采按巡检结论跳过（不硬造数据），未充电才产出
    charging = _device_charging(adb_path, device_available)
    if charging:
        assert "temp_battery_c" not in rows
        assert kit.last_readiness.degraded_items == ("Battery", "Temperature")
    else:
        assert rows["temp_battery_c"]["source"] == "pc_supplement"
        assert rows["temp_battery_c"]["sample_count"] >= 3

    # 三源同图数据基础：series 带 source，补采与 SoloPi 曲线同时间轴范围
    cpu_x = [p[0] for p in report["series"]["cpu_global_pct"]["points"]]
    assert cpu_x
    assert max(cpu_x) <= duration_s + 5.0  # 两条曲线落在同一条时间轴上
    if not charging:
        assert report["series"]["temp_battery_c"]["source"] == "pc_supplement"
        temp_x = [p[0] for p in report["series"]["temp_battery_c"]["points"]]
        assert temp_x
        assert max(temp_x) <= duration_s + 2.0

    meta = json.loads(
        (tmp_path / "runs" / report["runId"] / "meta.json").read_text(encoding="utf-8")
    )
    assert meta["supplement"]["enabled"] is True
    degraded_keys = {entry["key"] for entry in meta["supplement"]["degraded"]}
    # GPU：kgsl 可读则产出指标（不进 degraded）；不可读才如实降级
    if _kgsl_readable(adb_path, device_available):
        assert "gpu_util_pct" not in degraded_keys
        assert rows["gpu_util_pct"]["source"] == "pc_supplement"
        assert report["gpu"]["gpuSource"] == "kgsl_gpubusy"
    else:
        assert "gpu_util_pct" in degraded_keys
        events = store.fetch_events(report["runId"])
        assert any(event["kind"] == "supplement_degraded" for event in events)
