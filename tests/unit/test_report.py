"""report 骨架的单元测试：报告聚合与落盘（M3 补齐图表数据）。"""

import json
from html import escape as html_escape

import pytest

from mobile_perf import report


def _write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(rows) + "\n", encoding="gb18030")


def test_series_from_artifacts_maps_columns_to_keyed_curves(tmp_path):
    _write_csv(
        tmp_path / "artifacts" / "performance.csv",
        [
            "RecordTime,PSS-main-18390(MB),全局占用(%),SimpleTime",
            "1792895595000,10.0,5.0,0",
            "1792895596000,20.0,6.0,1",
            "1792895598000,30.0,7.0,3",
        ],
    )

    series = report.series_from_artifacts(tmp_path / "artifacts")

    pss = series["mem_pss_mb"]
    assert pss["unit"] == "MB"
    assert pss["labelZh"] == "应用PSS内存"
    # x 为相对首个样本的秒数，RecordTime 毫秒转秒
    assert pss["points"] == [[0.0, 10.0], [1.0, 20.0], [3.0, 30.0]]
    assert series["cpu_global_pct"]["points"] == [[0.0, 5.0], [1.0, 6.0], [3.0, 7.0]]


def test_series_from_artifacts_merges_files_and_skips_unmapped(tmp_path):
    _write_csv(
        tmp_path / "artifacts" / "a_memory.csv",
        ["RecordTime,PSS-main-1(MB),SimpleTime", "1000,10.0,0"],
    )
    _write_csv(
        tmp_path / "artifacts" / "b_memory.csv",
        ["RecordTime,PSS-main-2(MB),extra,SimpleTime", "2500,20.0,,1.5"],
    )
    _write_csv(tmp_path / "artifacts" / "note.txt", ["not a csv"])

    series = report.series_from_artifacts(tmp_path / "artifacts")

    assert series["mem_pss_mb"]["points"] == [[0.0, 10.0], [1.5, 20.0]]


def test_series_from_artifacts_without_recordtime_falls_back_to_row_index(tmp_path):
    _write_csv(
        tmp_path / "artifacts" / "performance.csv",
        ["PSS-main-18390(MB),SimpleTime", "10.0,abc", "20.0,def"],
    )

    series = report.series_from_artifacts(tmp_path / "artifacts")

    assert series["mem_pss_mb"]["points"] == [[0.0, 10.0], [1.0, 20.0]]


def test_series_from_artifacts_empty_dir_returns_empty(tmp_path):
    assert report.series_from_artifacts(tmp_path / "missing") == {}


def _meta():
    return {
        "runId": "run-x",
        "target": "tv.danmaku.bili",
        "items": ["CPU", "Memory"],
        "serial": "749622c9",
        "hostRunId": None,
        "startedAt": "2026-10-25T10:00:00",
        "stoppedAt": "2026-10-25T10:00:30",
        "durationS": 30.0,
    }


def _record():
    return {
        "sessionId": "sid-1",
        "marks": [{"label": "half_way", "elapsedS": 15.0}],
    }


def test_build_report_contains_session_and_artifacts():
    built = report.build_report(meta=_meta(), record=_record(), artifacts=["a/performance.csv"])

    assert built["schemaVersion"] == 1
    assert built["runId"] == "run-x"
    assert built["soloSessionId"] == "sid-1"
    assert built["artifacts"] == ["a/performance.csv"]
    assert built["marks"] == [{"label": "half_way", "elapsedS": 15.0}]
    # M3 填实：SoloPi/补采/事件三源同时间轴
    assert built["metricsSummary"] is None


def test_build_report_enriches_metric_labels_from_dictionary():
    meta = _meta() | {"deviceProfile": "PICO4-12"}
    metrics_summary = {
        "mem_pss_mb": {"unit": "MB", "source": "solo_pi", "median": 42.0, "sampleCount": 3}
    }

    built = report.build_report(
        meta=meta,
        record=_record(),
        artifacts=["a/performance.csv"],
        metrics_summary=metrics_summary,
        series={"mem_pss_mb": {"points": [[0.0, 42.0], [1.0, 43.0]]}},
        events=[{"kind": "mark", "detail": {}}],
    )

    assert built["deviceProfile"] == "PICO4-12"
    entry = built["metricsSummary"]["mem_pss_mb"]
    assert entry["labelZh"] == "应用PSS内存"
    assert entry["labelEn"] == "Process PSS memory"
    # 字典外的指标保留原样，不强行造标签
    assert built["metricsSummary"]["mem_pss_mb"]["median"] == 42.0
    assert built["series"]["mem_pss_mb"]["points"] == [[0.0, 42.0], [1.0, 43.0]]
    # 07 号工单的事件时间轴接入点：数据原样保留，渲染不在本票
    assert built["events"] == [{"kind": "mark", "detail": {}}]


def test_build_report_defaults_series_and_events_to_empty():
    built = report.build_report(meta=_meta(), record=_record(), artifacts=[])

    assert built["series"] == {}
    assert built["events"] == []


def test_write_html_embeds_same_source_data_and_chart_containers(tmp_path):
    built = report.build_report(
        meta=_meta(),
        record=_record(),
        artifacts=["a/performance.csv"],
        metrics_summary={"mem_pss_mb": {"unit": "MB", "median": 42.0, "sampleCount": 3}},
        series={"mem_pss_mb": {"unit": "MB", "labelZh": "应用PSS内存", "points": [[0.0, 42.0]]}},
    )
    report.write_report(tmp_path, built)

    path = report.write_html(tmp_path, built)

    assert path == tmp_path / "report.html"
    html = path.read_text(encoding="utf-8")
    # vendor 离线包整体内嵌（无 CDN）
    assert "vendor:echarts" in html and "echarts" in html
    # 图表容器：每个 series key 一个
    assert 'id="chart-mem_pss_mb"' in html
    # 双语标签出现在静态汇总表
    assert "应用PSS内存" in html and "Process PSS memory" in html
    # 嵌入的 JSON 与 report.json 完全同源
    embedded = json.loads(_extract_embedded_json(html))
    on_disk = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert embedded == on_disk == built
    # 图表脚本必须是立即执行的表达式（曾漏掉调用括号导致图表空白）
    assert html.count("(function () {") == html.count("})();")


def test_write_html_has_no_external_resource_references(tmp_path):
    built = report.build_report(meta=_meta(), record=_record(), artifacts=[], series={})

    path = report.write_html(tmp_path, built)

    html = path.read_text(encoding="utf-8")
    outside_vendor = _strip_vendor(html)
    for forbidden in ("http://", "https://", "<script src", "<link ", "url("):
        assert forbidden not in outside_vendor, forbidden


def _extract_embedded_json(html: str) -> str:
    marker = 'id="report-data">'
    start = html.index(marker) + len(marker)
    begin = html.index("window.__REPORT__ =", start) + len("window.__REPORT__ =")
    end = html.index(";</script>", begin)
    return html[begin:end].replace("<\\/", "</")


def _strip_vendor(html: str) -> str:
    begin_marker = "<!-- vendor:echarts BEGIN -->"
    end_marker = "<!-- vendor:echarts END -->"
    begin = html.index(begin_marker)
    end = html.index(end_marker) + len(end_marker)
    return html[:begin] + html[end:]


def test_assemble_run_report_from_run_dir_and_store(tmp_path):
    from mobile_perf.store import PerfStore

    run_dir = tmp_path / "runs" / "run-x"
    _write_csv(
        run_dir / "artifacts" / "performance.csv",
        ["RecordTime,PSS-main-18390(MB),SimpleTime", "1000,10.0,0", "2000,20.0,1"],
    )
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "runId": "run-x",
                "target": "tv.danmaku.bili",
                "items": ["Memory"],
                "serial": "749622c9",
                "startedAt": "2026-10-25T10:00:00",
                "stoppedAt": "2026-10-25T10:00:30",
                "durationS": 30.0,
                "marks": [],
                "integrity": {"deviceRowCheck": "skipped"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = PerfStore(tmp_path / "data" / "mobile_perf.db")
    store.record_session(
        {
            "run_id": "run-x",
            "target": "tv.danmaku.bili",
            "device_profile": "PICO4-Android12",
            "run_dir": str(run_dir),
        }
    )
    store.record_metrics(
        "run-x",
        [{"metric_key": "mem_pss_mb", "unit": "MB", "median": 15.0, "sample_count": 2}],
    )
    store.record_event("run-x", kind="integrity_warning", detail={"reason": "x"})

    built = report.assemble_run_report("run-x", runs_dir=tmp_path / "runs", store=store)

    assert built["runId"] == "run-x"
    assert built["deviceProfile"] == "PICO4-Android12"
    assert built["metricsSummary"]["mem_pss_mb"]["median"] == 15.0
    assert built["metricsSummary"]["mem_pss_mb"]["labelZh"] == "应用PSS内存"
    assert built["series"]["mem_pss_mb"]["points"] == [[0.0, 10.0], [1.0, 20.0]]
    assert [e["kind"] for e in built["events"]] == ["integrity_warning"]
    assert built["artifacts"] == [str(run_dir / "artifacts" / "performance.csv")]


def test_assemble_run_report_without_run_dir_falls_back_to_store(tmp_path):
    from mobile_perf.store import PerfStore

    store = PerfStore(tmp_path / "data" / "mobile_perf.db")
    store.record_session(
        {
            "run_id": "run-lost",
            "target": "tv.danmaku.bili",
            "items": ["CPU"],
            "serial": "749622c9",
            "started_at": "2026-10-25T10:00:00",
            "duration_s": 30.0,
        }
    )

    built = report.assemble_run_report("run-lost", runs_dir=tmp_path / "runs", store=store)

    assert built["target"] == "tv.danmaku.bili"
    assert built["series"] == {}
    assert built["artifacts"] == []


def test_assemble_run_report_unknown_run_raises(tmp_path):
    from mobile_perf.store import PerfStore

    store = PerfStore(tmp_path / "data" / "mobile_perf.db")
    with pytest.raises(report.RunNotFoundError):
        report.assemble_run_report("run-none", runs_dir=tmp_path / "runs", store=store)


def test_write_report_round_trip(tmp_path):
    built = report.build_report(meta=_meta(), record=_record(), artifacts=[])

    path = report.write_report(tmp_path, built)

    assert path == tmp_path / "report.json"
    assert json.loads(path.read_text(encoding="utf-8"))["runId"] == "run-x"


def test_series_entries_carry_metric_source(tmp_path):
    _write_csv(
        tmp_path / "artifacts" / "performance.csv",
        ["RecordTime,PSS-main-18390(MB),SimpleTime", "1000,10.0,0"],
    )

    series = report.series_from_artifacts(tmp_path / "artifacts")

    assert series["mem_pss_mb"]["source"] == "solo_pi"


def test_series_from_artifacts_absorbs_supplement_csv_on_elapsed_axis(tmp_path):
    _write_csv(
        tmp_path / "artifacts" / "supplement_thermal_gpu.csv",
        [
            "elapsed_s,temp_battery_c,gpu_util_pct",
            "0.0,28.0,",
            "2.0,28.1,40.0",
        ],
    )

    series = report.series_from_artifacts(tmp_path / "artifacts")

    assert series["temp_battery_c"]["source"] == "pc_supplement"
    assert series["temp_battery_c"]["unit"] == "°C"
    assert series["temp_battery_c"]["points"] == [[0.0, 28.0], [2.0, 28.1]]
    assert series["gpu_util_pct"]["points"] == [[2.0, 40.0]]


def _series_for_combined(*keys):
    pool = {
        "fps_frame": {
            "unit": "fps", "source": "solo_pi", "labelZh": "帧率", "points": [[0.0, 60.0]],
        },
        "temp_battery_c": {
            "unit": "°C", "source": "pc_supplement", "labelZh": "电池温度", "points": [[0.0, 28.0]],
        },
        "gpu_util_pct": {
            "unit": "%", "source": "pc_supplement", "labelZh": "GPU占用", "points": [[0.0, 30.0]],
        },
    }
    return {key: pool[key] for key in keys}


def test_write_html_renders_thermal_fps_combined_chart(tmp_path):
    built = report.build_report(
        meta=_meta(),
        record=_record(),
        artifacts=[],
        series=_series_for_combined("fps_frame", "temp_battery_c", "gpu_util_pct"),
    )

    path = report.write_html(tmp_path, built)
    html = path.read_text(encoding="utf-8")

    assert 'id="chart-combined-thermal-fps"' in html
    assert "温度-帧率同图" in html
    # 同图脚本同时引用帧率与补采曲线
    assert "fps_frame" in html and "temp_battery_c" in html and "gpu_util_pct" in html


def test_write_html_without_supplement_series_has_no_combined_chart(tmp_path):
    built = report.build_report(
        meta=_meta(),
        record=_record(),
        artifacts=[],
        series=_series_for_combined("fps_frame"),
    )

    path = report.write_html(tmp_path, built)
    html = path.read_text(encoding="utf-8")

    assert 'id="chart-combined-thermal-fps"' not in html


# ---- ticket 12：启动时间与启动期曲线同图/同轴呈现 ----

STARTUP_META = {
    "mode": "cold",
    "metricKey": "app_cold_start_ms",
    "ms": 487.0,
    "launchState": "COLD",
    "waitTimeMs": 502.0,
    "displayedMs": 501.0,
    "deviationMs": 14.0,
    "fullyDrawnMs": None,
    "component": "com.sinosoft.trainapp/.MainActivity",
    "status": "ok",
    "error": None,
    "elapsedS": 1.0,
}


def test_build_report_carries_startup_from_meta():
    built = report.build_report(meta=_meta(), record=_record(), artifacts=[])

    assert built["startup"] is None

    built = report.build_report(
        meta={**_meta(), "startup": STARTUP_META}, record=_record(), artifacts=[]
    )

    assert built["startup"] == STARTUP_META


def test_write_html_renders_startup_section_and_markline(tmp_path):
    built = report.build_report(
        meta={**_meta(), "startup": STARTUP_META},
        record=_record(),
        artifacts=[],
        metrics_summary={
            "app_cold_start_ms": {"unit": "ms", "median": 487.0, "sampleCount": 1}
        },
        series={
            "mem_pss_mb": {"unit": "MB", "labelZh": "应用PSS内存", "points": [[0.0, 42.0]]}
        },
    )

    html = report.write_html(tmp_path, built).read_text(encoding="utf-8")

    assert "启动时间 / Startup" in html
    assert "冷启动耗时" in html and "app_cold_start_ms" in html
    assert "487" in html
    assert "COLD" in html
    assert "501" in html and "14" in html  # 双路对照与偏差
    # 曲线上的启动点标记（同轴）
    assert "markLine" in html and "1" in html
    assert html.count("(function () {") == html.count("})();")


def test_write_html_without_startup_has_no_markline(tmp_path):
    built = report.build_report(
        meta=_meta(),
        record=_record(),
        artifacts=[],
        series={
            "mem_pss_mb": {"unit": "MB", "labelZh": "应用PSS内存", "points": [[0.0, 42.0]]}
        },
    )

    html = report.write_html(tmp_path, built).read_text(encoding="utf-8")

    # vendor 压缩包本身含 markLine 字样，只检查自产脚本段
    assert "markLine" not in _strip_vendor(html)


def test_write_html_renders_startup_error_when_no_ms(tmp_path):
    built = report.build_report(
        meta={
            **_meta(),
            "startup": {
                **STARTUP_META,
                "ms": None,
                "status": None,
                "displayedMs": None,
                "deviationMs": None,
                "error": "am start 未成功启动: Activity class com.foo/.Missing does not exist.",
            },
        },
        record=_record(),
        artifacts=[],
        series={},
    )

    html = report.write_html(tmp_path, built).read_text(encoding="utf-8")

    assert "启动时间 / Startup" in html
    assert "does not exist" in html


# ---------- 逐帧小节（ticket 15） ----------


def _frame_summary():
    return {
        "app_frame_p50_ms": {"unit": "ms", "source": "pc_supplement", "median": 8.0},
        "app_frame_p90_ms": {"unit": "ms", "source": "pc_supplement", "median": 12.0},
        "app_frame_p99_ms": {"unit": "ms", "source": "pc_supplement", "median": 48.0},
        "app_frame_jank_ratio_pct": {
            "unit": "%",
            "source": "pc_supplement",
            "median": 6.44,
        },
        "app_frame_count": {"unit": "帧", "source": "pc_supplement", "median": 3150.0},
    }


def test_build_report_records_frames_block_with_granularity():
    built = report.build_report(
        meta=_meta(),
        record=_record(),
        artifacts=[],
        metrics_summary=_frame_summary(),
        frames={"enabled": True, "frameCount": 3150, "refreshHz": 60.0, "jankMultiplier": 1.5},
    )

    assert built["frames"]["granularity"] == "per_frame"
    assert built["frames"]["source"] == "gfxinfo_framestats"
    assert built["frames"]["refreshHz"] == 60.0


def test_build_report_without_frames_omits_block():
    built = report.build_report(meta=_meta(), record=_record(), artifacts=[])

    assert "frames" not in built


def test_write_html_renders_frame_section_with_scope_warning(tmp_path):
    built = report.build_report(
        meta=_meta(),
        record=_record(),
        artifacts=[],
        metrics_summary=_frame_summary(),
        frames={"enabled": True, "frameCount": 3150, "refreshHz": 60.0, "jankMultiplier": 1.5},
    )

    html = report.write_html(tmp_path, built).read_text(encoding="utf-8")

    assert "逐帧卡顿" in html
    assert "Per-frame jank" in html
    # 必须显式标注两种口径不同、不可互算
    assert "不可互相换算" in html
    assert "dumpsys gfxinfo framestats" in html


def test_write_html_omits_frame_section_without_frames(tmp_path):
    built = report.build_report(meta=_meta(), record=_record(), artifacts=[])

    html = report.write_html(tmp_path, built).read_text(encoding="utf-8")

    assert "逐帧卡顿" not in html


# ---------- 网络剖面（ticket 17）：档位与生效证据 ----------


def _network_meta(**overrides):
    meta = {
        "profile": {"name": "weak", "latencyMs": 400.0, "downKbps": 400.0,
                    "upKbps": 200.0, "lossPct": 2.0},
        "tier": "passthrough",
        "applied": True,
        "effective": True,
        "evidence": "observed",
        "notTestedReason": None,
        "hint": None,
        "proxy": {"connections": 12, "connectRequests": 12, "httpRequests": 0,
                  "hosts": ["api.test"], "errors": 0, "bytesDown": 100, "bytesUp": 50,
                  "params": None},
        "setupErrors": [],
        "restored": {"deviceProxy": True, "reverse": True, "proxyProcess": True},
        "logPath": "/tmp/net_proxy.jsonl",
    }
    meta.update(overrides)
    return meta


def test_build_report_network_block_records_tier_and_source():
    built = report.build_report(
        meta=_meta(), record=_record(), artifacts=[], network=_network_meta()
    )

    assert built["network"]["tier"] == "passthrough"
    assert built["network"]["source"] == "mitmproxy"
    assert built["network"]["profile"]["name"] == "weak"


def test_build_report_without_network_omits_block():
    built = report.build_report(meta=_meta(), record=_record(), artifacts=[])

    assert "network" not in built


def test_write_html_renders_network_section_with_evidence_rule(tmp_path):
    built = report.build_report(
        meta=_meta(), record=_record(), artifacts=[], network=_network_meta()
    )

    html = report.write_html(tmp_path, built).read_text(encoding="utf-8")

    assert "网络剖面" in html
    assert "Network profile" in html
    assert "weak" in html
    assert "只看代理侧日志" in html  # 生效判据必须写明，防止拿页面表现当证据


def test_write_html_network_section_shows_not_tested_reason(tmp_path):
    built = report.build_report(
        meta=_meta(),
        record=_record(),
        artifacts=[],
        network=_network_meta(
            applied=False,
            effective=False,
            evidence="unavailable",
            notTestedReason="mitmproxy 不可用（未安装，或解释器版本低于其要求的 Python 3.12）",
            hint="把 MOBILE_PERF_MITMDUMP 指向该 venv 的 mitmdump",
            restored={},
        ),
    )

    html = report.write_html(tmp_path, built).read_text(encoding="utf-8")

    assert "未生效原因" in html
    assert "mitmproxy 不可用" in html
    assert "MOBILE_PERF_MITMDUMP" in html


def test_write_html_omits_network_section_without_network(tmp_path):
    built = report.build_report(meta=_meta(), record=_record(), artifacts=[])

    html = report.write_html(tmp_path, built).read_text(encoding="utf-8")

    assert "网络剖面" not in html


# ---------- GPU 双源（ticket 16）：数据源、显存与降级链 ----------


def _gpu_summary():
    return {
        "gpu_util_pct": {"unit": "%", "source": "pc_supplement", "min": 7.0,
                         "median": 28.0, "mean": 25.0, "max": 35.0, "p90": 33.0,
                         "sampleCount": 20, "labelZh": "GPU占用",
                         "labelEn": "GPU utilization"},
        "gpu_mem_mb": {"unit": "MB", "source": "pc_supplement", "min": 120.0,
                       "median": 125.4, "mean": 125.0, "max": 130.0, "p90": 128.0,
                       "sampleCount": 20, "labelZh": "应用GPU显存",
                       "labelEn": "App GPU memory"},
    }


def test_write_html_renders_gpu_section_with_actual_source(tmp_path):
    """数据源必须写出来：kgsl 是窗口平均、dumpsys 是瞬时，混看会误判。"""
    built = report.build_report(
        meta=_meta(),
        record=_record(),
        artifacts=[],
        metrics_summary=_gpu_summary(),
        gpu={"enabled": True, "gpuSource": "kgsl_gpubusy", "degraded": []},
    )

    html = report.write_html(tmp_path, built).read_text(encoding="utf-8")

    assert "GPU / GPU" in html
    assert "应用GPU显存" in html
    assert "gpubusy" in html
    assert "窗口平均" in html  # 口径限定：非瞬时值
    assert "不可跨机型比较" in html  # §2.1a：同一负载在不同 GPU/系统上可差一倍以上
    assert "125.4" in html


def test_write_html_gpu_section_shows_degradation_chain(tmp_path):
    built = report.build_report(
        meta=_meta(),
        record=_record(),
        artifacts=[],
        metrics_summary={},
        gpu={
            "enabled": True,
            "gpuSource": None,
            "degraded": [
                {"key": "gpu_util_pct", "reason": "output_unparseable_or_unsupported",
                 "stages": ["kgsl:kgsl_not_readable", "dumpsys:unparseable"]}
            ],
        },
    )

    html = report.write_html(tmp_path, built).read_text(encoding="utf-8")

    assert "降级项" in html
    assert "kgsl:kgsl_not_readable" in html
    assert "dumpsys:unparseable" in html
    assert "不产出 0 值指标" in html


def test_write_html_omits_gpu_section_without_gpu(tmp_path):
    built = report.build_report(meta=_meta(), record=_record(), artifacts=[])

    html = report.write_html(tmp_path, built).read_text(encoding="utf-8")

    assert "GPU / GPU" not in html


# ---------- 解密档与接口瀑布图（ticket 18） ----------


def _api_breakdown():
    return {
        "top": [
            {"host": "api.trainapp.com", "route": "POST /v1/login", "count": 3,
             "medianMs": 620.0, "p90Ms": 980.0, "maxMs": 1100.0, "errors": 1,
             "payloadKb": 12.5},
            {"host": "cdn.trainapp.com", "route": "GET /static/app.js", "count": 1,
             "medianMs": 45.0, "p90Ms": 45.0, "maxMs": 45.0, "errors": 0,
             "payloadKb": 220.0},
        ],
        "totalRoutes": 7,
        "byHost": {"api.trainapp.com": 5, "cdn.trainapp.com": 2},
        "errors": 1,
    }


def _decrypt_meta(**overrides):
    return _network_meta(
        tier="decrypt",
        trust="trusted",
        trustError=None,
        apiEffective=True,
        apiNotTestedReason=None,
        **overrides,
    )


def test_build_report_records_api_block_with_its_own_source():
    built = report.build_report(
        meta=_meta(), record=_record(), artifacts=[],
        network=_decrypt_meta(), api=_api_breakdown(),
    )

    assert built["api"]["source"] == "mitmproxy_decrypt"
    assert built["network"]["tier"] == "decrypt"
    assert built["api"]["totalRoutes"] == 7


def test_write_html_renders_api_waterfall_sorted_by_p90(tmp_path):
    built = report.build_report(
        meta=_meta(), record=_record(), artifacts=[],
        network=_decrypt_meta(), api=_api_breakdown(),
    )

    html = report.write_html(tmp_path, built).read_text(encoding="utf-8")

    assert "接口瀑布图" in html
    assert "API waterfall" in html
    assert "POST /v1/login" in html
    assert html.index("POST /v1/login") < html.index("GET /static/app.js")  # 慢的在上
    assert "980.0" in html  # P90
    assert "排序依据 P90" in html  # 口径必须写明，避免当成中位
    # 域名分组正确
    assert "api.trainapp.com：5 次" in html
    assert "cdn.trainapp.com：2 次" in html
    assert "共 7 个接口路由" in html
    # 与连接级口径的差异必须明示，不可互相换算
    assert "不可互相换算" in html


def test_write_html_network_section_shows_decrypt_trust_verdict(tmp_path):
    built = report.build_report(
        meta=_meta(), record=_record(), artifacts=[],
        network=_decrypt_meta(), api=_api_breakdown(),
    )

    html = report.write_html(tmp_path, built).read_text(encoding="utf-8")

    assert "解密 / decrypt" in html
    assert "信任探测" in html
    assert "成功 / trusted" in html


def test_write_html_network_section_shows_api_unavailable_reason(tmp_path):
    """证书不信任时：原因与下一步都进网络小节，且不渲染瀑布图（没数据不画空表）。"""
    built = report.build_report(
        meta=_meta(),
        record=_record(),
        artifacts=[],
        network=_network_meta(
            tier="decrypt",
            trust="rejected",
            trustError="tlsv1 alert unknown ca",
            apiEffective=False,
            apiNotTestedReason=(
                "被测 App 不信任代理 CA（tlsv1 alert unknown ca），本 run 不重试解密"
            ),
        ),
    )

    html = report.write_html(tmp_path, built).read_text(encoding="utf-8")

    assert "证书不信任 / rejected" in html
    assert "tlsv1 alert unknown ca" in html
    assert "接口级不可用" in html
    assert "本 run 不重试解密" in html
    assert "接口瀑布图" not in html  # 没有接口数据就不出这一栏


def test_write_html_passthrough_network_section_says_no_url():
    built = report.build_report(
        meta=_meta(), record=_record(), artifacts=[], network=_network_meta()
    )

    markup = report._network_markup(built)

    assert "直通档 / passthrough" in markup
    assert "无 URL / 状态码" in markup
    assert "信任探测" not in markup  # 直通档不解密，没有信任结论可说


# ---------- 整页加载耗时（ticket 19）：逐页呈现与时间轴标注 ----------


def _pages_meta(**overrides):
    meta = {
        "quietGapMs": 300.0,
        "timeoutMs": 15000.0,
        "clockOffsetS": -1000.025,
        "clockOffsetSource": "frame_anchor",
        "clockAnchorCount": 12,
        "clockPrecisionMs": 25.0,
        "clockSpreadMs": 180.0,
        "status": "ok",
        "pages": [
            {"page": "home", "status": "ok", "t0S": 1.0, "pageLoadMs": 1200.0,
             "stableAtS": 2.2, "frameCount": 3, "lastFrameS": 2.2, "reason": None},
            {"page": "carousel", "status": "timeout", "t0S": 5.0, "pageLoadMs": None,
             "stableAtS": None, "frameCount": 200, "lastFrameS": 20.0,
             "reason": "观测 15000 ms 内画面始终有新帧（无限动画/持续刷新），"
                       "未出现 ≥300 ms 的静默窗口"},
        ],
    }
    meta.update(overrides)
    return meta


def test_build_report_records_pages_block_with_its_own_source():
    built = report.build_report(
        meta=_meta(), record=_record(), artifacts=[], pages=_pages_meta()
    )

    assert built["pages"]["source"] == "pageflow_last_frame_stable"
    assert built["pages"]["quietGapMs"] == 300.0
    assert [item["page"] for item in built["pages"]["pages"]] == ["home", "carousel"]


def test_build_report_without_pages_omits_block():
    built = report.build_report(meta=_meta(), record=_record(), artifacts=[])

    assert "pages" not in built


def test_write_html_renders_page_section_with_per_page_status(tmp_path):
    built = report.build_report(
        meta=_meta(), record=_record(), artifacts=[], pages=_pages_meta()
    )

    html = report.write_html(tmp_path, built).read_text(encoding="utf-8")

    assert "整页加载耗时 / Page load" in html
    assert "1200" in html
    # 不可判定的页必须如实写 not_tested 与原因，不能留空
    assert "not_tested" in html
    assert "无限动画" in html
    assert "超时（持续动画）" in html
    # 与启动耗时口径不同这件事必须写在页面上
    assert "不可互相换算" in html


def test_write_html_page_section_explains_clock_offset_source(tmp_path):
    built = report.build_report(
        meta=_meta(), record=_record(), artifacts=[], pages=_pages_meta()
    )

    html = report.write_html(tmp_path, built).read_text(encoding="utf-8")

    assert "逐帧轮询锚点换算" in html
    assert "12 个锚点" in html  # 偏移由几轮锚点估出来的必须写出来
    assert "±25.0 ms" in html  # 可信度必须写出来，否则读者无从判断 700 ms 准不准


def test_write_html_page_section_says_why_no_clock_offset(tmp_path):
    built = report.build_report(
        meta=_meta(), record=_record(), artifacts=[],
        pages=_pages_meta(clockOffsetS=None, clockOffsetSource="no_frames_anchor",
                          clockAnchorCount=None, clockSpreadMs=None,
                          status="degraded", pages=[]),
    )

    html = report.write_html(tmp_path, built).read_text(encoding="utf-8")

    assert "没有可用帧时刻" in html
    assert "本 run 不硬算" in html


def test_write_html_page_section_says_frames_were_never_polled(tmp_path):
    built = report.build_report(
        meta=_meta(), record=_record(), artifacts=[],
        pages=_pages_meta(clockOffsetS=None, clockOffsetSource="no_frame_poll",
                          clockAnchorCount=None, clockSpreadMs=None,
                          status="degraded", pages=[]),
    )

    html = report.write_html(tmp_path, built).read_text(encoding="utf-8")

    assert "未开启逐帧采集" in html


def test_page_markline_appends_to_startup_markline(tmp_path):
    """启动点与页面稳定点共用一条 markLine：必须追加，不能互相覆盖。"""
    built = report.build_report(
        meta={**_meta(), "startup": STARTUP_META},
        record=_record(),
        artifacts=[],
        series={
            "mem_pss_mb": {"unit": "MB", "labelZh": "应用PSS内存", "points": [[0.0, 42.0]]}
        },
        pages=_pages_meta(),
    )

    html = report.write_html(tmp_path, built).read_text(encoding="utf-8")

    script = _strip_vendor(html)
    assert "启动点" in script
    # 页面稳定点以数据形式注入（名字在浏览器侧拼出来），故只看注入的 points 段
    points = script[script.index("var points = "):]
    points = points[: points.index(";")]
    assert '"page": "home"' in points and '"ms": 1200.0' in points
    assert "carousel" not in points  # 超时页没有稳定点，不画线
    # 追加语义：先判已有 markLine 再 concat，而不是直接赋值覆盖
    assert "markLine.data.concat" in script


def test_page_markline_absent_when_no_page_is_stable(tmp_path):
    built = report.build_report(
        meta=_meta(), record=_record(), artifacts=[],
        pages=_pages_meta(pages=[_pages_meta()["pages"][1]]),
    )

    html = report.write_html(tmp_path, built).read_text(encoding="utf-8")

    assert "markLine" not in _strip_vendor(html)


# ---- P0-01：内联 JavaScript 注入防护（页面名来自宿主 mark_page，不可假设可信） ----

_SCRIPT_PAYLOAD = "</script><script>alert(1)</script>"


def _pages_meta_with_page(name, **extra):
    item = {"page": name, "status": "ok", "t0S": 1.0, "pageLoadMs": 1200.0,
            "stableAtS": 2.2, "frameCount": 3, "lastFrameS": 2.2, "reason": None}
    item.update(extra)
    return _pages_meta(pages=[item])


def test_page_markline_neutralizes_script_injection(tmp_path):
    """恶意页面名不得在 script 上下文出现原始闭合序列，嵌入数据须可原样还原。"""
    built = report.build_report(
        meta=_meta(), record=_record(), artifacts=[],
        pages=_pages_meta_with_page(_SCRIPT_PAYLOAD),
    )

    html = report.write_html(tmp_path, built).read_text(encoding="utf-8")

    # 原始 payload 不得原样出现在 HTML 任何位置（script 数据块与 HTML 文本均不得）
    assert _SCRIPT_PAYLOAD not in html
    # 内联 points 数据解析后与原始输入完全一致
    script = _strip_vendor(html)
    start = script.index("var points = ") + len("var points = ")
    points, _ = json.JSONDecoder().raw_decode(script[start:])
    assert points[0]["page"] == _SCRIPT_PAYLOAD


def test_report_data_block_neutralizes_script_injection(tmp_path):
    """同源报告数据块 window.__REPORT__ 对同一 payload 也不逃逸，且解析回原值。"""
    built = report.build_report(
        meta=_meta(), record=_record(), artifacts=[],
        pages=_pages_meta_with_page(_SCRIPT_PAYLOAD),
    )

    html = report.write_html(tmp_path, built).read_text(encoding="utf-8")

    marker = "window.__REPORT__ = "
    start = html.index(marker) + len(marker)
    data, _ = json.JSONDecoder().raw_decode(html[start:])
    assert data["pages"]["pages"][0]["page"] == _SCRIPT_PAYLOAD


def test_page_markline_keeps_normal_names_round_trip(tmp_path):
    """普通中文/空格/引号/反斜杠/emoji 页面名：数据原样还原且表格可见。"""
    names = ["首页 Home", r'引号"页\反斜杠', "首页 🚀"]
    built = report.build_report(
        meta=_meta(), record=_record(), artifacts=[],
        pages=_pages_meta(pages=[
            {**_pages_meta()["pages"][0], "page": name, "stableAtS": 2.0 + i}
            for i, name in enumerate(names)
        ]),
    )

    html = report.write_html(tmp_path, built).read_text(encoding="utf-8")

    script = _strip_vendor(html)
    start = script.index("var points = ") + len("var points = ")
    points, _ = json.JSONDecoder().raw_decode(script[start:])
    assert [item["page"] for item in points] == names
    # 整页加载表格（HTML 文本上下文）仍按转义后的名字呈现
    for name in names:
        assert html_escape(name) in html


def test_jank_threshold_line_renders_numeric_config(tmp_path):
    """宿主配置数值阈值照常画出阈值线（light<heavy<severe 校验后）。"""
    built = report.build_report(
        meta=_meta(), record=_record(), artifacts=[],
        jank_thresholds_ms={"light": 34.0, "heavy": 300.0, "severe": 700.0},
        metrics_summary={"app_jank_light_count": {"median": 1.0, "sampleCount": 2}},
        series={
            "frame_delay_max_ms": {
                "unit": "ms", "labelZh": "最长帧延迟", "points": [[0.0, 50.0]]
            }
        },
    )

    html = report.write_html(tmp_path, built).read_text(encoding="utf-8")

    assert "yAxis: 34.0" in html


def test_jank_threshold_line_skips_non_numeric_values():
    """渲染层兜底：绕过 build_report 校验构造的 report 字典，非数值/NaN 阈值不进 JS。"""
    script = report._jank_threshold_line_script({
        "series": {"frame_delay_max_ms": {"unit": "ms", "points": [[0.0, 50.0]]}},
        "jank": {"thresholdsMs": {
            "light": '0", alert(1), "', "heavy": float("nan"), "severe": 700.0,
        }},
    })

    assert "alert(1)" not in script
    assert "700.0" in script


def test_build_report_rejects_non_numeric_thresholds():
    """build_report 入口即拒绝非数值阈值（validate_thresholds 既有契约）。"""
    with pytest.raises(ValueError):
        report.build_report(
            meta=_meta(), record=_record(), artifacts=[],
            jank_thresholds_ms={
                "light": 34.0, "heavy": '0", alert(1), "', "severe": 700.0,
            },
            metrics_summary={"app_jank_light_count": {"median": 1.0}},
        )


def test_network_markup_shows_restore_errors_and_doctor_hint(tmp_path):
    """恢复失败在报告网络小节明示，并指向 doctor 兜底（P1-07）。"""
    built = report.build_report(
        meta=_meta(), record=_record(), artifacts=[],
        network={
            "tier": "passthrough",
            "profile": {"name": "3g"},
            "effective": True,
            "restored": {"deviceProxy": True, "reverse": False},
            "restoreErrors": ["恢复 reverse 失败: adb 连接中断"],
        },
    )

    markup = report._network_markup(built)

    assert "恢复失败 / Restore errors" in markup
    assert "恢复 reverse 失败" in markup
    assert "netprof doctor" in markup


# ---- P1-04 复核：报告级 XSS 负向（api 统计字段等宿主可影响字段） ----

_XSS_PAYLOAD = "</td><script>alert(1)</script>"


def test_write_html_neutralizes_xss_in_api_and_host_fields(tmp_path):
    """复核注入复现：API 统计字段与宿主字段注入 </td><script> 不得进入 HTML。

    修复前 _api_markup 的 count/errors/byHost 计数与 _summary/_gpu/_jank 的
    sampleCount 原样插值，payload 直接出现在 HTML（复核实测）。
    """
    built = report.build_report(
        meta={**_meta(), "target": _XSS_PAYLOAD},
        record={**_record(), "marks": [{"label": _XSS_PAYLOAD}]},
        artifacts=[],
        metrics_summary={"mem_pss_mb": {"median": 42.0, "sampleCount": _XSS_PAYLOAD}},
        api={
            "top": [{
                "host": _XSS_PAYLOAD, "route": _XSS_PAYLOAD, "count": _XSS_PAYLOAD,
                "medianMs": 1.0, "p90Ms": 2.0, "maxMs": 3.0,
                "errors": _XSS_PAYLOAD, "payloadKb": 4.0,
            }],
            "byHost": {_XSS_PAYLOAD: _XSS_PAYLOAD},
            "totalRoutes": _XSS_PAYLOAD,
            "errors": _XSS_PAYLOAD,
        },
    )

    html = report.write_html(tmp_path, built).read_text(encoding="utf-8")

    # 原始 payload 不得出现在 HTML 任何位置（表格与同源 JSON 块都不行）
    assert _XSS_PAYLOAD not in html
    # 数据保真：嵌入 JSON 解析后与原值逐字符一致（转义不破坏数据）
    embedded = json.loads(_extract_embedded_json(html))
    assert embedded["api"]["top"][0]["count"] == _XSS_PAYLOAD
    assert embedded["metricsSummary"]["mem_pss_mb"]["sampleCount"] == _XSS_PAYLOAD
    # 文本上下文如实呈现转义后的值（可见但不可执行）
    assert "&lt;/td&gt;&lt;script&gt;" in html
