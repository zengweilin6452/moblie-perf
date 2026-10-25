"""指标字典：canonical key ↔ SoloPi 中文原始列名的唯一映射层（设计方案 §6.4/§7）。

canonical key 是全组件唯一词汇（judge/report/store/CLI/宿主只见 canonical key）；
中文原始列名只允许出现在本模块。初版依据设计方案 §7，M1 后续票对真实 CSV
校准后定稿（电量/电流列名当前待校准）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MetricSpec:
    """规范指标：canonical key、单位、来源与对应的 SoloPi 原始列。"""

    key: str
    unit: str
    source: str
    solo_columns: tuple[str, ...]


METRICS: dict[str, MetricSpec] = {
    spec.key: spec
    for spec in (
        MetricSpec("cpu_process_pct", "%", "solo_pi", ("应用进程-main-<pid>_CPU",)),
        MetricSpec("cpu_global_pct", "%", "solo_pi", ("全局占用_CPU",)),
        MetricSpec("mem_pss_mb", "MB", "solo_pi", ("PSS-main",)),
        MetricSpec("fps_frame", "fps", "solo_pi", ("帧率_FPS",)),
        MetricSpec("frame_delay_max_ms", "ms", "solo_pi", ("最长延迟时间",)),
        MetricSpec("net_rx_kbps", "kbps", "solo_pi", ("全局下行速率",)),
        MetricSpec("net_tx_kbps", "kbps", "solo_pi", ("全局上行速率",)),
        # 电量/电流原始列名待对真实 CSV 校准后补入 solo_columns
        MetricSpec("battery_pct", "%", "solo_pi", ()),
        MetricSpec("battery_current_ma", "mA", "solo_pi", ()),
        MetricSpec("temp_battery_c", "°C", "pc_supplement", ()),
        MetricSpec("gpu_util_pct", "%", "pc_supplement", ()),
    )
}

_COLUMN_INDEX: dict[str, MetricSpec] = {
    column: spec
    for spec in METRICS.values()
    for column in spec.solo_columns
    # 不同指标不得共享同一原始列名，否则映射不唯一
}


def column_to_metric(column: str) -> MetricSpec | None:
    """把 SoloPi 原始列名翻译为规范指标；字典外的列一律返回 None。"""
    return _COLUMN_INDEX.get(column)
