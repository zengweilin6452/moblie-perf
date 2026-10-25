"""judge 三态门禁的单元测试：纯函数直接喂构造的 report 字典，无需设备。"""

from dataclasses import replace

import pytest

from mobile_perf.judge import DEFAULT_MIN_SAMPLES, JudgeSpec, judge


def make_report(**overrides):
    """构造最小可判定的 run 报告：单指标 mem_pss_mb，样本数默认充足。"""
    report = {
        "runId": "run-under-judgement",
        "target": "tv.danmaku.bili",
        "deviceProfile": "PICO4-Android12",
        "startedAt": "2026-10-25T10:00:00",
        "stoppedAt": "2026-10-25T10:01:00",
        "metricsSummary": {
            "mem_pss_mb": {
                "unit": "MB",
                "median": 150.0,
                "sampleCount": 30,
            }
        },
    }
    report.update(overrides)
    return report


def make_spec(**overrides):
    """基线绑定同档案、与阈值同指标；thresholds 归宿主显式构造。"""
    spec = JudgeSpec(
        baseline="bili-home-v3",
        thresholds={"mem_pss_mb": ("lt", 220.0, "fail")},
        baseline_profile="PICO4-Android12",
        baseline_data={"mem_pss_mb": {"value": 180.0, "sourceRunId": "run-baseline-src"}},
    )
    return replace(spec, **overrides)


def test_all_thresholds_within_bounds_passes_with_evidence():
    verdict = judge(make_report(), make_spec())

    assert verdict.verdict == "passed"
    assert verdict.reason is None
    assert verdict.baseline == "bili-home-v3"
    (check,) = verdict.checks
    assert check == {
        "metricKey": "mem_pss_mb",
        "median": 150.0,
        "op": "lt",
        "threshold": 220.0,
        "level": "fail",
        "ok": True,
        "baselineValue": 180.0,
        "baselineSourceRunId": "run-baseline-src",
        "sampleCount": 30,
        "sourceRunId": "run-under-judgement",
        "window": {
            "startedAt": "2026-10-25T10:00:00",
            "stoppedAt": "2026-10-25T10:01:00",
        },
    }


def test_threshold_violation_at_fail_level_fails_verdict():
    report = make_report(
        metricsSummary={"mem_pss_mb": {"unit": "MB", "median": 250.0, "sampleCount": 30}}
    )

    verdict = judge(report, make_spec())

    assert verdict.verdict == "failed"
    (check,) = verdict.checks
    assert check["ok"] is False
    assert check["median"] == 250.0
    assert check["level"] == "fail"


def test_threshold_violation_at_warn_level_keeps_pass_but_records_warning():
    report = make_report(
        metricsSummary={"mem_pss_mb": {"unit": "MB", "median": 250.0, "sampleCount": 30}}
    )
    spec = make_spec(thresholds={"mem_pss_mb": ("lt", 220.0, "warn")})

    verdict = judge(report, spec)

    assert verdict.verdict == "passed"
    assert verdict.warnings == [verdict.checks[0]]
    assert verdict.checks[0]["ok"] is False
    assert verdict.checks[0]["level"] == "warn"


def test_warn_and_fail_mixed_violations_fail_overall():
    report = make_report(
        metricsSummary={
            "mem_pss_mb": {"unit": "MB", "median": 250.0, "sampleCount": 30},
            "cpu_app_pct": {"unit": "%", "median": 90.0, "sampleCount": 30},
        }
    )
    spec = make_spec(
        thresholds={
            "mem_pss_mb": ("lt", 220.0, "warn"),
            "cpu_app_pct": ("lt", 50.0, "fail"),
        },
        baseline_data={
            "mem_pss_mb": {"value": 180.0, "sourceRunId": "run-baseline-src"},
            "cpu_app_pct": {"value": 30.0, "sourceRunId": "run-baseline-src"},
        },
    )

    verdict = judge(report, spec)

    assert verdict.verdict == "failed"
    assert len(verdict.checks) == 2
    assert len(verdict.warnings) == 1


def test_sample_count_below_minimum_is_not_tested():
    report = make_report(
        metricsSummary={"mem_pss_mb": {"unit": "MB", "median": 150.0, "sampleCount": 3}}
    )

    verdict = judge(report, make_spec())

    assert verdict.verdict == "not_tested"
    assert "样本" in verdict.reason


def test_sample_count_minimum_can_be_overridden_per_spec():
    report = make_report(
        metricsSummary={"mem_pss_mb": {"unit": "MB", "median": 150.0, "sampleCount": 3}}
    )
    spec = make_spec(min_samples=3)

    verdict = judge(report, spec)

    assert verdict.verdict == "passed"


def test_default_min_samples_is_a_named_component_constant():
    assert DEFAULT_MIN_SAMPLES > 0
    spec = make_spec(min_samples=None)
    assert spec.effective_min_samples == DEFAULT_MIN_SAMPLES


def test_device_profile_mismatch_is_not_tested():
    report = make_report(deviceProfile="PICO4-Android14")

    verdict = judge(report, make_spec())

    assert verdict.verdict == "not_tested"
    assert "档案" in verdict.reason


def test_metric_missing_from_run_is_not_tested():
    report = make_report(metricsSummary={})

    verdict = judge(report, make_spec())

    assert verdict.verdict == "not_tested"


def test_metric_without_summary_entry_is_not_tested():
    report = make_report(
        metricsSummary={"cpu_app_pct": {"unit": "%", "median": 10.0, "sampleCount": 30}}
    )

    verdict = judge(report, make_spec())  # 阈值引用的 mem_pss_mb 不在 run 里

    assert verdict.verdict == "not_tested"
    assert "mem_pss_mb" in verdict.reason


def test_threshold_metric_missing_from_baseline_is_not_tested():
    spec = make_spec(baseline_data={})

    verdict = judge(make_report(), spec)

    assert verdict.verdict == "not_tested"
    assert "bili-home-v3" in verdict.reason


def test_empty_baseline_data_is_not_tested():
    verdict = judge(make_report(), make_spec(baseline_data={}))

    assert verdict.verdict == "not_tested"


def test_run_report_without_metrics_summary_is_not_tested():
    verdict = judge(make_report(metricsSummary=None), make_spec())

    assert verdict.verdict == "not_tested"


def test_verdict_to_dict_is_json_ready():
    verdict = judge(make_report(), make_spec())

    payload = verdict.to_dict()

    assert payload["verdict"] == "passed"
    assert payload["baseline"] == "bili-home-v3"
    assert payload["checks"] == verdict.checks
    assert payload["warnings"] == []


@pytest.mark.parametrize(
    ("op", "median", "threshold", "ok"),
    [
        ("lt", 219.9, 220.0, True),
        ("lt", 220.0, 220.0, False),
        ("le", 220.0, 220.0, True),
        ("gt", 50.1, 50.0, True),
        ("gt", 50.0, 50.0, False),
        ("ge", 50.0, 50.0, True),
    ],
)
def test_comparison_operators_boundary_semantics(op, median, threshold, ok):
    report = make_report(
        metricsSummary={"mem_pss_mb": {"unit": "MB", "median": median, "sampleCount": 30}}
    )
    spec = make_spec(thresholds={"mem_pss_mb": (op, threshold, "fail")})

    verdict = judge(report, spec)

    assert verdict.verdict == ("passed" if ok else "failed")
    assert verdict.checks[0]["ok"] is ok


def test_unknown_operator_is_rejected_at_spec_construction():
    with pytest.raises(ValueError, match="op"):
        make_spec(thresholds={"mem_pss_mb": ("between", 1.0, "fail")})


def test_unknown_level_is_rejected_at_spec_construction():
    with pytest.raises(ValueError, match="level"):
        make_spec(thresholds={"mem_pss_mb": ("lt", 1.0, "error")})


# ---- A1：not_tested 标注的指标跳过判定 ----

def test_not_tested_metric_is_skipped_and_evidenced():
    report = make_report(
        metricsSummary={
            "mem_pss_mb": {"unit": "MB", "median": 150.0, "sampleCount": 30},
            "battery_current_ma": {
                "unit": "mA",
                "median": -120.0,
                "sampleCount": 30,
                "notTested": True,
                "notTestedReason": "设备充电中，Battery 项降级 not_tested",
            },
        }
    )
    spec = make_spec(
        thresholds={
            "mem_pss_mb": ("lt", 220.0, "fail"),
            "battery_current_ma": ("gt", -200.0, "fail"),
        },
        baseline_data={
            "mem_pss_mb": {"value": 180.0, "sourceRunId": "run-baseline-src"},
            "battery_current_ma": {"value": -150.0, "sourceRunId": "run-baseline-src"},
        },
    )

    verdict = judge(report, spec)

    assert verdict.verdict == "passed"  # 其余指标照常判定
    assert [c["metricKey"] for c in verdict.checks] == ["mem_pss_mb"]
    (skipped,) = [w for w in verdict.warnings if w.get("notTested")]
    assert skipped["metricKey"] == "battery_current_ma"
    assert "充电" in skipped["reason"]


def test_all_threshold_metrics_not_tested_verdict_not_tested():
    report = make_report(
        metricsSummary={
            "mem_pss_mb": {
                "unit": "MB",
                "median": 150.0,
                "sampleCount": 30,
                "notTested": True,
                "notTestedReason": "设备充电中",
            },
        }
    )

    verdict = judge(report, make_spec())

    assert verdict.verdict == "not_tested"
    assert "not_tested" in verdict.reason and "mem_pss_mb" in verdict.reason
    assert verdict.checks == []
