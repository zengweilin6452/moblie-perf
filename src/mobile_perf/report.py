"""报告聚合：每个 run 产出同内容 JSON 与单文件 HTML（内嵌离线 ECharts，无 CDN 依赖）。

JSON 与 HTML 出自同一份报告字典（同一聚合数据源）；事件时间轴的渲染归 07 号
工单，本模块只保留 events 数据注入点。
"""

from __future__ import annotations

import csv
import json
from html import escape as html_escape
from pathlib import Path
from typing import Any

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

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>mobile-perf 报告 {run_id}</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 24px; color: #1f2430; }}
h1 {{ font-size: 20px; }} h2 {{ font-size: 16px; margin-top: 28px; }}
table {{ border-collapse: collapse; margin-top: 8px; }}
td, th {{ border: 1px solid #d5d9e0; padding: 4px 10px; font-size: 13px; text-align: left; }}
th {{ background: #f2f4f8; }}
.chart {{ width: 100%; height: 320px; margin-top: 12px; }}
.meta td:first-child {{ background: #f2f4f8; font-weight: 600; }}
</style>
</head>
<body>
<h1>mobile-perf 性能报告：{run_id}</h1>
<h2>会话元信息</h2>
<table class="meta">{meta_rows}</table>
{startup_markup}
{page_markup}
{jank_markup}
{frame_markup}
{gpu_markup}
{network_markup}
{api_markup}
<h2>聚合统计</h2>
<table>
<tr><th>指标</th><th>单位</th><th>来源</th><th>min</th><th>median</th><th>mean</th>
<th>max</th><th>p90</th><th>样本数</th></tr>
{summary_rows}
</table>
<h2>指标曲线</h2>
{chart_divs}
<noscript>曲线图需要 JavaScript（内嵌 ECharts）渲染；聚合统计见上表。</noscript>
<!-- vendor:echarts BEGIN -->
<script>
{echarts_js}
</script>
<!-- vendor:echarts END -->
<script id="report-data">window.__REPORT__ = {report_json};</script>
<script>
(function () {{
  var report = window.__REPORT__;
  var series = report.series || {{}};
  var startup = report.startup || null;
  Object.keys(series).forEach(function (key) {{
    var item = series[key];
    var el = document.getElementById("chart-" + key);
    if (!el || typeof echarts === "undefined") return;
    var chart = echarts.init(el);
    var option = {{
      title: {{ text: item.labelZh + " / " + item.labelEn + " (" + item.unit + ")",
               textStyle: {{ fontSize: 13 }} }},
      tooltip: {{ trigger: "axis" }},
      xAxis: {{ type: "value", name: "s" }},
      yAxis: {{ type: "value", name: item.unit }},
      series: [{{
        type: "line", showSymbol: false,
        data: item.points.map(function (p) {{ return [p[0], p[1]]; }})
      }}],
      grid: {{ left: 60, right: 24, top: 40, bottom: 40 }}
    }};
__STARTUP_MARKLINE__
__PAGE_MARKLINE__
__JANK_THRESHOLD_LINE__
    chart.setOption(option);
  }});
}})();
</script>
{combined_script}
</body>
</html>
"""


def write_html(run_dir: str | Path, report: dict[str, Any]) -> Path:
    """生成单文件 HTML 报告（内嵌离线 ECharts 与同源报告数据，无外部请求）。"""
    echarts_js = _ECHARTS_PATH.read_text(encoding="utf-8")
    # 避免 vendor 代码里可能的 </script> 提前闭合标签
    echarts_js = echarts_js.replace("</script", "<\\/script").replace("<!--", "<\\!--")
    report_json = json.dumps(report, ensure_ascii=False).replace("</", "<\\/")

    summary_rows = []
    for key, entry in (report.get("metricsSummary") or {}).items():
        summary_rows.append(
            "<tr><td>{label}</td><td>{unit}</td><td>{source}</td><td>{min}</td><td>{median}</td>"
            "<td>{mean}</td><td>{max}</td><td>{p90}</td><td>{count}</td></tr>".format(
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

    meta_rows = "".join(
        f"<tr><td>{html_escape(name)}</td><td>{html_escape(value)}</td></tr>"
        for name, value in _meta_rows(report)
    )
    chart_divs = "".join(
        f'<div class="chart" id="chart-{html_escape(key)}"></div>'
        for key in sorted(report.get("series") or {})
    )
    combined_div, combined_script = _combined_thermal_markup(report.get("series") or {})

    html_text = _HTML_TEMPLATE.format(
        run_id=html_escape(report["runId"]),
        meta_rows=meta_rows,
        startup_markup=_startup_markup(report),
        page_markup=_page_markup(report),
        jank_markup=_jank_markup(report),
        frame_markup=_frame_markup(report),
        gpu_markup=_gpu_markup(report),
        network_markup=_network_markup(report),
        api_markup=_api_markup(report),
        summary_rows="\n".join(summary_rows),
        chart_divs=combined_div + chart_divs,
        echarts_js=echarts_js,
        report_json=report_json,
        combined_script=combined_script,
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


_COMBINED_CHART_ID = "chart-combined-thermal-fps"

# 温度-帧率同图：帧率走左轴，补采曲线按**单位**分组各占一条右轴，共用统一
# 时间轴（§6.3）。单位必须分组——把 °C、%、MB 挤在同一条轴上会让人误读量级。
_COMBINED_SCRIPT = """<script>
(function () {
  var report = window.__REPORT__ || {};
  var series = report.series || {};
  var startup = report.startup || null;
  var fps = series["fps_frame"];
  var supplement = [series["temp_battery_c"], series["gpu_util_pct"],
                    series["gpu_mem_mb"]].filter(Boolean);
  if (!fps || supplement.length === 0 || typeof echarts === "undefined") return;
  var el = document.getElementById("__CHART_ID__");
  if (!el) return;
  var chart = echarts.init(el);
  var lines = [{
    name: fps.labelZh + " (" + fps.unit + ")", type: "line", showSymbol: false, yAxisIndex: 0,
    data: fps.points.map(function (p) { return [p[0], p[1]]; })
  }];
__STARTUP_MARKLINE__
__PAGE_MARKLINE__
  var axes = [{ type: "value", name: fps.unit }];
  var axisByUnit = {};
  axisByUnit[fps.unit] = 0;
  supplement.forEach(function (item) {
    if (axisByUnit[item.unit] === undefined) {
      axisByUnit[item.unit] = axes.length;
      axes.push({ type: "value", name: item.unit });
    }
    lines.push({
      name: item.labelZh + " (" + item.unit + ")", type: "line", showSymbol: false,
      yAxisIndex: axisByUnit[item.unit],
      data: item.points.map(function (p) { return [p[0], p[1]]; })
    });
  });
  chart.setOption({
    title: { text: "温度-帧率-显存同图（统一时间轴）", textStyle: { fontSize: 13 } },
    tooltip: { trigger: "axis" },
    legend: { top: 22 },
    xAxis: { type: "value", name: "s" },
    yAxis: axes,
    series: lines,
    grid: { left: 60, right: 60, top: 60, bottom: 40 }
  });
})();
</script>"""


def _combined_thermal_markup(series: dict[str, Any]) -> tuple[str, str]:
    """有帧率且有补采曲线时返回（同图 div，同图脚本）；否则返回空串。"""
    has_fps = "fps_frame" in series
    has_supplement = any(
        key in series for key in ("temp_battery_c", "gpu_util_pct", "gpu_mem_mb")
    )
    if not (has_fps and has_supplement):
        return "", ""
    div = f'<h2>温度-帧率同图</h2><div class="chart" id="{_COMBINED_CHART_ID}"></div>'
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
        rows.append(
            "<tr><td>{page}</td><td>{value}</td><td>{status}</td><td>{frames}</td>"
            "<td>{reason}</td></tr>".format(
                page=html_escape(str(item.get("page") or "")),
                value=_fmt(value) if value is not None else "not_tested",
                status=html_escape(_PAGE_STATUS_TEXT.get(status, status)),
                frames=item.get("frameCount", ""),
                reason=html_escape(str(item.get("reason") or "")),
            )
        )
    offset_source = str(pages.get("clockOffsetSource") or "")
    offset_text = {
        "uptime_bracket": "括号式 uptime 实测换算（读一次 /proc/uptime，往返取中点）",
        "no_uptime_offset": "未取到设备 uptime，帧时刻无法换算，本 run 不硬算",
        "no_frame_poll": "未开启逐帧采集，没有帧完成时刻",
    }.get(offset_source, offset_source or "未知")
    if pages.get("clockRttMs") is not None:
        offset_text += f"（往返 {pages['clockRttMs']} ms，对齐误差为往返量级）"
    parts = [
        "<h2>整页加载耗时 / Page load</h2>",
        '<table class="page-load"><tr><th>页面 / Page</th><th>整页耗时 / Load (ms)</th>'
        "<th>判定 / Status</th><th>帧数 / Frames</th><th>说明 / Note</th></tr>"
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
      label: { formatter: function (p) { return p.name; }, fontSize: 10 },
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
        "<tr><td>{label}</td><td>{median}</td><td>{p90}</td><td>{max}</td>"
        "<td>{count}</td><td>{unit}</td></tr>".format(
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
        '<table class="gpu-stats"><tr><th>指标 / Metric</th><th>中位 / Median</th>'
        "<th>P90</th><th>最大 / Max</th><th>样本 / Samples</th><th>单位 / Unit</th></tr>"
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
      label: { formatter: "启动 " + startup.ms + " ms", fontSize: 10 },
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
            f'<h2>启动时间 / Startup</h2><p>{header}：启动失败——'
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
        f'<h2>启动时间 / Startup</h2><p>{header}：<b>{_fmt(ms)} ms</b>'
        + (f"（{detail}）" if detail else "")
        + "</p>"
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
            "<tr><td>{label}</td><td>{count}</td><td>{samples}</td></tr>".format(
                label=html_escape(label),
                count=_fmt(entry.get("max")),
                samples=entry.get("sampleCount", ""),
            )
        )
    return (
        "<h2>卡顿分级 / Jank grading</h2>"
        '<table class="jank-threshold"><tr><th>分级 / Grade</th><th>窗口数 / Windows</th>'
        "<th>样本数 / Samples</th></tr>{rows}</table>"
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
        ("app_frame_count", "采样帧数 / Sampled frames"),
    )
    rows = [
        "<tr><td>{label}</td><td>{value}</td><td>{unit}</td></tr>".format(
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
    detail_text = ("；".join(detail) + "。") if detail else ""
    return (
        "<h2>逐帧卡顿 / Per-frame jank</h2>"
        '<table class="frame-stats"><tr><th>指标 / Metric</th><th>值 / Value</th>'
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
    rows = [
        "<tr><td>{host}</td><td>{route}</td><td>{count}</td><td>{median}</td>"
        "<td>{p90}</td><td>{max}</td><td>{errors}</td><td>{payload}</td></tr>".format(
            host=html_escape(str(item.get("host") or "")),
            route=html_escape(str(item.get("route") or "")),
            count=item.get("count", ""),
            median=_fmt(item.get("medianMs")),
            p90=_fmt(item.get("p90Ms")),
            max=_fmt(item.get("maxMs")),
            errors=item.get("errors", 0),
            payload=_fmt(item.get("payloadKb")),
        )
        for item in top
    ]
    by_host = api.get("byHost") or {}
    host_text = "；".join(
        f"{html_escape(str(host))}：{count} 次"
        for host, count in sorted(by_host.items(), key=lambda pair: -pair[1])
    )
    return (
        "<h2>接口瀑布图 / API waterfall</h2>"
        '<table class="api-waterfall"><tr><th>域名 / Host</th><th>接口 / Route</th>'
        "<th>请求数 / Count</th><th>中位 / Median (ms)</th><th>P90 (ms)</th>"
        "<th>最大 / Max (ms)</th><th>错误 / Errors</th><th>响应 / Payload (KB)</th></tr>"
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
    body = "\n".join(
        f"<tr><td>{html_escape(name)}</td><td>{html_escape(str(value))}</td></tr>"
        for name, value in rows
    )
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
        "生效判定<strong>只看代理侧日志</strong>——被测 App 有本地"
        "缓存与多域名兜底，页面能打开<strong>不</strong>代表代理生效。</p>"
    )


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
        f'label: {{ formatter: "{_fmt(thresholds[grade])} ms", fontSize: 10 }} }}'
        for grade in jank_module.GRADES
    )
    return (
        'if (key === "frame_delay_max_ms" && typeof option !== "undefined") {\n'
        '    option.series[0].markLine = { symbol: "none", data: [' + lines + "] };\n"
        "  }\n  "
    )


def _meta_rows(report: dict[str, Any]) -> list[tuple[str, str]]:
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
        ("产物 / Artifacts", "\n".join(report.get("artifacts") or []) or "无"),
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
