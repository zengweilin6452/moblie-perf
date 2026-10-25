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
) -> dict[str, Any]:
    """把采集元数据、会话记录与产物清单聚合为报告字典（JSON 与 HTML 共用）。

    metrics_summary 的每个指标补充双语标签（来自指标字典）；series 为
    {canonical key: 曲线数据}；events 只保留数据，渲染由后续工单接入。
    """
    return {
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
        summary_rows="\n".join(summary_rows),
        chart_divs=combined_div + chart_divs,
        echarts_js=echarts_js,
        report_json=report_json,
        combined_script=combined_script,
    )
    # 启动点标记按需注入（占位符在 format 之后替换，避免花括号转义）
    html_text = html_text.replace("__STARTUP_MARKLINE__", _startup_markline_script(report))
    path = Path(run_dir) / "report.html"
    path.write_text(html_text, encoding="utf-8")
    return path


_COMBINED_CHART_ID = "chart-combined-thermal-fps"

# 温度-帧率同图：帧率走左轴，补采温度/GPU 走右轴，共用统一时间轴（§6.3）
_COMBINED_SCRIPT = """<script>
(function () {
  var report = window.__REPORT__ || {};
  var series = report.series || {};
  var startup = report.startup || null;
  var fps = series["fps_frame"];
  var supplement = [series["temp_battery_c"], series["gpu_util_pct"]].filter(Boolean);
  if (!fps || supplement.length === 0 || typeof echarts === "undefined") return;
  var el = document.getElementById("__CHART_ID__");
  if (!el) return;
  var chart = echarts.init(el);
  var lines = [{
    name: fps.labelZh + " (" + fps.unit + ")", type: "line", showSymbol: false, yAxisIndex: 0,
    data: fps.points.map(function (p) { return [p[0], p[1]]; })
  }];
__STARTUP_MARKLINE__
  supplement.forEach(function (item) {
    lines.push({
      name: item.labelZh + " (" + item.unit + ")", type: "line", showSymbol: false, yAxisIndex: 1,
      data: item.points.map(function (p) { return [p[0], p[1]]; })
    });
  });
  chart.setOption({
    title: { text: "温度-帧率同图（统一时间轴）", textStyle: { fontSize: 13 } },
    tooltip: { trigger: "axis" },
    legend: { top: 22 },
    xAxis: { type: "value", name: "s" },
    yAxis: [
      { type: "value", name: fps.unit },
      { type: "value", name: supplement[0].unit }
    ],
    series: lines,
    grid: { left: 60, right: 60, top: 60, bottom: 40 }
  });
})();
</script>"""


def _combined_thermal_markup(series: dict[str, Any]) -> tuple[str, str]:
    """有帧率且有补采曲线时返回（同图 div，同图脚本）；否则返回空串。"""
    has_fps = "fps_frame" in series
    has_supplement = "temp_battery_c" in series or "gpu_util_pct" in series
    if not (has_fps and has_supplement):
        return "", ""
    div = f'<h2>温度-帧率同图</h2><div class="chart" id="{_COMBINED_CHART_ID}"></div>'
    return div, _COMBINED_SCRIPT.replace("__CHART_ID__", _COMBINED_CHART_ID)


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
