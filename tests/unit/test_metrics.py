"""指标字典骨架的单元测试（M1 后续票对真实 CSV 校准后定稿）。"""

from mobile_perf import metrics


def test_dictionary_covers_design_v03_initial_keys():
    expected = {
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
    }

    assert set(metrics.METRICS) == expected


def test_metric_keys_are_unique_and_snake_case_with_unit_suffix():
    for spec in metrics.METRICS.values():
        assert spec.key == spec.key  # 唯一性由 dict 本身保证
        assert "_" in spec.key
        assert spec.unit


def test_solo_column_maps_to_canonical_key():
    assert metrics.column_to_metric("PSS-main") is metrics.METRICS["mem_pss_mb"]
    assert metrics.column_to_metric("帧率_FPS") is metrics.METRICS["fps_frame"]
    assert metrics.column_to_metric("全局占用_CPU") is metrics.METRICS["cpu_global_pct"]


def test_unknown_column_returns_none_and_never_invents_mapping():
    assert metrics.column_to_metric("未知列") is None


def test_pc_supplement_metrics_have_no_solo_column():
    for key in ("temp_battery_c", "gpu_util_pct"):
        assert metrics.METRICS[key].source == "pc_supplement"
        assert metrics.METRICS[key].solo_columns == ()


def test_battery_columns_marked_pending_calibration():
    # 电量/电流原始列名待 M1 对真实 CSV 校准，骨架期不提供映射
    assert metrics.METRICS["battery_pct"].solo_columns == ()
    assert metrics.METRICS["battery_current_ma"].solo_columns == ()
    assert metrics.column_to_metric("电量") is None
