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
        # 启动编排指标（ticket 12，source=pc_orchestration）
        "app_cold_start_ms",
        "app_warm_start_ms",
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


def test_aggregate_row_computes_min_median_mean_max_p90():
    """聚合行组装唯一定义于 metrics：collector 与 supplement 共用同一实现。"""
    row = metrics.aggregate_row("mem_pss_mb", [30.0, 10.0, 20.0])

    assert row["metric_key"] == "mem_pss_mb"
    assert row["unit"] == "MB"
    assert row["source"] == "solo_pi"
    assert row["min"] == 10.0
    assert row["median"] == 20.0
    assert row["mean"] == 20.0
    assert row["max"] == 30.0
    assert row["p90"] == 28.0  # 线性插值：20 + (30-20)*0.8
    assert row["sample_count"] == 3


def test_percentile_linear_interpolation_matches_numpy_default():
    assert metrics.percentile([1.0], 0.9) == 1.0
    assert metrics.percentile([10.0, 20.0, 30.0, 40.0], 0.5) == 25.0
    assert metrics.percentile([10.0, 20.0], 0.9) == 19.0


def test_collector_and_supplement_share_metrics_aggregate_row():
    import inspect

    from mobile_perf import collector, supplement

    collector_src = inspect.getsource(collector.aggregate_artifacts)
    supplement_src = inspect.getsource(supplement.aggregate_samples)
    assert "aggregate_row" in collector_src
    assert "aggregate_row" in supplement_src
    assert "_percentile" not in collector_src
    assert "_percentile" not in supplement_src


def test_degraded_metric_keys_maps_charging_items_to_canonical_keys():
    """A1：充电降级采集项 → canonical key 集合（Battery 全族 + 温度补采项）。"""
    keys = metrics.degraded_metric_keys(("Battery", "Temperature"))

    assert keys == {
        "battery_pct",
        "battery_current_ma",
        "battery_current_avg_ma",
        "battery_power_mw",
        "battery_power_avg_mw",
        "temp_battery_c",
    }
    assert metrics.degraded_metric_keys(()) == set()
    assert metrics.degraded_metric_keys(("Battery",)) == {
        "battery_pct",
        "battery_current_ma",
        "battery_current_avg_ma",
        "battery_power_mw",
        "battery_power_avg_mw",
    }
