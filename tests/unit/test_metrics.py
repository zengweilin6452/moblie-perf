"""指标字典定稿后的单元测试（列名契约由 contract/test_metrics_dictionary.py 锁）。"""

import pytest

from mobile_perf import metrics


def test_dictionary_contains_design_v03_plus_calibrated_keys():
    expected = {
        # 设计方案 §7 初版
        "cpu_process_pct",
        "cpu_global_pct",
        "mem_pss_mb",
        "fps_frame",
        "frame_delay_max_ms",
        "net_rx_kbps",
        "net_tx_kbps",
        "battery_pct",
        "battery_current_ma",
        "temp_battery_c",
        "gpu_util_pct",
        # 真实 CSV 校准新增
        "mem_privdirty_mb",
        "mem_global_mb",
        "frame_delay_ratio_pct",
        "frame_delay_count",
        "net_rx_total_kb",
        "net_tx_total_kb",
        "battery_current_avg_ma",
        "battery_power_mw",
        "battery_power_avg_mw",
    }

    assert set(metrics.METRICS) == expected


@pytest.mark.parametrize(
    "column,expected_key",
    [
        ("应用进程-main-18390(%)", "cpu_process_pct"),
        ("全局占用(%)", "cpu_global_pct"),
        ("PSS-main-18390(MB)", "mem_pss_mb"),
        ("PrivateDirty-main-18390(MB)", "mem_privdirty_mb"),
        ("全局占用(MB)", "mem_global_mb"),
        ("帧率(帧)", "fps_frame"),
        ("延迟占比(%)", "frame_delay_ratio_pct"),
        ("延迟次数(次)", "frame_delay_count"),
        ("最长延迟时间(ms)", "frame_delay_max_ms"),
        ("全局下行速率(KB)", "net_rx_kbps"),
        ("全局上行速率(KB)", "net_tx_kbps"),
        ("累计全局下行流量(KB)", "net_rx_total_kb"),
        ("累计全局上行流量(KB)", "net_tx_total_kb"),
        ("实时电流(mA)", "battery_current_ma"),
        ("平均电流(mA)", "battery_current_avg_ma"),
        ("实时功率(mW)", "battery_power_mw"),
        ("平均功率(mW)", "battery_power_avg_mw"),
    ],
)
def test_solo_column_maps_to_canonical_key(column, expected_key):
    assert metrics.column_to_metric(column) is metrics.METRICS[expected_key]


def test_unknown_column_returns_none_and_never_invents_mapping():
    assert metrics.column_to_metric("未知列") is None
    assert metrics.column_to_metric("进程上行速率-main-18390(KB/S)") is None
    assert metrics.column_to_metric("应用进程-render-123(%)") is None


def test_pc_supplement_and_pending_metrics_have_no_solo_column():
    assert metrics.METRICS["temp_battery_c"].source == "pc_supplement"
    assert metrics.METRICS["gpu_util_pct"].source == "pc_supplement"
    for key in ("temp_battery_c", "gpu_util_pct", "battery_pct"):
        assert metrics.METRICS[key].solo_columns == ()
        assert metrics.METRICS[key].solo_shapes == ()


def test_every_metric_has_bilingual_labels_and_unit():
    for spec in metrics.METRICS.values():
        assert spec.label_zh and spec.label_en and spec.unit
