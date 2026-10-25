"""卡顿分级（ticket 14）：口径受 SoloPi CSV 粒度限制，按每采样窗口最长延迟分桶。

真实 CSV 只有 ``最长延迟时间(ms)`` 单值列（每个采样窗口一个值），没有逐帧
延迟分布；因此分级口径为"会话内该窗口最长延迟超阈值的样本次数"，而不是
逐帧卡顿次数。分桶互斥：一个样本只计入其所属的最高档（>100 / >300 / >700）。
"""

import csv
from pathlib import Path

import pytest

from mobile_perf import jank


def test_default_thresholds_are_light_heavy_severe():
    assert jank.DEFAULT_THRESHOLDS_MS == {"light": 100.0, "heavy": 300.0, "severe": 700.0}


def test_grade_samples_buckets_by_strictly_greater_than_threshold():
    # 边界值本身不算超阈值（> 而非 >=），一档只计一次（互斥）
    samples = [13.0, 100.0, 100.1, 300.0, 300.1, 700.0, 700.1, 1000.0]

    grades = jank.grade_samples(samples)

    assert grades == {"light": 2, "heavy": 2, "severe": 2}


def test_grade_samples_with_custom_thresholds():
    samples = [40.0, 55.0, 58.0, 65.0, 120.0]

    grades = jank.grade_samples(samples, {"light": 50.0, "heavy": 60.0, "severe": 100.0})

    assert grades == {"light": 2, "heavy": 1, "severe": 1}


@pytest.mark.parametrize(
    "thresholds",
    [
        {"light": 300.0, "heavy": 100.0, "severe": 700.0},  # 非递增
        {"light": 100.0, "heavy": 100.0, "severe": 700.0},  # 重复边界
        {"light": 0.0, "heavy": 300.0, "severe": 700.0},  # 非正
        {"light": 100.0, "heavy": 300.0},  # 缺档
    ],
)
def test_grade_samples_rejects_invalid_thresholds(thresholds):
    with pytest.raises(ValueError):
        jank.grade_samples([10.0], thresholds)


def test_grade_samples_empty_session_yields_zero_counts():
    assert jank.grade_samples([]) == {"light": 0, "heavy": 0, "severe": 0}


def test_jank_metric_keys_are_registered_in_dictionary():
    """三个分级 canonical key 必须在指标字典登记：双语标签 + source=solo_pi 派生统计。"""
    from mobile_perf import metrics

    for grade, key in jank.GRADE_METRIC_KEYS.items():
        spec = metrics.METRICS[key]
        assert spec.unit == "次"
        assert spec.source == "solo_pi"
        assert spec.label_zh and spec.label_en
        assert spec.solo_columns == () and spec.solo_shapes == ()
        assert grade in key


def test_jank_keys_follow_snake_case_naming_convention():
    for key in jank.GRADE_METRIC_KEYS.values():
        parts = key.split("_")
        assert len(parts) >= 2 and all(part.isalnum() and part.islower() for part in parts)


FIXTURES = Path(__file__).parent.parent / "fixtures" / "solopi_csv_sample"
_MAX_DELAY_CSV = next(FIXTURES.glob("最长延迟时间_FPS_*.csv"))


def _real_max_delay_samples() -> list[float]:
    with _MAX_DELAY_CSV.open(encoding="gb18030", newline="") as fh:
        reader = csv.reader(fh)
        next(reader)
        return [float(row[1]) for row in reader if len(row) > 1 and row[1]]


def test_real_csv_max_delay_is_per_window_single_value():
    """粒度锁：真实 CSV 的「最长延迟时间」是逐采样窗口单值列，无逐帧分布。

    样本数 ≈ 采集秒数（1 s 一窗），且取值随窗口变化而非累计递增——这是
    "窗口超阈次数"口径成立、逐帧分桶不成立的事实依据。
    """
    samples = _real_max_delay_samples()

    assert 150 <= len(samples) <= 180  # 约 170 s 采集，1 s 一窗
    assert len(set(samples)) > 10  # 逐窗口变化，不是单一常量
    assert not all(b >= a for a, b in zip(samples, samples[1:], strict=False))  # 非累计序列


def test_real_csv_grades_use_default_thresholds_without_inventing_frame_data():
    samples = _real_max_delay_samples()

    grades = jank.grade_samples(samples)

    # 真实样本最长延迟 max=62 ms：默认阈值下三档均为 0，如实不造数
    assert max(samples) == 62.0
    assert grades == {"light": 0, "heavy": 0, "severe": 0}


def test_real_csv_bucket_boundaries_with_lowered_thresholds():
    """把阈值降到真实样本覆盖范围内，验证分桶边界落在真实数据上。"""
    samples = _real_max_delay_samples()

    grades = jank.grade_samples(samples, {"light": 25.0, "heavy": 40.0, "severe": 60.0})

    assert grades["light"] == sum(1 for v in samples if 25.0 < v <= 40.0)
    assert grades["heavy"] == sum(1 for v in samples if 40.0 < v <= 60.0)
    assert grades["severe"] == sum(1 for v in samples if v > 60.0)
    assert sum(grades.values()) == sum(1 for v in samples if v > 25.0)


# ---------- 会话内分级：从 artifacts 真实样本分桶并组装聚合行 ----------


def test_aggregate_from_artifacts_counts_real_window_samples(tmp_path):
    """从 artifacts 目录读「最长延迟时间」列，按阈值分桶并组装可入库聚合行。"""
    header = "RecordTime,最长延迟时间(ms),extra,SimpleTime"
    rows = [header] + [f"{i},{v},tv.danmaku.bili,{i}" for i, v in enumerate([20, 150, 400, 900])]
    (tmp_path / "最长延迟时间_FPS_abc.csv").write_text(
        "\n".join(rows) + "\n", encoding="gb18030"
    )

    aggregates = jank.aggregate_from_artifacts(tmp_path)
    by_key = {row["metric_key"]: row for row in aggregates}

    assert by_key["app_jank_light_count"]["max"] == 1.0
    assert by_key["app_jank_heavy_count"]["max"] == 1.0
    assert by_key["app_jank_severe_count"]["max"] == 1.0
    assert by_key["app_jank_severe_count"]["unit"] == "次"
    assert by_key["app_jank_severe_count"]["source"] == "solo_pi"
    assert by_key["app_jank_severe_count"]["sample_count"] == 1


def test_aggregate_from_artifacts_returns_none_when_no_fps_data(tmp_path):
    """无 FPS 采集（无最长延迟列）时不产出分级行，不硬造 0 值入库。"""
    (tmp_path / "performance.csv").write_text(
        "RecordTime,PSS-main-18390(MB),extra,SimpleTime\n1,42.0,,1\n", encoding="gb18030"
    )

    assert jank.aggregate_from_artifacts(tmp_path) == []


def test_aggregate_from_artifacts_honours_custom_thresholds(tmp_path):
    rows = ["RecordTime,最长延迟时间(ms),extra,SimpleTime", "1,40,,1", "2,65,,2", "3,80,,3"]
    (tmp_path / "最长延迟时间_FPS_abc.csv").write_text(
        "\n".join(rows) + "\n", encoding="gb18030"
    )

    aggregates = jank.aggregate_from_artifacts(
        tmp_path, {"light": 50.0, "heavy": 60.0, "severe": 70.0}
    )
    by_key = {row["metric_key"]: row for row in aggregates}

    # 40 不超 50；65 超 60 未超 70 → heavy；80 超 70 → severe（互斥取最高档）
    assert by_key["app_jank_light_count"]["max"] == 0.0
    assert by_key["app_jank_heavy_count"]["max"] == 1.0
    assert by_key["app_jank_severe_count"]["max"] == 1.0


# ---------- judge 三态：分级计数作为阈值目标（阈值归宿主） ----------


def _judge_report(severe_count: float, sample_count: int = 1) -> dict:
    return {
        "runId": "run-jank-1",
        "deviceProfile": "PICO4-Android12",
        "startedAt": "2026-10-25T10:00:00",
        "stoppedAt": "2026-10-25T10:01:00",
        "metricsSummary": {
            "app_jank_severe_count": {
                "unit": "次",
                "median": severe_count,
                "sampleCount": sample_count,
            }
        },
    }


def _judge_spec(threshold: float) -> object:
    from mobile_perf.judge import JudgeSpec

    return JudgeSpec(
        baseline="bili-jank-v1",
        thresholds={"app_jank_severe_count": ("le", threshold, "fail")},
        min_samples=1,
        baseline_profile="PICO4-Android12",
        baseline_data={"app_jank_severe_count": {"value": 0.0, "sourceRunId": "run-base"}},
    )


def test_judge_passes_when_severe_jank_within_threshold():
    from mobile_perf.judge import judge

    verdict = judge(_judge_report(0.0), _judge_spec(0.0))

    assert verdict.verdict == "passed"


def test_judge_fails_when_severe_jank_exceeds_threshold():
    from mobile_perf.judge import judge

    verdict = judge(_judge_report(3.0), _judge_spec(0.0))

    assert verdict.verdict == "failed"
    (check,) = verdict.checks
    assert check["metricKey"] == "app_jank_severe_count"
    assert check["ok"] is False


def test_judge_is_not_tested_when_jank_metric_missing():
    """未采集 FPS 的 run 没有分级指标 → 不判定（三态里的 not_tested）。"""
    from mobile_perf.judge import judge

    report = _judge_report(0.0)
    report["metricsSummary"] = {}

    verdict = judge(report, _judge_spec(0.0))

    assert verdict.verdict == "not_tested"


# ---------- 报告呈现：分级聚合表小节 + 分级曲线（含阈值线） ----------


def _report_with_jank() -> dict:
    from mobile_perf import report as report_module

    return report_module.build_report(
        meta={
            "runId": "run-jank-report",
            "target": "tv.danmaku.bili",
            "items": ["CPU", "FPS"],
            "serial": "749622c9",
            "startedAt": "2026-10-25T10:00:00",
            "stoppedAt": "2026-10-25T10:01:00",
            "durationS": 60.0,
        },
        record={"sessionId": "sid-1"},
        artifacts=["a/最长延迟时间_FPS.csv"],
        metrics_summary={
            "app_jank_light_count": {
                "unit": "次", "source": "solo_pi", "median": 2.0,
                "max": 2.0, "sampleCount": 1,
            },
            "app_jank_heavy_count": {
                "unit": "次", "source": "solo_pi", "median": 1.0,
                "max": 1.0, "sampleCount": 1,
            },
            "app_jank_severe_count": {
                "unit": "次", "source": "solo_pi", "median": 0.0,
                "max": 0.0, "sampleCount": 1,
            },
        },
        series={
            "frame_delay_max_ms": {
                "unit": "ms", "labelZh": "最长帧延迟", "labelEn": "Max frame delay",
                "points": [[0.0, 25.0], [1.0, 400.0]],
            }
        },
    )


def test_write_html_renders_jank_grading_section(tmp_path):
    from mobile_perf import report as report_module

    report_module.write_html(tmp_path, _report_with_jank())

    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "卡顿分级" in html and "Jank grading" in html
    assert "轻度卡顿窗口数(&gt;100ms)" in html or "轻度卡顿窗口数(>100ms)" in html
    assert "严重卡顿窗口数" in html
    # 阈值线：分级阈值来自 jank 默认口径（100/300/700 ms）
    assert "700" in html and "jank-threshold" in html


def test_write_html_omits_jank_section_without_jank_metrics(tmp_path):
    from mobile_perf import report as report_module

    built = report_module.build_report(
        meta={
            "runId": "run-no-jank", "target": "x", "items": ["CPU"],
            "serial": "s", "startedAt": "a", "stoppedAt": "b", "durationS": 1.0,
        },
        record={"sessionId": "sid"},
        artifacts=[],
        metrics_summary={"mem_pss_mb": {"unit": "MB", "median": 1.0, "sampleCount": 1}},
    )
    report_module.write_html(tmp_path, built)

    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "卡顿分级" not in html


def test_build_report_records_jank_method_and_thresholds():
    """报告数据带口径说明与阈值，供宿主与 HTML 呈现共用（默认阈值也显式记录）。"""
    from mobile_perf import report as report_module

    built = report_module.build_report(
        meta={
            "runId": "r", "target": "x", "items": ["FPS"], "serial": "s",
            "startedAt": "a", "stoppedAt": "b", "durationS": 1.0,
        },
        record={"sessionId": "sid"},
        artifacts=[],
        metrics_summary={
            "app_jank_severe_count": {"unit": "次", "median": 0.0, "max": 0.0, "sampleCount": 1}
        },
        jank_thresholds_ms={"light": 50.0, "heavy": 200.0, "severe": 500.0},
    )

    assert built["jank"]["thresholdsMs"] == {"light": 50.0, "heavy": 200.0, "severe": 500.0}
    assert built["jank"]["sourceMetric"] == "frame_delay_max_ms"
    assert built["jank"]["granularity"] == "per_sampling_window"


def test_assemble_run_report_reuses_thresholds_from_meta(tmp_path):
    """report 子命令重组报告时沿用 meta.json 记录的自定义阈值（口径不漂移）。"""
    import json

    from mobile_perf import report as report_module
    from mobile_perf.store import PerfStore

    run_dir = tmp_path / "runs" / "run-jank-meta"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "runId": "run-jank-meta",
                "target": "tv.danmaku.bili",
                "items": ["FPS"],
                "serial": "749622c9",
                "startedAt": "a",
                "stoppedAt": "b",
                "durationS": 1.0,
                "jank": {"thresholdsMs": {"light": 50.0, "heavy": 200.0, "severe": 500.0}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = PerfStore(tmp_path / "perf.db")
    store.record_session(
        {
            "run_id": "run-jank-meta",
            "solo_session_id": "sid",
            "serial": "749622c9",
            "device_profile": None,
            "target": "tv.danmaku.bili",
            "items": ["FPS"],
            "started_at": "a",
            "stopped_at": "b",
            "duration_s": 1.0,
            "host_run_id": None,
            "status": "stopped",
            "run_dir": str(run_dir),
        }
    )
    store.record_metrics(
        "run-jank-meta",
        [
            {
                "metric_key": "app_jank_severe_count",
                "unit": "次",
                "source": "solo_pi",
                "min": 0.0,
                "median": 0.0,
                "mean": 0.0,
                "max": 0.0,
                "p90": 0.0,
                "sample_count": 1,
            }
        ],
    )

    built = report_module.assemble_run_report(
        "run-jank-meta", runs_dir=tmp_path / "runs", store=store
    )

    assert built["jank"]["thresholdsMs"] == {"light": 50.0, "heavy": 200.0, "severe": 500.0}
