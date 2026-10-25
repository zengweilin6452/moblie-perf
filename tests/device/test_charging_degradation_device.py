"""A1 充电降级的真机验证（`-m device` 门控；无设备跳过且不计为通过）。

前提：真机 749622c9 正在充电（USB 供电）。验证 run_collect 编排把充电降级
落到报告：battery_* / 温度指标在 metricsSummary 标注 notTested（附原因），
CPU/Memory 等其余指标照常产出且无标注；judge 门面对标注项跳过判定。
"""

import json
import os

import pytest

from mobile_perf.adb import bundled_adb_path, list_devices
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


def _charging(serial: str) -> bool | None:
    from mobile_perf.keeper import parse_battery_charging
    from mobile_perf.supplement import default_call_device

    result = default_call_device(str(bundled_adb_path()))(
        ["-s", serial, "shell", "dumpsys", "battery"], 10.0
    )
    return parse_battery_charging(result.stdout or "")


def test_run_collect_marks_battery_not_tested_while_charging(device_available):
    if _charging(device_available) is not True:
        pytest.skip("真机当前未充电，充电降级路径未验证（拔电场景另行覆盖）")

    kit = PerfKit(
        adb_path=str(bundled_adb_path()),
        serial=device_available,
        runs_dir=os.environ.get("MOBILE_PERF_RUNS_DIR"),
    )

    report = kit.run_collect(
        target="tv.danmaku.bili",
        items=["CPU", "Memory", "Battery"],
        duration_s=8.0,
        runs_dir=None,
    )

    print(json.dumps(report["metricsSummary"], ensure_ascii=False, indent=2))

    # 巡检结论：充电 → Battery/Temperature 降级项如实记录
    readiness = kit.last_readiness
    assert readiness is not None
    assert "Battery" in readiness.degraded_items

    summary = report["metricsSummary"]
    battery_rows = {k: v for k, v in summary.items() if k.startswith("battery_")}
    temp_rows = summary.get("temp_battery_c")
    assert battery_rows or temp_rows, "充电 run 应产出 battery/温度聚合行供标注验证"

    for key, entry in battery_rows.items():
        assert entry.get("notTested") is True, f"{key} 应标注 notTested"
        assert entry.get("notTestedReason"), f"{key} 应附降级原因"
    if temp_rows is not None:
        assert temp_rows.get("notTested") is True

    # 其余指标照常：CPU/Memory 聚合存在且无 notTested 标注
    for key in ("cpu_process_pct", "cpu_global_pct", "mem_pss_mb"):
        if key in summary:
            assert "notTested" not in summary[key], f"{key} 不应被充电降级牵连"

    # 判定联动：对标注 notTested 的指标传阈值 → verdict 证据里说明并跳过
    from mobile_perf.judge import JudgeSpec

    battery_key = sorted(battery_rows)[0] if battery_rows else "temp_battery_c"
    kit.store.record_baseline(
        name="device-charging-probe",
        target_pkg="tv.danmaku.bili",
        device_profile=report.get("deviceProfile"),
        metric_key=battery_key,
        value=summary[battery_key]["median"],
        source_run_id=report["runId"],
    )
    verdict = kit.judge(
        report,
        JudgeSpec(
            baseline="device-charging-probe",
            thresholds={battery_key: ("lt", 10**9, "fail")},
        ),
    )
    assert verdict.verdict in ("not_tested", "passed")
    if verdict.verdict == "not_tested":
        assert battery_key in verdict.reason
    else:
        assert any(
            w.get("metricKey") == battery_key and w.get("notTested")
            for w in verdict.warnings
        )
