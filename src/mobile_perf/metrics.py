"""指标字典：canonical key ↔ SoloPi 中文原始列名的唯一映射层（设计方案 §6.4/§7）。

canonical key 是全组件唯一词汇（judge/report/store/CLI/宿主只见 canonical key）；
中文原始列名只允许出现在本模块。已对 perf-output 真机 CSV 校准定稿：

- 真实值列名带 ``(单位)`` 后缀（如 ``全局占用(%)``），精确匹配按全列名；
- 同名不同单位靠后缀区分（``全局占用(%)`` 是 CPU、``全局占用(MB)`` 是内存）；
- 列名含动态 pid（``PSS-main-18390(MB)``）用 ``*`` 通配形状匹配；
- 聚合值（min/median/mean/max/p90）是 metrics 表字段，不是 key。

双端联动契约：SoloPi 开启 ``KEY_CSV_ENGLISH_NAME`` 后，CSV 文件名前缀由
``SoloPi/solopi-app/app/src/main/java/com/alipay/hulu/util/RecordUtil.java``
的英文前缀映射表生成；该表与本指标字典的采集项/指标对应关系属双端契约，
改动任一端必须同步另一端，并回归 tests/contract/test_metrics_dictionary.py。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MetricSpec:
    """规范指标：canonical key、单位、来源、双语标签与对应的 SoloPi 原始列。

    solo_columns 为精确列名；solo_shapes 形如 ``前缀*后缀``（``*`` 通配 pid 段），
    用于列名含动态 pid 的指标。二者必须覆盖且只覆盖各自的原始列。
    """

    key: str
    unit: str
    source: str
    label_zh: str
    label_en: str
    solo_columns: tuple[str, ...] = ()
    solo_shapes: tuple[str, ...] = field(default=())


METRICS: dict[str, MetricSpec] = {
    spec.key: spec
    for spec in (
        MetricSpec(
            "cpu_process_pct", "%", "solo_pi",
            "应用进程CPU占用", "Process CPU usage",
            solo_shapes=("应用进程-main-*(%)",),
        ),
        MetricSpec(
            "cpu_global_pct", "%", "solo_pi",
            "全局CPU占用", "Global CPU usage",
            solo_columns=("全局占用(%)",),
        ),
        MetricSpec(
            "mem_pss_mb", "MB", "solo_pi",
            "应用PSS内存", "Process PSS memory",
            solo_shapes=("PSS-main-*(MB)",),
        ),
        MetricSpec(
            "mem_privdirty_mb", "MB", "solo_pi",
            "应用PrivateDirty内存", "Process PrivateDirty memory",
            solo_shapes=("PrivateDirty-main-*(MB)",),
        ),
        MetricSpec(
            "mem_global_mb", "MB", "solo_pi",
            "全局内存占用", "Global memory usage",
            solo_columns=("全局占用(MB)",),
        ),
        MetricSpec(
            "fps_frame", "fps", "solo_pi",
            "帧率", "Frame rate",
            # 真实 CSV 单位标注为"帧"，语义为每秒帧数
            solo_columns=("帧率(帧)",),
        ),
        MetricSpec(
            "frame_delay_ratio_pct", "%", "solo_pi",
            "帧延迟占比", "Frame delay ratio",
            solo_columns=("延迟占比(%)",),
        ),
        MetricSpec(
            "frame_delay_count", "次", "solo_pi",
            "帧延迟次数", "Frame delay count",
            solo_columns=("延迟次数(次)",),
        ),
        MetricSpec(
            "frame_delay_max_ms", "ms", "solo_pi",
            "最长帧延迟", "Max frame delay",
            solo_columns=("最长延迟时间(ms)",),
        ),
        MetricSpec(
            "net_rx_kbps", "kbps", "solo_pi",
            "全局下行速率", "Global download rate",
            # 真实 CSV 单位标注 KB，与进程速率 KB/S 对照实为 KB/s
            solo_columns=("全局下行速率(KB)",),
        ),
        MetricSpec(
            "net_tx_kbps", "kbps", "solo_pi",
            "全局上行速率", "Global upload rate",
            solo_columns=("全局上行速率(KB)",),
        ),
        MetricSpec(
            "net_rx_total_kb", "KB", "solo_pi",
            "累计全局下行流量", "Total download traffic",
            solo_columns=("累计全局下行流量(KB)",),
        ),
        MetricSpec(
            "net_tx_total_kb", "KB", "solo_pi",
            "累计全局上行流量", "Total upload traffic",
            solo_columns=("累计全局上行流量(KB)",),
        ),
        MetricSpec(
            "battery_current_ma", "mA", "solo_pi",
            "实时电流", "Instantaneous current",
            solo_columns=("实时电流(mA)",),
        ),
        MetricSpec(
            "battery_current_avg_ma", "mA", "solo_pi",
            "平均电流", "Average current",
            solo_columns=("平均电流(mA)",),
        ),
        MetricSpec(
            "battery_power_mw", "mW", "solo_pi",
            "实时功率", "Instantaneous power",
            solo_columns=("实时功率(mW)",),
        ),
        MetricSpec(
            "battery_power_avg_mw", "mW", "solo_pi",
            "平均功率", "Average power",
            solo_columns=("平均功率(mW)",),
        ),
        # 真机样本中无电量%列：保留 canonical key，映射待上游确认后补入
        MetricSpec("battery_pct", "%", "solo_pi", "电量", "Battery level"),
        MetricSpec("temp_battery_c", "°C", "pc_supplement", "电池温度", "Battery temperature"),
        MetricSpec("gpu_util_pct", "%", "pc_supplement", "GPU占用", "GPU utilization"),
        # 启动编排指标（ticket 12）：采集窗口内 force-stop/退后台后 am start -W
        # 计得，单样本入库（聚合字段填单值）；无 SoloPi 原始列
        MetricSpec("app_cold_start_ms", "ms", "pc_orchestration", "冷启动耗时", "Cold start time"),
        MetricSpec("app_warm_start_ms", "ms", "pc_orchestration", "温启动耗时", "Warm start time"),
        # 卡顿分级（ticket 14）：同源派生统计——对 SoloPi「最长延迟时间」逐采样
        # 窗口值按阈值分桶计数（>100/>300/>700 ms 默认，宿主可覆盖），非逐帧
        # 数据（SoloPi CSV 无逐帧延迟分布，口径偏差见 jank.py 模块文档）
        MetricSpec(
            "app_jank_light_count", "次", "solo_pi",
            "轻度卡顿窗口数(>100ms)", "Light jank windows (>100ms)",
        ),
        MetricSpec(
            "app_jank_heavy_count", "次", "solo_pi",
            "重度卡顿窗口数(>300ms)", "Heavy jank windows (>300ms)",
        ),
        MetricSpec(
            "app_jank_severe_count", "次", "solo_pi",
            "严重卡顿窗口数(>700ms)", "Severe jank windows (>700ms)",
        ),
        # 逐帧口径（ticket 15）：源自 `dumpsys gfxinfo <pkg> framestats` 的单帧
        # 耗时（FrameCompleted − IntendedVsync），会话级单样本结论；与工单 14 的
        # 窗口级卡顿分级粒度不同、不可互算（见 frameinfo.py 模块文档）
        MetricSpec(
            "app_frame_p50_ms", "ms", "pc_supplement",
            "帧耗时P50", "Frame duration P50",
        ),
        MetricSpec(
            "app_frame_p90_ms", "ms", "pc_supplement",
            "帧耗时P90", "Frame duration P90",
        ),
        MetricSpec(
            "app_frame_p99_ms", "ms", "pc_supplement",
            "帧耗时P99", "Frame duration P99",
        ),
        MetricSpec(
            "app_frame_jank_ratio_pct", "%", "pc_supplement",
            "超时帧占比", "Janky frame ratio",
        ),
        MetricSpec(
            "app_frame_count", "帧", "pc_supplement",
            "采样帧数", "Sampled frame count",
        ),
        # 泄漏趋势斜率（ticket 13）：跨 run 回归产物，非采集指标、无 SoloPi 原始列；
        # 由 leak.analyze_leak_trend 计算，阈值判定归宿主（leak.leak_verdict）
        MetricSpec(
            "mem_pss_slope_mb_per_day",
            "MB/day",
            "analysis",
            "应用PSS内存增长斜率",
            "Process PSS memory growth slope",
        ),
    )
}

_COLUMN_INDEX: dict[str, MetricSpec] = {}
_SHAPE_INDEX: dict[tuple[str, str], MetricSpec] = {}
for _spec in METRICS.values():
    for _column in _spec.solo_columns:
        if _column in _COLUMN_INDEX:
            raise RuntimeError(f"原始列 {_column!r} 被多个指标映射，翻译不唯一")
        _COLUMN_INDEX[_column] = _spec
    for _shape in _spec.solo_shapes:
        if "*" not in _shape:
            raise RuntimeError(f"形状 {_shape!r} 缺少 * 通配符")
        _prefix, _suffix = _shape.split("*", 1)
        _shape_key = (_prefix, _suffix)
        if _shape_key in _SHAPE_INDEX:
            raise RuntimeError(f"形状 {_shape!r} 被多个指标映射，翻译不唯一")
        _SHAPE_INDEX[_shape_key] = _spec


def column_to_metric(column: str) -> MetricSpec | None:
    """把 SoloPi 原始列名翻译为规范指标；字典外的列一律返回 None。"""
    spec = _COLUMN_INDEX.get(column)
    if spec is not None:
        return spec
    for (prefix, suffix), shape_spec in _SHAPE_INDEX.items():
        if column.startswith(prefix) and column.endswith(suffix):
            return shape_spec
    return None


# 充电降级采集项（keeper ReadinessReport.degraded_items 的元素）到 canonical key
# 的映射口径：Battery 覆盖全部 battery_* 指标，Temperature 对应温度补采项
DEGRADED_ITEM_BATTERY = "Battery"
DEGRADED_ITEM_TEMPERATURE = "Temperature"


def degraded_metric_keys(degraded_items) -> set[str]:
    """把巡检的充电降级采集项映射为应标注 not_tested 的 canonical key 集合（§6.2）。"""
    items = set(degraded_items or ())
    keys: set[str] = set()
    if DEGRADED_ITEM_BATTERY in items:
        keys |= {key for key in METRICS if key.startswith("battery_")}
    if DEGRADED_ITEM_TEMPERATURE in items:
        keys.add("temp_battery_c")
    return keys


def mark_not_tested(metrics_summary: dict, degraded_items) -> dict:
    """对降级采集项对应的 metricsSummary 条目就地标注 not_tested（A1/§6.2）。

    其余指标不受影响；无聚合数据的降级项自然无处标注。返回原字典便于链式使用。
    """
    for key in sorted(degraded_metric_keys(degraded_items)):
        entry = metrics_summary.get(key)
        if entry is None:
            continue
        item = DEGRADED_ITEM_TEMPERATURE if key == "temp_battery_c" else DEGRADED_ITEM_BATTERY
        entry["notTested"] = True
        entry["notTestedReason"] = (
            f"设备充电中，巡检 degraded_items 含 {item}，该采集项降级 not_tested"
        )
    return metrics_summary


def percentile(sorted_values: list[float], ratio: float) -> float:
    """线性插值百分位（与 numpy 默认 percentile 一致）；输入须已升序。"""
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = ratio * (len(sorted_values) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = rank - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction


def aggregate_row(metric_key: str, values: list[float]) -> dict:
    """把一组样本聚合成 store.record_metrics 可吃的行（min/median/mean/max/p90）。

    全组件唯一的聚合行组装（collector 的 CSV 聚合与 supplement 的样本聚合共用）。
    """
    ordered = sorted(values)
    count = len(ordered)
    spec = METRICS[metric_key]
    return {
        "metric_key": metric_key,
        "unit": spec.unit,
        "source": spec.source,
        "min": ordered[0],
        "median": percentile(ordered, 0.5),
        "mean": sum(ordered) / count,
        "max": ordered[-1],
        "p90": percentile(ordered, 0.9),
        "sample_count": count,
    }
