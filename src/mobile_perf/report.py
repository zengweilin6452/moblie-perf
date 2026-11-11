"""报告聚合：每个 run 产出同内容 JSON 与单文件 HTML（内嵌离线 ECharts，无 CDN 依赖）。

JSON 与 HTML 出自同一份报告字典（同一聚合数据源）；事件在元信息小节以折叠
列表呈现（数据来自 store 已落盘事件，不新增采集）。
"""

from __future__ import annotations

import csv
import json
import os
from html import escape as html_escape
from pathlib import Path
from typing import Any

from . import battery as battery_module
from . import collector
from . import jank as jank_module
from . import metrics as metrics_module

SCHEMA_VERSION = 1

# SoloPi 真机 CSV 为 GBK 家族编码（与 collector 保持同一口径）
CSV_ENCODING = "gb18030"

# 曲线单指标最大点数：超出时等距抽样，避免长会话把 HTML 撑爆
MAX_SERIES_POINTS = 3600


def series_from_artifacts(artifacts_dir: str | Path) -> dict[str, Any]:
    """从 pull 产物 CSV 提取各 canonical key 的时间曲线（报告图表数据源）。

    x 为相对首个样本的秒数（RecordTime 毫秒转秒；无 RecordTime 时依次退化为
    SimpleTime 可解析数值、行序号）；字典外列跳过。无任何样本返回空 dict。
    """
    artifacts_dir = Path(artifacts_dir)
    if not artifacts_dir.is_dir():
        return {}
    # key -> (基准毫秒 | None, [(x 秒, y), ...])
    curves: dict[str, tuple[float | None, list[tuple[float, float]]]] = {}
    for path in sorted(artifacts_dir.rglob("*.csv")):
        if not path.is_file() or path.stat().st_size == 0:
            continue
        _absorb_csv(path, curves)
    series: dict[str, Any] = {}
    for key in sorted(curves):
        t0, points = curves[key]
        points.sort(key=lambda item: item[0])
        if t0 is not None:
            offset_s = t0 / 1000.0
            points = [(round(x - offset_s, 3), y) for x, y in points]
        else:
            points = [(float(index), y) for index, (_, y) in enumerate(points)]
        spec = metrics_module.METRICS[key]
        series[key] = {
            "unit": spec.unit,
            "source": spec.source,
            "labelZh": spec.label_zh,
            "labelEn": spec.label_en,
            "points": _downsample(points),
        }
    return series


def _absorb_csv(path: Path, curves: dict[str, tuple[float | None, list[tuple[float, float]]]]):
    with path.open(encoding=CSV_ENCODING, newline="") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            return
        keyed: list[tuple[int, str]] = []
        time_index: int | None = None
        simple_index: int | None = None
        elapsed_index: int | None = None
        for index, name in enumerate(header):
            name = name.strip()
            if name == "RecordTime":
                time_index = index
            elif name == "SimpleTime":
                simple_index = index
            elif name == "elapsed_s":
                # 补采 CSV（supplement.py）：统一时间轴秒列
                elapsed_index = index
            else:
                spec = metrics_module.column_to_metric(name)
                if spec is None and name in metrics_module.METRICS:
                    spec = metrics_module.METRICS[name]  # 补采 CSV 用 canonical key 做列名
                if spec is not None:
                    keyed.append((index, spec.key))
        for row in reader:
            for index, key in keyed:
                if index >= len(row):
                    continue
                try:
                    value = float(row[index])
                except ValueError:
                    continue
                x_ms = _row_time_ms(row, time_index, simple_index, elapsed_index)
                # elapsed 轴已是相对秒，基准恒为 0（空值行不移动零点）
                default_t0 = 0.0 if elapsed_index is not None else x_ms
                t0, points = curves.setdefault(key, (default_t0, []))
                if t0 is None:
                    t0 = x_ms
                    curves[key] = (t0, points)
                if x_ms is not None and t0 is not None:
                    points.append((x_ms / 1000.0, value))
                else:
                    points.append((0.0, value))


def _row_time_ms(
    row: list[str],
    time_index: int | None,
    simple_index: int | None,
    elapsed_index: int | None = None,
) -> float | None:
    if elapsed_index is not None and elapsed_index < len(row):
        try:
            return float(row[elapsed_index]) * 1000.0  # 补采 elapsed_s 为秒
        except ValueError:
            return None
    if time_index is not None and time_index < len(row):
        try:
            return float(row[time_index])  # SoloPi RecordTime 为毫秒时间戳
        except ValueError:
            return None
    if simple_index is not None and simple_index < len(row):
        try:
            return float(row[simple_index]) * 1000.0  # SimpleTime 为秒
        except ValueError:
            return None
    return None


def _downsample(points: list[tuple[float, float]]) -> list[list[float]]:
    if len(points) <= MAX_SERIES_POINTS:
        return [[x, y] for x, y in points]
    stride = -(-len(points) // MAX_SERIES_POINTS)
    return [[x, y] for x, y in points[::stride]]


def build_report(
    *,
    meta: dict[str, Any],
    record: dict[str, Any],
    artifacts: list[str],
    metrics_summary: dict[str, Any] | None = None,
    integrity: dict[str, Any] | None = None,
    series: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
    jank_thresholds_ms: dict[str, float] | None = None,
    frames: dict[str, Any] | None = None,
    pages: dict[str, Any] | None = None,
    network: dict[str, Any] | None = None,
    gpu: dict[str, Any] | None = None,
    api: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把采集元数据、会话记录与产物清单聚合为报告字典（JSON 与 HTML 共用）。

    metrics_summary 的每个指标补充双语标签（来自指标字典）；series 为
    {canonical key: 曲线数据}；events 只保留数据，渲染由后续工单接入。
    jank_thresholds_ms 为卡顿分级阈值（宿主可覆盖，缺省组件默认口径）；
    仅当报告含分级指标时记录口径元信息（ticket 14）。
    """
    report = {
        "schemaVersion": SCHEMA_VERSION,
        "runId": meta["runId"],
        "target": meta["target"],
        "items": meta["items"],
        "serial": meta["serial"],
        "deviceProfile": meta.get("deviceProfile"),
        "hostRunId": meta.get("hostRunId"),
        "startedAt": meta["startedAt"],
        "stoppedAt": meta["stoppedAt"],
        "durationS": meta["durationS"],
        "soloSessionId": record.get("sessionId"),
        "marks": record.get("marks", []),
        "artifacts": artifacts,
        "metricsSummary": _with_metric_labels(metrics_summary),
        "series": series if series is not None else {},
        "events": list(events or []),
        "startup": meta.get("startup"),
        "logwatch": meta.get("logwatch"),
        "integrity": integrity,
    }
    if any(key in (metrics_summary or {}) for key in jank_module.GRADE_METRIC_KEYS.values()):
        report["jank"] = {
            "sourceMetric": jank_module.SOURCE_METRIC_KEY,
            "granularity": "per_sampling_window",
            "thresholdsMs": jank_module.validate_thresholds(
                jank_thresholds_ms or jank_module.DEFAULT_THRESHOLDS_MS
            ),
        }
    # 场景耗电量（perf-metrics-expansion 01）：口径随报告落盘——只计放电段的
    # 梯形积分，与平均电流（瞬时口径）不可互算
    if battery_module.METRIC_KEY in (metrics_summary or {}):
        report["battery"] = {
            "sourceMetric": battery_module.SOURCE_METRIC_KEY,
            "granularity": "per_session",
            "method": "trapezoid_discharge_only",
        }
    # 逐帧口径（ticket 15）：与上面窗口级口径并存，粒度不同、不可互算
    if frames is not None:
        report["frames"] = {
            **frames,
            "source": "gfxinfo_framestats",
            "granularity": "per_frame",
        }
    # 整页加载耗时（ticket 19）：与启动耗时**分开**呈现——一个从进程拉起算起，
    # 一个从页面打标算起，混在一张表里会被读成同一件事
    if pages is not None:
        report["pages"] = {**pages, "source": "pageflow_last_frame_stable"}
    # 网络剖面（ticket 17）：档位是可比性维度，与逐帧/GPU 并列展示。
    # 只放已脱敏的结论（档位参数、证据、恢复结果），不含任何流量内容
    if network is not None:
        report["network"] = {**network, "source": "mitmproxy"}
    # 接口级明细（ticket 18）：仅解密档且信任探测成功时有值。与连接级指标
    # 分栏呈现——两者粒度不同（按 URL vs 按隧道），不可互相换算
    if api is not None:
        report["api"] = {**api, "source": "mitmproxy_decrypt"}
    # GPU 双源（ticket 16）：记下实际生效的数据源与降级链，供报告与判读使用。
    # 与 frames 一样只做补充，不改变 metricsSummary 的数值口径
    if gpu is not None:
        report["gpu"] = {**gpu, "source": "dumpsys_and_sysfs"}
    return report


def _with_metric_labels(
    metrics_summary: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not metrics_summary:
        return metrics_summary
    labeled: dict[str, Any] = {}
    for key, entry in metrics_summary.items():
        spec = metrics_module.METRICS.get(key)
        labeled[key] = {
            **entry,
            "labelZh": spec.label_zh if spec else None,
            "labelEn": spec.label_en if spec else None,
        }
    return labeled


def summarize_aggregates(aggregates: list[dict[str, Any]]) -> dict[str, Any]:
    """把 collector 的聚合行转成报告 metricsSummary 形状（camelCase，供 JSON/HTML）。"""
    return {
        agg["metric_key"]: {
            "unit": agg["unit"],
            "source": agg["source"],
            "min": agg["min"],
            "median": agg["median"],
            "mean": agg["mean"],
            "max": agg["max"],
            "p90": agg["p90"],
            "sampleCount": agg["sample_count"],
        }
        for agg in aggregates
    }


class RunNotFoundError(RuntimeError):
    """store 中不存在指定 run_id 的会话记录。"""


def assemble_run_report(
    run_id: str,
    *,
    runs_dir: str | Path,
    store: Any,
) -> dict[str, Any]:
    """从 runs/<runId>/（meta.json、artifacts）与 store 组装完整报告数据。

    store 是 PerfStore（鸭子类型，便于测试替身）；meta.json 优先于 store 行，
    run 目录缺失时退化为纯 store 数据（series/artifacts 为空）。events 取自
    store（07 号工单接入渲染）。产出经 build_report 统一补标签，随后可用
    write_report / write_html 落盘双产物。
    """
    runs_dir = Path(runs_dir)
    session = next(
        (s for s in store.fetch_sessions(limit=10000) if s.get("run_id") == run_id), None
    )
    if session is None:
        raise RunNotFoundError(f"store 中没有 run_id={run_id} 的会话记录")

    run_dir = Path(session["run_dir"]) if session.get("run_dir") else runs_dir / run_id
    meta_path = run_dir / "meta.json"
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    else:
        meta = {
            "runId": run_id,
            "target": session.get("target_pkg"),
            "items": session.get("items", []),
            "serial": session.get("serial"),
            "hostRunId": session.get("host_run_id"),
            "startedAt": session.get("started_at"),
            "stoppedAt": session.get("stopped_at"),
            "durationS": session.get("duration_s"),
        }
    # device_profile 只入库不进 meta.json：store 行是权威来源，缺了就补
    meta.setdefault("deviceProfile", session.get("device_profile"))

    artifacts_dir = run_dir / "artifacts"
    artifact_files = (
        [
            str(path)
            for path in sorted(artifacts_dir.rglob("*.csv"))
            if path.is_file() and path.stat().st_size > 0
        ]
        if artifacts_dir.is_dir()
        else []
    )

    metric_rows = store.fetch_metrics(run_id)
    if metric_rows:
        metrics_summary = {
            row["metric_key"]: {
                "unit": row.get("unit"),
                "source": row.get("source"),
                "min": row.get("min"),
                "median": row.get("median"),
                "mean": row.get("mean"),
                "max": row.get("max"),
                "p90": row.get("p90"),
                "sampleCount": row.get("sample_count"),
            }
            for row in metric_rows
        }
    elif artifact_files:
        metrics_summary = summarize_aggregates(collector.aggregate_artifacts(artifacts_dir))
    else:
        metrics_summary = None

    return build_report(
        meta=meta,
        record={
            "sessionId": session.get("solo_session_id"),
            "marks": meta.get("marks", []),
        },
        artifacts=artifact_files,
        metrics_summary=metrics_summary,
        integrity=meta.get("integrity"),
        series=series_from_artifacts(artifacts_dir),
        events=store.fetch_events(run_id),
        # 自定义分级阈值随 meta.json 持久化，重组报告时沿用（口径不漂移）
        jank_thresholds_ms=(meta.get("jank") or {}).get("thresholdsMs"),
        # 逐帧采集元信息同样随 meta.json 持久化，重组报告时口径一致
        frames=meta.get("frames"),
        # 网络剖面结论同样随 meta.json 持久化（档位是可比性维度，不能丢）
        network=meta.get("network"),
    )


def write_report(run_dir: str | Path, report: dict[str, Any]) -> Path:
    path = Path(run_dir) / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


# ---- 单文件 HTML：内嵌离线 ECharts，运行时零外部请求（Q8） ----

_ASSETS_DIR = Path(__file__).resolve().parent / "assets"
_ECHARTS_PATH = _ASSETS_DIR / "echarts.min.js"

# 设计体系：浅色工程仪器面板。纸感灰底 + 白色盘面 + 墨蓝文字，仪器蓝主色，
# 语义色只用于真实判定（完整性/生效/失败/降级），不发明性能好坏结论。
# 离线约束：无 webfont、无 url()、无外部引用（Q8），系统字体栈 + 等宽数字。
_CSS = """
:root {
  --bg: #f4f6f8; --surface: #ffffff; --ink: #1c2430; --muted: #5b6672; --faint: #8a94a1;
  --line: #dfe3e8; --line-soft: #eceff3;
  --accent: #2f5fd0; --accent-ink: #1e4396; --accent-bg: #e9f0fc;
  --ok: #177245; --ok-bg: #e6f4ec;
  --warn: #9a6a00; --warn-bg: #fbf3dd;
  --danger: #b3261e; --danger-bg: #fbe9e7;
  --mono: ui-monospace, "Cascadia Mono", Consolas, monospace;
}
* { box-sizing: border-box; }
html { scroll-padding-top: 60px; }
@media (prefers-reduced-motion: no-preference) { html { scroll-behavior: smooth; } }
body {
  margin: 0; background: var(--bg); color: var(--ink); font-size: 14px; line-height: 1.65;
  font-family: -apple-system, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans SC", sans-serif;
  counter-reset: sec;
}
code { font-family: var(--mono); font-size: 12px; }
.mono { font-family: var(--mono); font-size: 12px; }
a { color: var(--accent-ink); }
a:focus-visible, summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }

.toc { position: sticky; top: 0; z-index: 30; background: var(--surface); border-bottom: 1px solid var(--line); }
.toc-inner { max-width: 1200px; margin: 0 auto; padding: 0 20px; display: flex; overflow-x: auto; scrollbar-width: thin; scrollbar-color: var(--line) transparent; }
.toc-inner::-webkit-scrollbar { height: 4px; }
.toc-inner::-webkit-scrollbar-thumb { background: var(--line); border-radius: 2px; }
.toc-inner a { flex: none; padding: 12px 10px; font-size: 13px; color: var(--muted); text-decoration: none; border-bottom: 2px solid transparent; }
.toc-inner a.toc-brand { font-weight: 650; color: var(--ink); font-family: var(--mono); font-size: 12px; }
.toc-inner a.active { color: var(--accent-ink); border-bottom-color: var(--accent); }
@media (hover: hover) { .toc-inner a:hover { color: var(--ink); } }

.wrap { max-width: 1200px; margin: 0 auto; padding: 0 20px 72px; }
.overview { padding-top: 26px; }
.kicker { font-size: 11px; color: var(--faint); text-transform: uppercase; }
h1 { font-size: 24px; font-weight: 650; letter-spacing: -0.01em; margin: 4px 0 6px; }
.head-sub { font-size: 12px; color: var(--muted); }
.head-sub code { color: var(--muted); }
.head-time { margin-left: 12px; color: var(--faint); }
.chips { margin-top: 12px; display: flex; flex-wrap: wrap; gap: 8px; }
.chip { display: inline-flex; align-items: center; gap: 7px; padding: 3px 11px; border-radius: 999px; font-size: 12px; background: #eef1f5; color: var(--muted); }
.chip.ok { background: var(--ok-bg); color: var(--ok); }
.chip.warn { background: var(--warn-bg); color: var(--warn); }
.chip.danger { background: var(--danger-bg); color: var(--danger); }
.chip.ok::before, .chip.warn::before, .chip.danger::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }

.kpis { margin-top: 18px; display: grid; grid-template-columns: repeat(4, 1fr); gap: 1px; background: var(--line); border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
.kpi { display: block; background: var(--surface); padding: 14px 16px; text-decoration: none; color: inherit; }
.kpi .k-label { display: block; font-size: 12px; color: var(--muted); }
.kpi .k-value { display: block; font-size: 26px; font-weight: 650; font-variant-numeric: tabular-nums; letter-spacing: -0.01em; margin-top: 2px; }
.kpi .k-unit { font-size: 13px; font-weight: 400; color: var(--faint); margin-left: 4px; }
.kpi .k-sub { display: block; font-size: 11px; color: var(--faint); margin-top: 2px; font-variant-numeric: tabular-nums; }
@media (hover: hover) { .kpi:hover { background: #f7f9fc; } }

.section { margin-top: 38px; }
.section h2 { font-size: 16px; font-weight: 650; margin: 0 0 12px; display: flex; align-items: baseline; gap: 10px; }
.section h2::before { counter-increment: sec; content: counter(sec, decimal-leading-zero); font-family: var(--mono); font-size: 12px; font-weight: 400; color: var(--faint); }
.section p { font-size: 12.5px; color: var(--muted); line-height: 1.7; max-width: 78ch; margin: 10px 0 0; }

table { border-collapse: separate; border-spacing: 0; width: 100%; background: var(--surface); border: 1px solid var(--line); border-radius: 10px; overflow: hidden; margin-top: 4px; }
th { font-size: 12px; color: var(--muted); font-weight: 600; text-align: left; padding: 8px 12px; background: #f7f9fb; border-bottom: 1px solid var(--line); }
td { padding: 7px 12px; font-size: 13px; border-bottom: 1px solid var(--line-soft); vertical-align: top; }
tr:last-child td { border-bottom: none; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
@media (hover: hover) { tbody tr:hover td { background: #f7f9fc; } }
tr.group td { background: #f2f5f9; font-size: 12px; font-weight: 600; color: var(--muted); padding: 6px 12px; }
.meta td:first-child { width: 190px; background: #f7f9fb; color: var(--muted); font-size: 12px; font-weight: 500; }

.badge { display: inline-block; padding: 1px 9px; border-radius: 999px; font-size: 12px; white-space: nowrap; }
.badge.ok { color: var(--ok); background: var(--ok-bg); }
.badge.warn { color: var(--warn); background: var(--warn-bg); }
.badge.danger { color: var(--danger); background: var(--danger-bg); }
.badge.muted { color: var(--muted); background: #eef1f5; }
.grade-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 8px; }
.grade-dot.light { background: #b97e0f; }
.grade-dot.heavy { background: #c2600f; }
.grade-dot.severe { background: #b3261e; }
.danger-text { color: var(--danger); font-weight: 600; }

.stat-line { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 14px 18px; }
.stat-value { font-size: 30px; font-weight: 650; font-variant-numeric: tabular-nums; letter-spacing: -0.01em; }
.stat-unit { color: var(--faint); }
.stat-label { color: var(--muted); font-size: 13px; margin-left: 6px; }

.chart-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(430px, 1fr)); gap: 16px; margin-top: 4px; }
.chart-card { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px 10px; margin: 0; }
.chart-card figcaption { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.c-label { font-size: 13px; font-weight: 600; }
.c-en { font-size: 11px; color: var(--faint); }
.c-unit { font-size: 11px; font-family: var(--mono); color: var(--accent-ink); background: var(--accent-bg); padding: 1px 7px; border-radius: 999px; }
.c-src { font-size: 11px; color: var(--faint); margin-left: auto; }
.chart { width: 100%; height: 260px; margin-top: 6px; }
.chart-wide { grid-column: 1 / -1; }
.chart-wide .chart { height: 320px; }
.chart-note { font-size: 11.5px; color: var(--warn); margin: 6px 0 2px; }

details { background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 8px 12px; }
summary { cursor: pointer; font-size: 13px; color: var(--accent-ink); font-weight: 500; }
details[open] summary { margin-bottom: 8px; }
.event-list { margin-top: 12px; }
.artifact-list { padding: 6px 10px; }
.artifact-links { line-height: 1.9; }
.noscript-note { padding: 12px; color: var(--warn); }

@media (max-width: 1000px) { .kpis { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 640px) {
  .wrap { padding: 0 12px 56px; }
  .toc-inner { padding: 0 12px; }
  h1 { font-size: 20px; }
  .kpi .k-value { font-size: 22px; }
  .chart-grid { grid-template-columns: 1fr; }
  .meta td:first-child { width: 110px; }
  th, td { padding: 6px 8px; }
  /* 宽表（聚合 9 列）在窄屏内部横向滚动，不撑破页面 */
  table { display: block; overflow-x: auto; }
}
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
@media print {
  .toc { display: none; }
  body { background: #ffffff; }
  .section { margin-top: 20px; }
  .chart-card { break-inside: avoid; }
}
"""

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mobile-perf 报告 {run_id}</title>
<style>{css}</style>
</head>
<body>
<nav class="toc" aria-label="报告章节"><div class="toc-inner"><a class="toc-brand" href="#overview">mobile-perf</a>{nav_links}</div></nav>
<div class="wrap">
<section id="overview" class="overview">
{header_html}
{kpi_html}
</section>
{body_sections}
</div>
<noscript><p class="noscript-note">曲线图需要 JavaScript（内嵌 ECharts）渲染；聚合统计见上表。</p></noscript>
<!-- vendor:echarts BEGIN -->
<script>
{echarts_js}
</script>
<!-- vendor:echarts END -->
<script id="report-data">window.__REPORT__ = {report_json};</script>
{main_script}
{combined_script}
{scrollspy_script}
</body>
</html>
"""

# 图表主脚本：统一仪器蓝调色板、轴样式、滚轮缩放与 PNG 导出；纵轴离群截断
# 只影响显示（JSON 数据不动），并在图下如实标注离群点数量与真实最大值。
# 注意：零参回调一律用箭头函数，避免与 IIFE 计数断言 "(function () {" 冲突。
_MAIN_SCRIPT = """<script>
(function () {
  var report = window.__REPORT__;
  var series = report.series || {};
  var startup = report.startup || null;
  var PALETTE = ["#2f5fd0", "#0e8f6e", "#c93a32", "#b97e0f", "#7a4fd0", "#0e7f9e", "#c24a8a", "#6f7d1f"];
  var registry = window.__perfCharts = [];
  function axisCap(points) {
    if (!points || points.length < 20) return null;
    var ys = points.map(function (p) { return p[1]; }).sort(function (a, b) { return a - b; });
    var p95 = ys[Math.floor(ys.length * 0.95)];
    var vmax = ys[ys.length - 1];
    if (!(vmax > 10 * Math.max(Math.abs(p95), 1e-9))) return null;
    // 截断基准取 p90：离群簇可污染 p95/p99（真机 fps 毛刺占 7%），p90 仍落在主体内
    var p90 = ys[Math.floor(ys.length * 0.90)];
    if (p90 <= 0) return null;
    var raw = p90 * 1.5;
    var mag = Math.pow(10, Math.floor(Math.log10(raw)));
    var cap = Math.ceil(raw / mag * 2) / 2 * mag;
    var outliers = ys.filter(function (y) { return y > cap; }).length;
    if (!outliers || cap <= 0) return null;
    return { max: cap, outliers: outliers, trueMax: vmax };
  }
  function capNote(el, cap, unit) {
    var p = document.createElement("p");
    p.className = "chart-note";
    p.textContent = "纵轴已截断至 " + cap.max + " " + unit + "：" + cap.outliers +
      " 个离群点超出显示范围（真实最大 " + cap.trueMax + " " + unit + "），数据未改动。";
    el.parentNode.insertBefore(p, el.nextSibling);
  }
  function axisStyle(name) {
    return { type: "value", name: name,
      nameTextStyle: { color: "#8a94a1", fontSize: 11 },
      axisLabel: { color: "#5b6672", fontSize: 11 },
      axisLine: { show: false }, axisTick: { show: false },
      splitLine: { lineStyle: { color: "#eceff3" } } };
  }
  var xAxis = { type: "value", name: "s",
    nameTextStyle: { color: "#8a94a1", fontSize: 11 },
    axisLabel: { color: "#5b6672", fontSize: 11 },
    axisLine: { lineStyle: { color: "#c9cfd8" } },
    splitLine: { show: false } };
  var tooltip = { trigger: "axis", backgroundColor: "#ffffff",
    borderColor: "#dfe3e8", borderWidth: 1,
    textStyle: { color: "#1c2430", fontSize: 12 },
    axisPointer: { type: "line", lineStyle: { color: "#9aa4b2", type: "dashed" } } };
  function toolbox(key) {
    return { right: 6, top: 0, itemSize: 14,
      iconStyle: { borderColor: "#8a94a1" },
      emphasis: { iconStyle: { borderColor: "#2f5fd0" } },
      feature: { saveAsImage: { title: "导出 PNG", name: report.runId + "-" + key,
        pixelRatio: 2, backgroundColor: "#ffffff" } } };
  }
  window.__perfViz = { palette: PALETTE, axisCap: axisCap, capNote: capNote,
    axisStyle: axisStyle, xAxis: xAxis, tooltip: tooltip, toolbox: toolbox };
  Object.keys(series).forEach(function (key) {
    var item = series[key];
    var el = document.getElementById("chart-" + key);
    if (!el || typeof echarts === "undefined") return;
    var chart = echarts.init(el);
    var yAxis = axisStyle(item.unit);
    var cap = axisCap(item.points);
    if (cap) yAxis.max = cap.max;
    var option = {
      color: PALETTE,
      tooltip: tooltip,
      xAxis: xAxis,
      yAxis: yAxis,
      series: [{
        type: "line", showSymbol: false, symbol: "circle", symbolSize: 5,
        lineStyle: { width: 1.6 }, areaStyle: { opacity: 0.07 },
        data: item.points.map(function (p) { return [p[0], p[1]]; })
      }],
      dataZoom: [{ type: "inside", xAxisIndex: 0 }],
      toolbox: toolbox(key),
      grid: { left: 52, right: 18, top: 26, bottom: 30 }
    };
__STARTUP_MARKLINE__
__PAGE_MARKLINE__
__JANK_THRESHOLD_LINE__
    chart.setOption(option);
    if (cap) capNote(el, cap, item.unit);
    registry.push(chart);
  });
  var resizePending = false;
  window.addEventListener("resize", () => {
    if (resizePending) return;
    resizePending = true;
    requestAnimationFrame(() => {
      resizePending = false;
      registry.forEach(function (c) { c.resize(); });
    });
  });
})();
</script>"""

# 章节导航 scroll-spy：IntersectionObserver 不可用时退化为纯锚点（老 WebView）
_SCROLLSPY_SCRIPT = """<script>
(function () {
  if (!("IntersectionObserver" in window)) return;
  var links = Array.prototype.slice.call(document.querySelectorAll(".toc-inner a[href^='#']"));
  var byId = {};
  links.forEach(function (a) { byId[a.getAttribute("href").slice(1)] = a; });
  var current = null;
  var obs = new IntersectionObserver(function (entries) {
    entries.forEach(function (en) {
      if (!en.isIntersecting) return;
      var a = byId[en.target.id];
      if (!a) return;
      if (current) { current.classList.remove("active"); current.removeAttribute("aria-current"); }
      a.classList.add("active");
      a.setAttribute("aria-current", "true");
      current = a;
    });
  }, { rootMargin: "-60px 0px -75% 0px", threshold: 0 });
  document.querySelectorAll("section[id]").forEach(function (s) { obs.observe(s); });
})();
</script>"""


def write_html(run_dir: str | Path, report: dict[str, Any]) -> Path:
    """生成单文件 HTML 报告（内嵌离线 ECharts 与同源报告数据，无外部请求）。"""
    echarts_js = _ECHARTS_PATH.read_text(encoding="utf-8")
    # 避免 vendor 代码里可能的 </script> 提前闭合标签
    echarts_js = echarts_js.replace("</script", "<\\/script").replace("<!--", "<\\!--")
    report_json = json.dumps(report, ensure_ascii=False).replace("</", "<\\/")

    sections, combined_script = _body_sections(report, run_dir)
    section_ids = {sid for sid, _, _ in sections}
    nav_links = "".join(
        f'<a href="#{sid}">{html_escape(label)}</a>' for sid, label, _ in sections
    )
    body_sections = "\n".join(
        f'<section id="{sid}" class="section">\n{content}\n</section>'
        for sid, _, content in sections
    )

    html_text = _HTML_TEMPLATE.format(
        run_id=html_escape(report["runId"]),
        css=_CSS,
        nav_links=nav_links,
        header_html=_header_html(report),
        kpi_html=_kpi_html(report, section_ids),
        body_sections=body_sections,
        echarts_js=echarts_js,
        report_json=report_json,
        main_script=_MAIN_SCRIPT,
        combined_script=combined_script,
        scrollspy_script=_SCROLLSPY_SCRIPT,
    )
    # 启动点标记按需注入（占位符在 format 之后替换，避免花括号转义）
    html_text = html_text.replace("__STARTUP_MARKLINE__", _startup_markline_script(report))
    html_text = html_text.replace("__PAGE_MARKLINE__", _page_markline_script(report))
    html_text = html_text.replace(
        "__JANK_THRESHOLD_LINE__", _jank_threshold_line_script(report)
    )
    path = Path(run_dir) / "report.html"
    path.write_text(html_text, encoding="utf-8")
    return path


# 指标分组：聚合表与曲线网格共用同一顺序（帧率/卡顿在最前，网络/稳定性殿后）
_METRIC_GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("启动与页面", ("app_cold_start_ms", "app_warm_start_ms", "page_load_ms")),
    ("帧率与卡顿", (
        "fps_frame", "frame_delay_max_ms", "frame_delay_count", "frame_delay_ratio_pct",
        "app_jank_light_count", "app_jank_heavy_count", "app_jank_severe_count",
        "app_frame_p50_ms", "app_frame_p90_ms", "app_frame_p99_ms",
        "app_frame_jank_ratio_pct", "app_frame_frozen_ratio_pct",
        "app_frame_jank_streak_max", "app_frame_count",
    )),
    ("CPU 与热", (
        "cpu_process_pct", "cpu_global_pct", "cpu_freq_max_mhz",
        "device_thermal_status", "temp_battery_c",
    )),
    ("内存", ("mem_pss_mb", "mem_privdirty_mb", "mem_global_mb", "mem_pss_slope_mb_per_day")),
    ("GPU", ("gpu_util_pct", "gpu_mem_mb")),
    ("电池", (
        "battery_current_avg_ma", "battery_current_ma",
        "battery_power_avg_mw", "battery_power_mw", "battery_consumed_mah", "battery_pct",
    )),
    ("网络与接口", (
        "net_conn_duration_ms", "net_rx_kbps", "net_tx_kbps", "net_rx_total_kb", "net_tx_total_kb",
        "api_req_duration_ms", "api_req_count", "api_error_rate_pct", "api_payload_kb",
    )),
    ("稳定性与资源", ("app_crash_count", "app_anr_count", "app_thread_count", "app_fd_count")),
]


def _ordered_keys(keys: Any) -> list[str]:
    """按 _METRIC_GROUPS 排序，字典外 key 排最后（保持原相对顺序）。"""
    remaining = list(keys)
    ordered = []
    for _, group_keys in _METRIC_GROUPS:
        for key in group_keys:
            if key in remaining:
                ordered.append(key)
                remaining.remove(key)
    return ordered + remaining


def _body_sections(report: dict[str, Any], run_dir: str | Path) -> tuple[list[tuple[str, str, str]], str]:
    """按固定顺序收集非空小节；返回 (小节列表, 同图脚本)。导航与正文同源。"""
    charts_html, combined_script = _charts_grid_html(report)
    candidates = [
        ("startup", "启动", _startup_markup(report)),
        ("jank", "卡顿", _jank_markup(report)),
        ("frames", "逐帧", _frame_markup(report)),
        ("gpu", "GPU", _gpu_markup(report)),
        ("network", "网络", _network_markup(report)),
        ("api", "接口", _api_markup(report)),
        ("pages", "整页", _page_markup(report)),
        ("summary", "聚合", _summary_html(report)),
        ("charts", "曲线", charts_html),
        ("meta", "元信息", _meta_html(report, run_dir)),
    ]
    return [(sid, label, content) for sid, label, content in candidates if content], combined_script


def _header_html(report: dict[str, Any]) -> str:
    """页头：被测应用为主标题，run/时间为辅，状态 chips 只反映真实判定。"""
    target = html_escape(str(report.get("target") or "未知应用"))
    run_id = html_escape(str(report.get("runId") or ""))
    started = html_escape(str(report.get("startedAt") or ""))
    stopped = html_escape(str(report.get("stoppedAt") or ""))
    chips: list[str] = []
    device_text = " · ".join(
        str(x) for x in (report.get("deviceProfile"), report.get("serial")) if x
    )
    if device_text:
        chips.append(f'<span class="chip">{html_escape(device_text)}</span>')
    if report.get("durationS") is not None:
        chips.append(f'<span class="chip">时长 {_fmt(report["durationS"])} s</span>')
    integrity = report.get("integrity") or {}
    if integrity:
        check = str(integrity.get("deviceRowCheck") or "未知")
        kind = "ok" if check == "passed" else "warn"
        chips.append(f'<span class="chip {kind}">完整性 {html_escape(check)}</span>')
    network = report.get("network") or {}
    if network:
        effective = bool(network.get("effective"))
        kind = "ok" if effective else "warn"
        text = "网络生效" if effective else "网络未生效"
        chips.append(f'<span class="chip {kind}">{text}</span>')
    startup = report.get("startup") or {}
    if startup and startup.get("ms") is None:
        chips.append('<span class="chip danger">启动失败</span>')
    return (
        '<header class="page-head">'
        '<div class="kicker">mobile-perf 性能报告</div>'
        f"<h1>{target}</h1>"
        f'<div class="head-sub"><code>{run_id}</code>'
        f'<span class="head-time">{started} → {stopped}</span></div>'
        f'<div class="chips">{"".join(chips)}</div>'
        "</header>"
    )


def _kpi_html(report: dict[str, Any], section_ids: set[str]) -> str:
    """KPI 数字带：只呈现本 run 真实存在的指标，点击跳到对应曲线/小节。

    不做性能好坏判定（阈值归宿主/judge），数值一律墨色；副行给 P90/峰值等
    第二口径，避免单数字下结论。
    """
    summary = report.get("metricsSummary") or {}
    series = report.get("series") or {}
    items: list[tuple[str, str, str, str, str]] = []

    def href_for(key: str, section: str) -> str:
        if key in series:
            return f"#chart-{key}"
        return f"#{section}" if section in section_ids else "#summary"

    startup = report.get("startup") or {}
    if isinstance(startup.get("ms"), (int, float)):
        spec = metrics_module.METRICS.get(startup.get("metricKey") or "")
        label = spec.label_zh if spec else "启动耗时"
        sub_bits = []
        if startup.get("displayedMs") is not None:
            sub_bits.append(f"Displayed {_fmt(startup['displayedMs'])}")
        if startup.get("deviationMs") is not None:
            sub_bits.append(f"偏差 {_fmt(startup['deviationMs'])}")
        items.append((label, _fmt(startup["ms"]), "ms", " / ".join(sub_bits) + " ms" if sub_bits else "", "#startup"))

    def add(key: str, label: str, stat: str = "median", sub: str = "", section: str = "summary"):
        entry = summary.get(key) or {}
        value = entry.get(stat)
        if value is None:
            return
        if not sub and entry.get("p90") is not None:
            sub = f"P90 {_fmt(entry['p90'])}"
        items.append((label, _fmt(value), str(entry.get("unit") or ""), sub, href_for(key, section)))

    add("fps_frame", "帧率（中位）")
    frozen = (summary.get("app_frame_frozen_ratio_pct") or {}).get("median")
    add(
        "app_frame_jank_ratio_pct", "超时帧占比",
        sub=f"冻帧占比 {_fmt(frozen)}%" if frozen is not None else "", section="frames",
    )
    p50 = (summary.get("app_frame_p50_ms") or {}).get("median")
    p90 = (summary.get("app_frame_p90_ms") or {}).get("median")
    add(
        "app_frame_p99_ms", "帧耗时 P99",
        sub=f"P50 {_fmt(p50)} · P90 {_fmt(p90)}" if p50 is not None and p90 is not None else "",
        section="frames",
    )
    add("cpu_process_pct", "应用 CPU（中位）")
    mem_max = (summary.get("mem_pss_mb") or {}).get("max")
    add("mem_pss_mb", "PSS 内存（中位）", sub=f"峰值 {_fmt(mem_max)}" if mem_max is not None else "")
    power = (summary.get("battery_power_avg_mw") or {}).get("median")
    add(
        "battery_current_avg_ma", "平均电流",
        sub=f"平均功率 {_fmt(power)} mW" if power is not None else "",
    )
    conn_n = (summary.get("net_conn_duration_ms") or {}).get("sampleCount")
    add(
        "net_conn_duration_ms", "连接耗时 P90", stat="p90",
        sub=f"{conn_n} 次样本" if conn_n is not None else "", section="network",
    )
    if not items:
        return ""
    cells = "".join(
        f'<a class="kpi" href="{html_escape(href)}"><span class="k-label">{html_escape(label)}</span>'
        f'<span class="k-value">{html_escape(value)}<span class="k-unit">{html_escape(unit)}</span></span>'
        f'<span class="k-sub">{html_escape(sub)}</span></a>'
        for label, value, unit, sub, href in items
    )
    return f'<div class="kpis">{cells}</div>'


def _summary_html(report: dict[str, Any]) -> str:
    """聚合统计：按指标域分组，数字右对齐 + 等宽数字。"""
    summary = report.get("metricsSummary") or {}
    if not summary:
        return ""
    group_of = {key: name for name, keys in _METRIC_GROUPS for key in keys}
    rows: list[str] = []
    last_group = None
    for key in _ordered_keys(summary):
        entry = summary[key]
        group = group_of.get(key, "其他")
        if group != last_group:
            rows.append(f'<tr class="group"><td colspan="9">{html_escape(group)}</td></tr>')
            last_group = group
        rows.append(
            '<tr><td>{label}</td><td>{unit}</td><td class="mono">{source}</td>'
            '<td class="num">{min}</td><td class="num">{median}</td><td class="num">{mean}</td>'
            '<td class="num">{max}</td><td class="num">{p90}</td><td class="num">{count}</td></tr>'.format(
                label=html_escape(
                    _bilingual(entry.get("labelZh"), entry.get("labelEn"), key)
                ),
                unit=html_escape(str(entry.get("unit") or "")),
                source=html_escape(str(entry.get("source") or "")),
                min=_fmt(entry.get("min")),
                median=_fmt(entry.get("median")),
                mean=_fmt(entry.get("mean")),
                max=_fmt(entry.get("max")),
                p90=_fmt(entry.get("p90")),
                count=entry.get("sampleCount", ""),
            )
        )
    return (
        "<h2>聚合统计 / Aggregates</h2>"
        '<table class="summary-table">'
        '<tr><th>指标</th><th>单位</th><th>来源</th><th class="num">min</th>'
        '<th class="num">median</th><th class="num">mean</th>'
        '<th class="num">max</th><th class="num">p90</th><th class="num">样本数</th></tr>'
        f"{''.join(rows)}</table>"
    )


def _charts_grid_html(report: dict[str, Any]) -> tuple[str, str]:
    """曲线网格：同图（宽卡）在前，单指标两列网格；返回 (网格 HTML, 同图脚本)。"""
    series = report.get("series") or {}
    if not series:
        return "", ""
    combined_div, combined_script = _combined_thermal_markup(series)
    figures = []
    for key in _ordered_keys(series):
        item = series[key]
        figures.append(
            '<figure class="chart-card"><figcaption>'
            f'<span class="c-label">{html_escape(str(item.get("labelZh") or key))}</span>'
            f'<span class="c-en">{html_escape(str(item.get("labelEn") or ""))}</span>'
            f'<span class="c-unit">{html_escape(str(item.get("unit") or ""))}</span>'
            f'<span class="c-src">{html_escape(str(item.get("source") or ""))}</span>'
            "</figcaption>"
            f'<div class="chart" id="chart-{html_escape(key)}"></div></figure>'
        )
    grid = f'<div class="chart-grid">{combined_div}{"".join(figures)}</div>'
    return "<h2>指标曲线 / Trends</h2>" + grid, combined_script


def _meta_html(report: dict[str, Any], run_dir: str | Path) -> str:
    meta_rows = "".join(
        f"<tr><td>{html_escape(name)}</td>"
        f"<td>{value if name.startswith(('产物', '报告产物')) else html_escape(value)}</td></tr>"
        for name, value in _meta_rows(report, run_dir)
    )
    return (
        "<h2>会话元信息 / Session</h2>"
        f'<table class="meta">{meta_rows}</table>'
        + _events_html(report)
    )


def _events_html(report: dict[str, Any]) -> str:
    """事件列表（折叠）：只展示 store 已落盘的事件，不新增数据。"""
    events = report.get("events") or []
    if not events:
        return ""
    rows = []
    for ev in sorted(events, key=lambda e: str(e.get("ts") or "")):
        kind = str(ev.get("kind") or "")
        rows.append(
            f'<tr><td class="mono">{html_escape(str(ev.get("ts") or ""))}</td>'
            f'<td><span class="badge muted">{html_escape(kind)}</span></td>'
            f"<td>{html_escape(_event_summary(kind, ev.get('detail') or {}))}</td></tr>"
        )
    return (
        f'<details class="event-list"><summary>事件 / Events（{len(events)}）</summary>'
        "<table><tr><th>时间 / Time</th><th>类型 / Kind</th><th>摘要 / Detail</th></tr>"
        f"{''.join(rows)}</table></details>"
    )


def _event_summary(kind: str, detail: dict[str, Any]) -> str:
    if kind == "startup":
        if detail.get("ms") is not None:
            return f"{detail.get('mode')} {_fmt(detail['ms'])} ms"
        return f"启动失败：{detail.get('error') or '未知原因'}"
    if kind == "foreground":
        restored = "已恢复" if detail.get("restored") else "未恢复"
        return (
            f"{detail.get('phase')}：前台为 {detail.get('observed')}"
            f"（期望 {detail.get('before')}），{restored}"
        )
    if kind == "net_profile":
        profile = detail.get("profile") or {}
        effective = "生效" if detail.get("effective") else "未生效"
        return f"档位 {profile.get('name')} / {detail.get('tier')}，{effective}"
    if kind == "mark":
        return str(detail.get("label") or "")
    if kind == "page_load":
        return f"{detail.get('page')} {_fmt(detail.get('pageLoadMs'))} ms"
    if kind == "capture_har":
        return f"HAR {detail.get('status')}，{detail.get('entries', 0)} 条"
    if kind == "integrity_warning":
        return str(detail.get("reason") or "")
    return json.dumps(detail, ensure_ascii=False)


_COMBINED_CHART_ID = "chart-combined-thermal-fps"

# 温度-帧率同图：帧率走左轴，补采曲线按**单位**分组各占一条右轴，共用统一
# 时间轴（§6.3）。单位必须分组——把 °C、%、MB 挤在同一条轴上会让人误读量级。
_COMBINED_SCRIPT = """<script>
(function () {
  var viz = window.__perfViz;
  var report = window.__REPORT__ || {};
  var series = report.series || {};
  var startup = report.startup || null;
  var fps = series["fps_frame"];
  var supplement = [["temp_battery_c", series["temp_battery_c"]],
                    ["gpu_util_pct", series["gpu_util_pct"]],
                    ["gpu_mem_mb", series["gpu_mem_mb"]]].filter(function (pair) { return pair[1]; });
  if (!viz || !fps || supplement.length === 0 || typeof echarts === "undefined") return;
  var el = document.getElementById("__CHART_ID__");
  if (!el) return;
  var chart = echarts.init(el);
  var COLORS = { fps_frame: "#2f5fd0", temp_battery_c: "#c93a32",
                 gpu_util_pct: "#0e8f6e", gpu_mem_mb: "#7a4fd0" };
  var notes = [];
  function cappedAxis(unit, label, points, position, offset) {
    var axis = viz.axisStyle(unit);
    axis.position = position;
    if (offset) axis.offset = offset;
    if (position === "right") axis.splitLine = { show: false };
    var cap = viz.axisCap(points);
    if (cap) {
      axis.max = cap.max;
      notes.push(label + " 截断至 " + cap.max + " " + unit + "（" + cap.outliers +
        " 个离群点，真实最大 " + cap.trueMax + " " + unit + "）");
    }
    return axis;
  }
  var lines = [{
    name: fps.labelZh + " (" + fps.unit + ")", type: "line", showSymbol: false, yAxisIndex: 0,
    lineStyle: { width: 1.6 }, itemStyle: { color: COLORS.fps_frame },
    data: fps.points.map(function (p) { return [p[0], p[1]]; })
  }];
__STARTUP_MARKLINE__
__PAGE_MARKLINE__
  var axes = [cappedAxis(fps.unit, fps.labelZh, fps.points, "left", 0)];
  var axisByUnit = {};
  axisByUnit[fps.unit] = 0;
  supplement.forEach(function (pair) {
    var key = pair[0], item = pair[1];
    if (axisByUnit[item.unit] === undefined) {
      axisByUnit[item.unit] = axes.length;
      axes.push(cappedAxis(item.unit, item.labelZh, item.points, "right", (axes.length - 1) * 48));
    }
    lines.push({
      name: item.labelZh + " (" + item.unit + ")", type: "line", showSymbol: false,
      yAxisIndex: axisByUnit[item.unit], lineStyle: { width: 1.6 },
      itemStyle: { color: COLORS[key] },
      data: item.points.map(function (p) { return [p[0], p[1]]; })
    });
  });
  chart.setOption({
    tooltip: viz.tooltip,
    legend: { top: 0, textStyle: { color: "#5b6672", fontSize: 11 } },
    xAxis: viz.xAxis,
    yAxis: axes,
    series: lines,
    dataZoom: [{ type: "inside", xAxisIndex: 0 }],
    toolbox: viz.toolbox("combined-thermal-fps"),
    grid: { left: 52, right: 16 + 48 * (axes.length - 1), top: 34, bottom: 30 }
  });
  if (notes.length) {
    var p = document.createElement("p");
    p.className = "chart-note";
    p.textContent = "纵轴已截断：" + notes.join("；") + "。数据未改动。";
    el.parentNode.insertBefore(p, el.nextSibling);
  }
  (window.__perfCharts = window.__perfCharts || []).push(chart);
})();
</script>"""


def _combined_thermal_markup(series: dict[str, Any]) -> tuple[str, str]:
    """有帧率且有补采曲线时返回（同图宽卡，同图脚本）；否则返回空串。"""
    has_fps = "fps_frame" in series
    has_supplement = any(
        key in series for key in ("temp_battery_c", "gpu_util_pct", "gpu_mem_mb")
    )
    if not (has_fps and has_supplement):
        return "", ""
    div = (
        '<figure class="chart-card chart-wide"><figcaption>'
        '<span class="c-label">温度-帧率同图</span>'
        '<span class="c-en">Thermal / FPS / GPU（统一时间轴）</span>'
        '<span class="c-src">solo_pi + pc_supplement</span>'
        "</figcaption>"
        f'<div class="chart" id="{_COMBINED_CHART_ID}"></div></figure>'
    )
    return div, _COMBINED_SCRIPT.replace("__CHART_ID__", _COMBINED_CHART_ID)


def _page_markup(report: dict[str, Any]) -> str:
    """整页加载耗时小节（ticket 19）：逐页一行，不可判定的页如实写原因。

    与启动耗时分开呈现：启动耗时从进程拉起算起，整页耗时从**页面打标**算起，
    两者的计时原点不同，混在一张表里会被读成同一件事。
    """
    pages = report.get("pages")
    if not pages:
        return ""
    rows = []
    for item in pages.get("pages") or []:
        status = str(item.get("status") or "")
        value = item.get("pageLoadMs")
        status_kind = {"ok": "ok", "timeout": "warn"}.get(status, "muted")
        status_cell = (
            f'<span class="badge {status_kind}">'
            f"{html_escape(_PAGE_STATUS_TEXT.get(status, status))}</span>"
        )
        rows.append(
            '<tr><td>{page}</td><td class="num">{value}</td><td>{status}</td>'
            '<td class="num">{frames}</td><td>{reason}</td></tr>'.format(
                page=html_escape(str(item.get("page") or "")),
                value=_fmt(value) if value is not None else "not_tested",
                status=status_cell,
                frames=item.get("frameCount", ""),
                reason=html_escape(str(item.get("reason") or "")),
            )
        )
    offset_source = str(pages.get("clockOffsetSource") or "")
    offset_text = {
        "frame_anchor": "逐帧轮询锚点换算（每轮宿主时刻与最新帧时刻配对取最小）",
        "no_frames_anchor": "逐帧窗口内没有可用帧时刻，无法换算，本 run 不硬算",
        "no_frame_poll": "未开启逐帧采集，没有帧完成时刻",
    }.get(offset_source, offset_source or "未知")
    if pages.get("clockAnchorCount") is not None:
        offset_text += f"；{pages['clockAnchorCount']} 个锚点"
        if pages.get("clockPrecisionMs") is not None:
            offset_text += f"，误差不超过 ±{pages['clockPrecisionMs']} ms（半个 adb 往返）"
    parts = [
        "<h2>整页加载耗时 / Page load</h2>",
        '<table class="page-load"><tr><th>页面 / Page</th>'
        '<th class="num">整页耗时 / Load (ms)</th>'
        '<th>判定 / Status</th><th class="num">帧数 / Frames</th><th>说明 / Note</th></tr>'
        f"{''.join(rows)}</table>",
        "<p>口径：从页面打标到画面进入稳定的耗时（首个连续 "
        f"≥{html_escape(str(pages.get('quietGapMs', '')))} ms 无新帧的窗口起点）；"
        f"超过 {html_escape(str(pages.get('timeoutMs', '')))} ms 仍无静默则判不可判定。"
        f"时钟换算 / Clock：{html_escape(offset_text)}。"
        "「稳定」是<b>视觉稳定</b>，不保证懒加载数据已就绪；首帧前的空白期计入耗时。"
        "本指标与启动耗时口径不同、不可互相换算。</p>",
    ]
    return "".join(parts)


_PAGE_STATUS_TEXT = {
    "ok": "稳定 / stable",
    "timeout": "超时（持续动画）/ timeout",
    "no_frames": "无帧 / no frames",
    "incomplete": "观测不足 / incomplete",
}


def _page_markline_script(report: dict[str, Any]) -> str:
    """整页耗时标注脚本块（ticket 19）：有可判定页面才在时间轴上画竖线。"""
    pages = report.get("pages") or {}
    points = [
        {"s": item.get("stableAtS"), "ms": item.get("pageLoadMs"), "page": item.get("page")}
        for item in pages.get("pages") or []
        if item.get("stableAtS") is not None and item.get("pageLoadMs") is not None
    ]
    if not points:
        return ""
    return """(function () {
    var points = __PAGE_POINTS__;
    var mark = {
      symbol: "none",
      lineStyle: { color: "#2f7d32", type: "dashed" },
      label: { formatter: function (p) { return p.name; }, fontSize: 10,
        position: "end", align: "left", verticalAlign: "bottom", distance: 2 },
      data: points.map(function (p) {
        return { xAxis: p.s, name: p.page + " " + p.ms + " ms" };
      })
    };
    // 启动点竖线可能已经占用了 markLine：追加而不是覆盖，两者都该看到
    var host = null;
    if (typeof option !== "undefined" && option.series && option.series.length) {
      host = option.series[0];
    } else if (typeof lines !== "undefined" && lines.length > 0) {
      host = lines[0];
    }
    if (!host) return;
    if (host.markLine && host.markLine.data) {
      host.markLine.data = host.markLine.data.concat(mark.data);
    } else {
      host.markLine = mark;
    }
  })();
  """.replace("__PAGE_POINTS__", json.dumps(points, ensure_ascii=False))


def _gpu_markup(report: dict[str, Any]) -> str:
    """GPU 小节（工单 16）：利用率/显存的聚合值、实际数据源与降级链原因。

    数据源必须写出来——`kgsl_gpubusy` 是驱动侧 ~1 s 窗口的累计值（窗口平均），
    与 `dumpsys gpu` 的瞬时读数口径不同，混着看会得出错误结论。
    """
    summary = report.get("metricsSummary") or {}
    gpu = report.get("gpu") or {}
    rows = [
        '<tr><td>{label}</td><td class="num">{median}</td><td class="num">{p90}</td>'
        '<td class="num">{max}</td><td class="num">{count}</td><td>{unit}</td></tr>'.format(
            label=html_escape(_bilingual(entry.get("labelZh"), entry.get("labelEn"), key)),
            median=_fmt(entry.get("median")),
            p90=_fmt(entry.get("p90")),
            max=_fmt(entry.get("max")),
            count=entry.get("sampleCount", ""),
            unit=html_escape(str(entry.get("unit") or "")),
        )
        for key in ("gpu_util_pct", "gpu_mem_mb")
        if (entry := summary.get(key)) is not None
    ]
    degraded = [
        item for item in (gpu.get("degraded") or [])
        if item.get("key") in ("gpu_util_pct", "gpu_mem_mb")
    ]
    if not rows and not degraded:
        return ""
    source = gpu.get("gpuSource")
    source_text = {
        "kgsl_gpubusy": "kgsl <code>gpubusy</code> 窗口计数（窗口平均，非瞬时）",
        "dumpsys_gpu": "<code>dumpsys gpu</code> 文本行（瞬时读数）",
    }.get(source or "", "未取得")
    parts = [
        "<h2>GPU / GPU</h2>",
        '<table class="gpu-stats"><tr><th>指标 / Metric</th><th class="num">中位 / Median</th>'
        '<th class="num">P90</th><th class="num">最大 / Max</th>'
        '<th class="num">样本 / Samples</th><th>单位 / Unit</th></tr>'
        f"{''.join(rows)}</table>",
        f"<p>数据源 / Source：{source_text}。"
        "利用率与显存只报聚合（median/p90/max），不承诺逐点序列与真实瞬时负载对齐。"
        "该比值的语义按机型不同（同一负载在不同 GPU/系统上可差一倍以上，"
        "部分机型静止桌面读数高于滑动负载），"
        "绝对值不可跨机型比较，阈值须按机型建档。</p>",
    ]
    if degraded:
        items = "；".join(
            f"{html_escape(str(item.get('key')))}（{html_escape(str(item.get('reason')))}"
            + (
                f"：{' → '.join(html_escape(str(s)) for s in item['stages'])}"
                if item.get("stages")
                else ""
            )
            + "）"
            for item in degraded
        )
        parts.append(f"<p>⚠️ 降级项 / Degraded：{items}。降级项不产出 0 值指标。</p>")
    return "".join(parts)


def _startup_markline_script(report: dict[str, Any]) -> str:
    """启动点 markLine 脚本块（ticket 12）：有启动编排且计得 ms 才注入，否则替换为空。"""
    startup = report.get("startup")
    if not startup or not isinstance(startup.get("elapsedS"), (int, float)):
        return ""
    if not isinstance(startup.get("ms"), (int, float)):
        return ""
    return """if (startup && typeof startup.elapsedS === "number") {
    var mark = {
      symbol: "none",
      lineStyle: { color: "#d0483e", type: "dashed" },
      label: { formatter: "启动 " + startup.ms + " ms", fontSize: 10,
        position: "end", align: "left", verticalAlign: "bottom", distance: 2 },
      data: [{ xAxis: startup.elapsedS }]
    };
    if (typeof option !== "undefined") { option.series[0].markLine = mark; }
    else if (typeof lines !== "undefined" && lines.length > 0) { lines[0].markLine = mark; }
  }
  """


def _startup_markup(report: dict[str, Any]) -> str:
    """启动时间小节（ticket 12）：无启动编排返回空串；启动失败如实呈现 error。"""
    startup = report.get("startup")
    if not startup:
        return ""
    metric = startup.get("metricKey") or ""
    spec = metrics_module.METRICS.get(metric)
    label = _bilingual(spec.label_zh, spec.label_en, metric) if spec else metric
    header = f"{html_escape(label)}（<code>{html_escape(metric)}</code>）"
    ms = startup.get("ms")
    if ms is None:
        return (
            '<h2>启动时间 / Startup</h2>'
            f'<p><span class="badge danger">启动失败</span> {header}：'
            f"{html_escape(str(startup.get('error') or '未知原因'))}</p>"
        )
    bits: list[str] = []
    if startup.get("launchState"):
        bits.append(f"LaunchState={startup['launchState']}")
    if startup.get("displayedMs") is not None:
        deviation = startup.get("deviationMs")
        deviation_text = f"，偏差 {_fmt(deviation)} ms" if deviation is not None else ""
        bits.append(f"logcat Displayed {_fmt(startup['displayedMs'])} ms{deviation_text}")
    if startup.get("fullyDrawnMs") is not None:
        bits.append(f"Fully drawn {_fmt(startup['fullyDrawnMs'])} ms")
    if startup.get("elapsedS") is not None:
        bits.append(f"启动点 {startup['elapsedS']} s")
    detail = "；".join(html_escape(bit) for bit in bits)
    return (
        '<h2>启动时间 / Startup</h2>'
        '<div class="stat-line">'
        f'<span class="stat-value">{_fmt(ms)}</span><span class="stat-unit">ms</span>'
        f'<span class="stat-label">{header}</span>'
        "</div>"
        + (f"<p>{detail}</p>" if detail else "")
    )


# 卡顿分级小节（ticket 14）：三档阈值线与口径说明。阈值归宿主时由报告
# 数据带出（report["jank"]["thresholdsMs"]），缺省用组件默认口径。
_JANK_GRADE_LABELS = {
    jank_module.GRADE_LIGHT: ("轻度", "Light"),
    jank_module.GRADE_HEAVY: ("重度", "Heavy"),
    jank_module.GRADE_SEVERE: ("严重", "Severe"),
}


def _jank_thresholds(report: dict[str, Any]) -> dict[str, float]:
    configured = (report.get("jank") or {}).get("thresholdsMs") or {}
    return {**jank_module.DEFAULT_THRESHOLDS_MS, **configured}


def _jank_entries(report: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    summary = report.get("metricsSummary") or {}
    return [
        (grade, summary[key])
        for grade, key in jank_module.GRADE_METRIC_KEYS.items()
        if key in summary
    ]


def _jank_markup(report: dict[str, Any]) -> str:
    """卡顿分级小节：三档计数 + 口径说明；无分级指标返回空串。"""
    entries = _jank_entries(report)
    if not entries:
        return ""
    thresholds = _jank_thresholds(report)
    rows = []
    for grade, entry in entries:
        zh, en = _JANK_GRADE_LABELS[grade]
        label = f"{zh}卡顿窗口数(>{_fmt(thresholds[grade])}ms) / {en} jank windows"
        rows.append(
            '<tr><td><span class="grade-dot {grade}"></span>{label}</td>'
            '<td class="num">{count}</td><td class="num">{samples}</td></tr>'.format(
                grade=grade,
                label=html_escape(label),
                count=_fmt(entry.get("max")),
                samples=entry.get("sampleCount", ""),
            )
        )
    return (
        "<h2>卡顿分级 / Jank grading</h2>"
        '<table class="jank-threshold"><tr><th>分级 / Grade</th>'
        '<th class="num">窗口数 / Windows</th>'
        "<th class=\"num\">样本数 / Samples</th></tr>{rows}</table>"
        "<p>口径 / Method：SoloPi 无逐帧延迟分布，按每个采样窗口的"
        "「最长延迟时间」是否超过阈值计数（分桶互斥，只计最高档）。</p>"
    ).format(rows="\n".join(rows))


def _frame_markup(report: dict[str, Any]) -> str:
    """逐帧小节（ticket 15）：P50/P90/P99 + 超时帧占比 + 与窗口级口径的差异说明。

    与「卡顿分级」并列呈现，并明确写出两种口径粒度不同、不可互算。
    """
    summary = report.get("metricsSummary") or {}
    frames = report.get("frames") or {}
    rows_spec = (
        ("app_frame_p50_ms", "帧耗时 P50 / Frame P50"),
        ("app_frame_p90_ms", "帧耗时 P90 / Frame P90"),
        ("app_frame_p99_ms", "帧耗时 P99 / Frame P99"),
        ("app_frame_jank_ratio_pct", "超时帧占比 / Janky frame ratio"),
        ("app_frame_frozen_ratio_pct", "冻帧占比 / Frozen frame ratio"),
        ("app_frame_jank_streak_max", "最长连续超时帧段 / Max jank streak"),
        ("app_frame_count", "采样帧数 / Sampled frames"),
    )
    rows = [
        '<tr><td>{label}</td><td class="num">{value}</td><td>{unit}</td></tr>'.format(
            label=html_escape(label),
            value=_fmt((summary.get(key) or {}).get("median")),
            unit=html_escape(str((summary.get(key) or {}).get("unit") or "")),
        )
        for key, label in rows_spec
        if key in summary
    ]
    if not rows:
        return ""
    refresh = frames.get("refreshHz")
    multiplier = frames.get("jankMultiplier")
    detail = []
    if refresh:
        detail.append(f"刷新率 / Refresh ≈ {_fmt(refresh)} Hz")
    if multiplier:
        detail.append(f"超时判据 / Jank criterion：帧耗时 > 刷新周期 × {_fmt(multiplier)}")
    frozen_ms = frames.get("frozenThresholdMs")
    if frozen_ms:
        detail.append(f"冻帧判据 / Frozen：帧耗时 > {_fmt(frozen_ms)} ms")
    detail_text = ("；".join(detail) + "。") if detail else ""
    return (
        "<h2>逐帧卡顿 / Per-frame jank</h2>"
        '<table class="frame-stats"><tr><th>指标 / Metric</th><th class="num">值 / Value</th>'
        "<th>单位 / Unit</th></tr>{rows}</table>"
        "<p>数据源 / Source：<code>dumpsys gfxinfo framestats</code>（逐帧，"
        "FrameCompleted − IntendedVsync）。{detail}</p>"
        "<p>⚠️ 与上方「卡顿分级」口径不同：此处按<strong>单帧</strong>耗时统计，"
        "上方按<strong>采样窗口</strong>的最长延迟分桶，两者粒度不同、不可互相换算。</p>"
    ).format(rows="\n".join(rows), detail=html_escape(detail_text))


def _api_markup(report: dict[str, Any]) -> str:
    """接口瀑布图（ticket 18）：Top N 慢接口 + 按域名分组。

    排序用 P90 而非中位：慢接口的痛点在尾部，中位会把它抹平。表格里同时给出
    请求数、中位、P90、最大、错误数与累计响应大小，避免只看一个数下结论。
    """
    api = report.get("api") or {}
    top = api.get("top") or []
    if not top:
        return ""
    rows = []
    for item in top:
        errors = item.get("errors", 0)
        errors_cell = (
            f'<span class="danger-text">{errors}</span>' if errors else str(errors)
        )
        rows.append(
            '<tr><td>{host}</td><td>{route}</td><td class="num">{count}</td>'
            '<td class="num">{median}</td><td class="num">{p90}</td>'
            '<td class="num">{max}</td><td class="num">{errors}</td>'
            '<td class="num">{payload}</td></tr>'.format(
                host=html_escape(str(item.get("host") or "")),
                route=html_escape(str(item.get("route") or "")),
                count=item.get("count", ""),
                median=_fmt(item.get("medianMs")),
                p90=_fmt(item.get("p90Ms")),
                max=_fmt(item.get("maxMs")),
                errors=errors_cell,
                payload=_fmt(item.get("payloadKb")),
            )
        )
    by_host = api.get("byHost") or {}
    host_text = "；".join(
        f"{html_escape(str(host))}：{count} 次"
        for host, count in sorted(by_host.items(), key=lambda pair: -pair[1])
    )
    return (
        "<h2>接口瀑布图 / API waterfall</h2>"
        '<table class="api-waterfall"><tr><th>域名 / Host</th><th>接口 / Route</th>'
        '<th class="num">请求数 / Count</th><th class="num">中位 / Median (ms)</th>'
        '<th class="num">P90 (ms)</th><th class="num">最大 / Max (ms)</th>'
        '<th class="num">错误 / Errors</th><th class="num">响应 / Payload (KB)</th></tr>'
        f"{''.join(rows)}</table>"
        f"<p>按域名分组 / By host：{host_text}。"
        f"共 {api.get('totalRoutes', len(top))} 个接口路由，错误 {api.get('errors', 0)} 次。"
        "排序依据 P90（尾部耗时），不是中位。</p>"
        "<p>⚠️ 与上方「网络剖面」的连接级指标口径不同：这里按 <strong>URL 路由</strong>"
        "统计（同一隧道的多个请求各算一次），连接级指标按 <strong>TLS 隧道</strong>统计，"
        "两者不可互相换算。</p>"
    )


def _network_markup(report: dict[str, Any]) -> str:
    """网络剖面小节（ticket 17）：档位参数 + 代理侧证据 + 恢复结果。

    生效判定**只看代理侧日志**——报告里明写这一点，避免读者拿"页面能打开"
    当证据。未生效时把原因与下一步摆在表里，不隐藏。
    """
    network = report.get("network") or {}
    if not network:
        return ""
    profile = network.get("profile") or {}
    proxy = network.get("proxy") or {}
    restored = network.get("restored") or {}
    decrypt = network.get("tier") == "decrypt"
    rows = [
        ("档位 / Profile", profile.get("name") or ""),
        ("模式 / Tier", "解密 / decrypt" if decrypt else "直通 / passthrough"),
        ("延迟 / Latency (ms)", _fmt(profile.get("latencyMs"))),
        ("下行 / Down (kbps)", _fmt(profile.get("downKbps"))),
        ("上行 / Up (kbps)", _fmt(profile.get("upKbps"))),
        ("丢包 / Loss (%)", _fmt(profile.get("lossPct"))),
        ("代理连接 / Connections", _fmt(proxy.get("connections"))),
        ("命中域名 / Hosts", ", ".join(proxy.get("hosts") or []) or "无"),
        ("代理侧错误 / Proxy errors", _fmt(proxy.get("errors"))),
        ("生效 / Effective", "是 / yes" if network.get("effective") else "否 / no"),
    ]
    if decrypt:
        rows.append(("信任探测 / Trust probe", _trust_text(network.get("trust"))))
        if network.get("trustError"):
            rows.append(("证书错误 / Cert error", network["trustError"]))
    if network.get("notTestedReason"):
        rows.append(("未生效原因 / Reason", network["notTestedReason"]))
    if network.get("apiNotTestedReason"):
        rows.append(("接口级不可用 / API unavailable", network["apiNotTestedReason"]))
    if network.get("hint"):
        rows.append(("下一步 / Next step", network["hint"]))
    if restored:
        rows.append(
            (
                "环境恢复 / Restored",
                "，".join(f"{key}={value}" for key, value in restored.items()),
            )
        )
    trust_kind = {"trusted": "ok", "rejected": "danger"}.get(
        str(network.get("trust") or ""), "muted"
    )
    rendered = []
    for name, value in rows:
        cell = html_escape(str(value))
        if name.startswith("生效"):
            kind = "ok" if network.get("effective") else "warn"
            cell = f'<span class="badge {kind}">{cell}</span>'
        elif name.startswith("信任探测"):
            cell = f'<span class="badge {trust_kind}">{cell}</span>'
        rendered.append(f"<tr><td>{html_escape(name)}</td><td>{cell}</td></tr>")
    body = "\n".join(rendered)
    # APP 抓包（HAR）摘要：只在 network.har 存在时追加；旧 run 无此键时
    # 输出与现版本逐字节一致。链接给相对 run 目录的 network/app.har，
    # 报告 JSON/HTML 均不内嵌 HAR 正文或任何敏感值。
    body += _har_rows_html(network.get("har"))
    tier_note = (
        "解密档 / decrypt：解 TLS 后同时产出连接级与接口级指标；接口级只在"
        "信任探测成功时可用，否则如实 not_tested（不重试、不猜）。"
        if decrypt
        else "直通档 / passthrough：不解密 TLS，只产出连接级耗时与字节数，无 URL / 状态码。"
    )
    return (
        "<h2>网络剖面 / Network profile</h2>"
        '<table class="net-profile"><tr><th>项 / Field</th><th>值 / Value</th></tr>'
        f"{body}</table>"
        f"<p>档位 / Tier：{tier_note}"
        "生效判定<strong>只看代理侧日志</strong>：被测 App 有本地"
        "缓存与多域名兜底，页面能打开<strong>不</strong>代表代理生效。</p>"
    )


_HAR_STATUS_TEXT = {
    "not_enabled": "未开启 / not enabled",
    "not_tested": "未取得可用产物 / not tested",
    "complete": "完整 / complete",
    "partial": "部分 / partial（仅明文 HTTP）",
    "failed": "失败 / failed",
}


def _har_rows_html(har: Any) -> str:
    """HAR 摘要行（仅 network.har 存在时调用）；危险/降级必须显著标注。"""
    if not isinstance(har, dict):
        return ""
    status = str(har.get("status") or "")
    artifact = (
        '<a href="network/app.har">network/app.har</a>'
        if har.get("entries") and har.get("path")
        else "无（未创建空 HAR）"
    )
    status_text = _HAR_STATUS_TEXT.get(status, status)
    status_kind = {"failed": "danger", "complete": "ok", "partial": "warn"}.get(status, "muted")
    status_cell = (
        f'<span class="badge {status_kind}">'
        + ("⚠️ " if status == "failed" else "")
        + html_escape(status_text)
        + "</span>"
    )
    redacted_cell = (
        '<span class="badge ok">是 / yes</span>'
        if har.get("redacted")
        else '<span class="badge danger">⚠️ 否 / no（危险：HAR 含敏感原值，勿外传）</span>'
    )
    rows: list[tuple[str, str]] = [
        ("HAR / HAR", status_cell),
        ("HAR 条目 / HAR entries", str(har.get("entries", 0))),
        ("HAR 产物 / HAR artifact", artifact),
        (
            "正文 / Bodies",
            (
                "完整正文 / full（按单条上限截断）"
                if har.get("bodyMode") == "full"
                else "仅元数据 / metadata（size + sha256）"
            ),
        ),
        ("脱敏 / Redacted", redacted_cell),
        (
            "截断 / Truncated",
            (
                f"是 / yes（{har.get('truncatedReason') or '限额'}）"
                if har.get("truncated")
                else "否 / no"
            ),
        ),
        (
            "HTTPS 覆盖 / HTTPS coverage",
            "完整 / full"
            if har.get("httpsCoverage") == "full"
            else "无（仅明文 HTTP）/ none",
        ),
    ]
    if har.get("notTestedReason"):
        rows.append(("HAR 不可用原因 / HAR unavailable", str(har["notTestedReason"])))
    rendered = []
    for name, value in rows:
        raw = name.startswith(("HAR 产物", "HAR / HAR", "脱敏 / Redacted"))
        cell = value if raw else html_escape(value)
        rendered.append(f"<tr><td>{html_escape(name)}</td><td>{cell}</td></tr>")
    return "".join(rendered)


def _trust_text(trust: Any) -> str:
    """信任探测三态的展示文本（与 netprof.TRUST_* 一一对应）。"""
    return {
        "trusted": "成功 / trusted（App 信任代理 CA）",
        "rejected": "证书不信任 / rejected（App 不信任代理 CA）",
        "pending": "未取得结论 / pending（会话仍在进行）",
    }.get(str(trust or ""), "未知 / unknown（未观察到客户端 TLS 握手）")


def _jank_threshold_line_script(report: dict[str, Any]) -> str:
    """最长延迟曲线上的三档阈值线（仅当该曲线存在时注入）。"""
    if "frame_delay_max_ms" not in (report.get("series") or {}):
        return ""
    thresholds = _jank_thresholds(report)
    lines = ", ".join(
        f'{{ yAxis: {_fmt(thresholds[grade])}, lineStyle: {{ type: "dashed" }}, '
        f'label: {{ formatter: "{_fmt(thresholds[grade])} ms", fontSize: 10, '
        f'position: "insideStartTop" }} }}'
        for grade in jank_module.GRADES
    )
    return (
        'if (key === "frame_delay_max_ms" && typeof option !== "undefined") {\n'
        '    option.series[0].markLine = { symbol: "none", data: [' + lines + "] };\n"
        "  }\n  "
    )


def _artifact_links_html(report: dict[str, Any], run_dir: str | Path) -> str:
    """产物清单单元格：折叠收纳，一行一个相对 report.html 所在目录的链接。"""
    artifacts = report.get("artifacts") or []
    if not artifacts:
        return "无"
    links = []
    for art in artifacts:
        # relpath 兜底：产物不在 run 目录（历史 run/外部路径）或跨盘符时，如实回退原路径
        try:
            rel = Path(os.path.relpath(art, run_dir)).as_posix()
        except ValueError:
            rel = Path(art).as_posix()
        links.append(f'<a href="{html_escape(rel)}">{html_escape(rel)}</a>')
    return (
        f'<details class="artifact-list"><summary>{len(links)} 个产物文件</summary>'
        f'<div class="artifact-links">{"<br>".join(links)}</div></details>'
    )


def _report_outputs_html(run_dir: str | Path) -> str:
    """报告自身产物行：报告双产物 / meta.json / 网络日志，相对链接一行一个（存在才列）。"""
    run = Path(run_dir)
    names = ["report.html", "report.json", "meta.json"]
    if (run / "network").is_dir():
        names.extend(
            p.relative_to(run).as_posix()
            for p in sorted((run / "network").iterdir())
            if p.is_file()
        )
    links = [
        f'<a href="{html_escape(rel)}">{html_escape(rel)}</a>'
        for rel in names
        if (run / rel).exists()
    ]
    return "<br>".join(links) if links else "无"


def _meta_rows(report: dict[str, Any], run_dir: str | Path) -> list[tuple[str, str]]:
    integrity = report.get("integrity") or {}
    marks = report.get("marks") or []
    startup = report.get("startup") or {}
    if startup.get("ms") is not None:
        startup_text = f"{startup.get('mode')} {_fmt(startup['ms'])} ms"
    elif startup.get("error"):
        startup_text = f"error: {startup['error']}"
    else:
        startup_text = ""
    rows = [
        ("被测应用 / Target", report.get("target")),
        ("Run ID", report.get("runId")),
        ("宿主运行 / Host Run", report.get("hostRunId")),
        ("设备 serial", report.get("serial")),
        ("设备档案 / Device Profile", report.get("deviceProfile") or "未记录"),
        ("采集项 / Items", ", ".join(report.get("items") or [])),
        ("开始 / Started", report.get("startedAt")),
        ("结束 / Stopped", report.get("stoppedAt")),
        ("时长 / Duration (s)", report.get("durationS")),
        ("SoloPi 会话", report.get("soloSessionId")),
        ("打标 / Marks", json.dumps(marks, ensure_ascii=False) if marks else "无"),
        ("启动 / Startup", startup_text or "未编排"),
        ("产物 / Artifacts", _artifact_links_html(report, run_dir)),
        ("报告产物 / Report outputs", _report_outputs_html(run_dir)),
        ("完整性 / Integrity", json.dumps(integrity, ensure_ascii=False) if integrity else "无"),
    ]
    return [(name, str(value if value is not None else "")) for name, value in rows]


def _bilingual(label_zh: str | None, label_en: str | None, fallback: str) -> str:
    if label_zh and label_en:
        return f"{label_zh} / {label_en}"
    return label_zh or label_en or fallback


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)
