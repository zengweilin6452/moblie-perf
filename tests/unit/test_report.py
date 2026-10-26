"""report 骨架的单元测试：报告聚合与落盘（M3 补齐图表数据）。"""

import json

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
