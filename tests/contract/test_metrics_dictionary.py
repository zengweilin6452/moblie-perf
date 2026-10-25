"""指标字典契约：真实 SoloPi CSV 表头 ↔ canonical key 的映射锁。

fixtures/solopi_csv_sample/ 是 perf-output 真机产物的固定基线（GBK 编码、
列名含 (单位) 后缀）。上游 SoloPi 改任何采集列名，本文件即红。
"""

from pathlib import Path

import pytest

from mobile_perf import metrics

FIXTURES = Path(__file__).parent.parent / "fixtures" / "solopi_csv_sample"


def _fixture_files():
    return sorted(p.name for p in FIXTURES.glob("*.csv"))


def test_fixture_sample_exists():
    # 基线在库：有人删了 fixtures 本测试先红
    assert len(_fixture_files()) >= 16


def _value_column(path: Path) -> str:
    """取 CSV 第二列（值列）真实表头；文件为 GBK 编码。"""
    with path.open(encoding="gb18030", newline="") as fh:
        header = fh.readline().rstrip("\r\n").split(",")
    return header[1]


@pytest.mark.parametrize(
    "filename_fragment,expected_key",
    [
        ("应用进程-main-18390_CPU", "cpu_process_pct"),
        ("全局占用_CPU", "cpu_global_pct"),
        ("PSS-main-18390_Memory", "mem_pss_mb"),
        ("PrivateDirty-main-18390_Memory", "mem_privdirty_mb"),
        ("全局占用_Memory", "mem_global_mb"),
        ("帧率_FPS", "fps_frame"),
        ("延迟占比_FPS", "frame_delay_ratio_pct"),
        ("延迟次数_FPS", "frame_delay_count"),
        ("最长延迟时间_FPS", "frame_delay_max_ms"),
        ("全局下行速率_Network", "net_rx_kbps"),
        ("全局上行速率_Network", "net_tx_kbps"),
        ("累计全局下行流量_Network", "net_rx_total_kb"),
        ("累计全局上行流量_Network", "net_tx_total_kb"),
        ("实时电流_Battery", "battery_current_ma"),
        ("平均电流_Battery", "battery_current_avg_ma"),
        ("实时功率_Battery", "battery_power_mw"),
        ("平均功率_Battery", "battery_power_avg_mw"),
    ],
)
def test_real_csv_value_column_maps_to_canonical_key(filename_fragment, expected_key):
    matches = [name for name in _fixture_files() if filename_fragment in name]
    assert len(matches) == 1, f"fixture 缺失或重复: {filename_fragment}"

    column = _value_column(FIXTURES / matches[0])

    spec = metrics.column_to_metric(column)
    assert spec is not None, f"真实列名 {column!r} 未映射到任何 canonical key"
    assert spec.key == expected_key


def test_real_fixture_headers_are_exactly_locked():
    # 静态锁：上游新增/改名任何目标列，此清单需要显式更新
    locked = {
        "应用进程-main-18390(%)": "cpu_process_pct",
        "全局占用(%)": "cpu_global_pct",
        "PSS-main-18390(MB)": "mem_pss_mb",
        "PrivateDirty-main-18390(MB)": "mem_privdirty_mb",
        "全局占用(MB)": "mem_global_mb",
        "帧率(帧)": "fps_frame",
        "延迟占比(%)": "frame_delay_ratio_pct",
        "延迟次数(次)": "frame_delay_count",
        "最长延迟时间(ms)": "frame_delay_max_ms",
        "全局下行速率(KB)": "net_rx_kbps",
        "全局上行速率(KB)": "net_tx_kbps",
        "累计全局下行流量(KB)": "net_rx_total_kb",
        "累计全局上行流量(KB)": "net_tx_total_kb",
        "实时电流(mA)": "battery_current_ma",
        "平均电流(mA)": "battery_current_avg_ma",
        "实时功率(mW)": "battery_power_mw",
        "平均功率(mW)": "battery_power_avg_mw",
    }
    for column, key in locked.items():
        spec = metrics.column_to_metric(column)
        assert spec is not None and spec.key == key


def test_pid_suffixed_columns_still_map_after_pid_changes():
    # pid 是动态的：换 pid 后前缀映射仍成立
    assert metrics.column_to_metric("应用进程-main-99999(%)").key == "cpu_process_pct"
    assert metrics.column_to_metric("PSS-main-99999(MB)").key == "mem_pss_mb"
    assert metrics.column_to_metric("PrivateDirty-main-99999(MB)").key == "mem_privdirty_mb"


def test_same_chinese_name_different_unit_maps_differently():
    # 全局占用 同名列按单位区分：CPU 的 % 与 Memory 的 MB 不得混淆
    assert metrics.column_to_metric("全局占用(%)").key == "cpu_global_pct"
    assert metrics.column_to_metric("全局占用(MB)").key == "mem_global_mb"


def test_every_metric_has_bilingual_labels():
    for spec in metrics.METRICS.values():
        assert spec.label_zh, f"{spec.key} 缺中文标签"
        assert spec.label_en, f"{spec.key} 缺英文标签"


def test_battery_pct_and_pc_supplement_stay_unmapped():
    # 真实样本中无电量%列；温度/GPU 为 pc_supplement，不在 SoloPi CSV 中
    assert metrics.column_to_metric("电量(%)") is None
    for key in ("battery_pct", "temp_battery_c", "gpu_util_pct"):
        assert metrics.METRICS[key].solo_columns == ()
        assert metrics.METRICS[key].solo_shapes == ()


def test_key_naming_convention_snake_case():
    # §7 命名法 <对象>_<指标>_<单位>：key 一律小写下划线（单位后缀在语义成立处出现）
    for spec in metrics.METRICS.values():
        parts = spec.key.split("_")
        assert len(parts) >= 2 and all(p.isalnum() and p.islower() for p in parts), spec.key


# ---- ticket 14：卡顿分级派生指标（无 SoloPi 原始列，同源派生统计） ----


def test_jank_grading_keys_are_derived_from_locked_max_delay_column():
    """分级 key 不映射任何原始列：由「最长延迟时间(ms)」派生，来源列必须已锁定。"""
    from mobile_perf import jank

    assert metrics.column_to_metric("最长延迟时间(ms)").key == jank.SOURCE_METRIC_KEY
    for key in jank.GRADE_METRIC_KEYS.values():
        spec = metrics.METRICS[key]
        assert spec.solo_columns == () and spec.solo_shapes == ()
        assert spec.source == "solo_pi"
        assert spec.unit == "次"


def test_real_csv_granularity_is_per_window_not_per_frame():
    """粒度契约：真实 FPS 延迟列只有 count/ratio/max 三列，无逐帧延迟分布。

    上游若新增逐帧分布列，本测试会提示分级口径可升级（当前口径为窗口级）。
    """
    value_columns = {
        _value_column(path)
        for path in FIXTURES.glob("*_FPS_*.csv")
    }
    assert value_columns == {"帧率(帧)", "延迟占比(%)", "延迟次数(次)", "最长延迟时间(ms)"}
