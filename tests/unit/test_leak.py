"""内存泄漏趋势分析（ticket 13）：斜率纯函数、三态判定与 kit 门面。

口径归组件：OLS 对 median 随墙钟时间回归，样本下限默认 3 个 run，
时间跨度 < 24h 标注低置信度而不拒绝；阈值归宿主（leak_verdict 显式传入）。
"""

from __future__ import annotations

import pytest

from mobile_perf import kit as kit_module
from mobile_perf import leak
from mobile_perf.judge import VERDICT_FAILED, VERDICT_NOT_TESTED, VERDICT_PASSED
from mobile_perf.store import PerfStore


def _run(run_id: str, started_at: str, median: float, duration_s: float = 30.0) -> dict:
    return {
        "runId": run_id,
        "startedAt": started_at,
        "durationS": duration_s,
        "median": median,
    }


def _day(index: int, hour: int = 0) -> str:
    return f"2026-10-{20 + index:02d}T{hour:02d}:00:00+08:00"


# --- 斜率纯函数：合成序列三分支 -------------------------------------------


def test_stable_series_has_near_zero_slope():
    runs = [_run(f"r{i}", _day(i), 200.0) for i in range(3)]

    analysis = leak.analyze_leak_trend(runs)

    assert analysis.slope_mb_per_day == pytest.approx(0.0, abs=1e-9)
    assert analysis.run_count == 3
    assert analysis.span_hours == pytest.approx(48.0)
    assert analysis.confidence == leak.CONFIDENCE_OK
    assert analysis.insufficient is False


def test_linear_growth_series_recovers_expected_slope():
    # 每日 +10 MB，独立手算：100 → 110 → 120
    runs = [
        _run("r0", _day(0), 100.0),
        _run("r1", _day(1), 110.0),
        _run("r2", _day(2), 120.0),
    ]

    analysis = leak.analyze_leak_trend(runs)

    assert analysis.slope_mb_per_day == pytest.approx(10.0)
    assert analysis.confidence == leak.CONFIDENCE_OK
    assert analysis.first_median == pytest.approx(100.0)
    assert analysis.last_median == pytest.approx(120.0)


def test_step_jump_series_slope_is_pulled_by_outlier_run():
    # 100 → 100 → 200，OLS 手算 slope = 2.0833 MB/h = 50 MB/day
    runs = [
        _run("r0", _day(0), 100.0),
        _run("r1", _day(1), 100.0),
        _run("r2", _day(2), 200.0),
    ]

    analysis = leak.analyze_leak_trend(runs)

    assert analysis.slope_mb_per_day == pytest.approx(50.0)
    assert analysis.run_count == 3
    assert analysis.span_hours == pytest.approx(48.0)


def test_below_min_runs_yields_no_slope_and_insufficient_confidence():
    runs = [_run("r0", _day(0), 100.0), _run("r1", _day(1), 120.0)]

    analysis = leak.analyze_leak_trend(runs)

    assert analysis.slope_mb_per_day is None
    assert analysis.confidence == leak.CONFIDENCE_INSUFFICIENT
    assert analysis.insufficient is True
    assert analysis.run_count == 2
    assert "3" in analysis.confidence_reason  # 如实说明所需样本下限


def test_min_runs_is_overridable():
    runs = [_run("r0", _day(0), 100.0), _run("r1", _day(1), 120.0)]

    analysis = leak.analyze_leak_trend(runs, min_runs=2)

    assert analysis.slope_mb_per_day == pytest.approx(20.0)
    assert analysis.insufficient is False


def test_short_span_is_low_confidence_but_slope_still_reported():
    # 3 个 run 仅跨 2 小时：斜率可算但时间跨度不足 24h，标低置信度不拒绝
    runs = [
        _run("r0", "2026-10-20T10:00:00+08:00", 100.0),
        _run("r1", "2026-10-20T11:00:00+08:00", 101.0),
        _run("r2", "2026-10-20T12:00:00+08:00", 102.0),
    ]

    analysis = leak.analyze_leak_trend(runs)

    assert analysis.slope_mb_per_day == pytest.approx(24.0)
    assert analysis.confidence == leak.CONFIDENCE_LOW
    assert analysis.insufficient is False
    assert "24" in analysis.confidence_reason


def test_zero_span_series_cannot_be_fitted():
    runs = [_run(f"r{i}", "2026-10-20T10:00:00+08:00", 100.0 + i) for i in range(3)]

    analysis = leak.analyze_leak_trend(runs)

    assert analysis.slope_mb_per_day is None
    assert analysis.confidence == leak.CONFIDENCE_INSUFFICIENT


def test_runs_are_sorted_by_started_at_regardless_of_input_order():
    runs = [
        _run("r2", _day(2), 120.0),
        _run("r0", _day(0), 100.0),
        _run("r1", _day(1), 110.0),
    ]

    analysis = leak.analyze_leak_trend(runs)

    assert [item["runId"] for item in analysis.runs] == ["r0", "r1", "r2"]
    assert analysis.slope_mb_per_day == pytest.approx(10.0)


# --- 三态判定：宿主阈值 ---------------------------------------------------


def _linear_analysis(slope_series=(100.0, 110.0, 120.0)) -> leak.LeakTrend:
    runs = [
        _run(f"r{i}", _day(i), median) for i, median in enumerate(slope_series)
    ]
    return leak.analyze_leak_trend(runs)


def test_leak_verdict_fails_when_slope_exceeds_host_threshold():
    analysis = _linear_analysis()  # 10 MB/day

    verdict = leak.leak_verdict(analysis, threshold=5.0, level="fail")

    assert verdict.verdict == VERDICT_FAILED
    check = verdict.checks[0]
    assert check["slopeMbPerDay"] == pytest.approx(10.0)
    assert check["threshold"] == 5.0
    assert check["runCount"] == 3
    assert check["spanHours"] == pytest.approx(48.0)
    assert check["ok"] is False


def test_leak_verdict_passes_when_slope_within_threshold():
    analysis = _linear_analysis()

    verdict = leak.leak_verdict(analysis, threshold=20.0, level="fail")

    assert verdict.verdict == VERDICT_PASSED
    assert verdict.checks[0]["ok"] is True


def test_leak_verdict_warn_level_keeps_passed_with_warning():
    analysis = _linear_analysis()

    verdict = leak.leak_verdict(analysis, threshold=5.0, level="warn")

    assert verdict.verdict == VERDICT_PASSED
    assert verdict.warnings and verdict.warnings[0]["metricKey"] == leak.SLOPE_METRIC_KEY


def test_leak_verdict_not_tested_when_samples_insufficient():
    analysis = leak.analyze_leak_trend(
        [_run("r0", _day(0), 100.0), _run("r1", _day(1), 120.0)]
    )

    verdict = leak.leak_verdict(analysis, threshold=5.0)

    assert verdict.verdict == VERDICT_NOT_TESTED
    assert "样本" in verdict.reason


def test_leak_verdict_low_confidence_still_judged_and_marked():
    runs = [
        _run("r0", "2026-10-20T10:00:00+08:00", 100.0),
        _run("r1", "2026-10-20T11:00:00+08:00", 101.0),
        _run("r2", "2026-10-20T12:00:00+08:00", 102.0),
    ]

    verdict = leak.leak_verdict(leak.analyze_leak_trend(runs), threshold=10.0)

    assert verdict.verdict == VERDICT_FAILED
    assert verdict.warnings  # 低置信度如实进 warnings，不拒绝判定
    assert verdict.checks[0]["confidence"] == leak.CONFIDENCE_LOW


# --- kit 门面：跨 profile 语义与 trend 完全一致 ---------------------------


def _seed_leak_runs(
    store: PerfStore,
    target: str,
    entries: list[tuple[str, str, str | None, str, float]],
) -> None:
    for run_id, started_at, profile, metric_key, median in entries:
        store.record_session(
            {
                "run_id": run_id,
                "target": target,
                "device_profile": profile,
                "serial": "749622c9",
                "started_at": started_at,
                "duration_s": 30.0,
            }
        )
        store.record_metrics(
            run_id,
            [
                {
                    "metric_key": metric_key,
                    "unit": "MB",
                    "source": "solo_pi",
                    "min": median,
                    "median": median,
                    "mean": median,
                    "max": median,
                    "p90": median,
                    "sample_count": 10,
                }
            ],
        )


def _make_kit(tmp_path, store: PerfStore):
    """只读门面：不触设备的 PerfKit（leak_trend 只读 store）。"""
    adb = tmp_path / "adb.exe"
    adb.write_bytes(b"x")
    return kit_module.PerfKit(
        adb_path=str(adb),
        serial="749622c9",
        store=store,
        runs_dir=tmp_path / "runs",
    )


def test_leak_trend_returns_series_slope_and_labels(tmp_path):
    store = PerfStore(tmp_path / "data" / "mobile_perf.db")
    _seed_leak_runs(
        store,
        "com.sinosoft.trainapp",
        [
            ("r0", _day(0), "Xiaomi-2206123SC-Android14", "mem_pss_mb", 100.0),
            ("r1", _day(1), "Xiaomi-2206123SC-Android14", "mem_pss_mb", 110.0),
            ("r2", _day(2), "Xiaomi-2206123SC-Android14", "mem_pss_mb", 120.0),
        ],
    )
    kit = _make_kit(tmp_path, store)

    payload = kit.leak_trend("com.sinosoft.trainapp")

    assert payload["pkg"] == "com.sinosoft.trainapp"
    assert payload["metric"] == "mem_pss_mb"
    assert payload["slopeMetricKey"] == leak.SLOPE_METRIC_KEY
    assert payload["unit"] == "MB/day"
    assert payload["labelZh"] == "应用PSS内存增长斜率"
    assert payload["labelEn"] == "Process PSS memory growth slope"
    assert payload["slopeMbPerDay"] == pytest.approx(10.0)
    assert payload["runCount"] == 3
    assert payload["confidence"] == leak.CONFIDENCE_OK
    assert [item["runId"] for item in payload["runs"]] == ["r0", "r1", "r2"]
    assert [item["median"] for item in payload["runs"]] == [100.0, 110.0, 120.0]
    assert payload["runs"][0]["startedAt"] == _day(0)
    assert payload["runs"][0]["durationS"] == 30.0


def test_leak_trend_rejects_cross_profile_without_explicit_profile(tmp_path):
    store = PerfStore(tmp_path / "data" / "mobile_perf.db")
    _seed_leak_runs(
        store,
        "com.sinosoft.trainapp",
        [
            ("r0", _day(0), "PICO4-Android12", "mem_pss_mb", 100.0),
            ("r1", _day(1), "Mate60-Harmony4", "mem_pss_mb", 110.0),
        ],
    )
    kit = _make_kit(tmp_path, store)

    with pytest.raises(kit_module.ProfileMismatchError) as excinfo:
        kit.leak_trend("com.sinosoft.trainapp")
    assert "PICO4-Android12" in str(excinfo.value)
    assert "Mate60-Harmony4" in str(excinfo.value)


def test_leak_trend_explicit_profile_marks_caliber_mismatch(tmp_path):
    store = PerfStore(tmp_path / "data" / "mobile_perf.db")
    _seed_leak_runs(
        store,
        "com.sinosoft.trainapp",
        [
            ("r0", _day(0), "PICO4-Android12", "mem_pss_mb", 100.0),
            ("r1", _day(1), "PICO4-Android12", "mem_pss_mb", 110.0),
            ("r2", _day(2), "Mate60-Harmony4", "mem_pss_mb", 999.0),
        ],
    )
    kit = _make_kit(tmp_path, store)

    payload = kit.leak_trend("com.sinosoft.trainapp", profile="PICO4-Android12")

    assert payload["profileMismatch"] is True
    assert [item["runId"] for item in payload["runs"]] == ["r0", "r1"]
    assert payload["runCount"] == 2
    assert payload["confidence"] == leak.CONFIDENCE_INSUFFICIENT


def test_leak_trend_raises_for_unknown_package_or_missing_metric(tmp_path):
    store = PerfStore(tmp_path / "data" / "mobile_perf.db")
    _seed_leak_runs(
        store,
        "com.sinosoft.trainapp",
        [("r0", _day(0), None, "cpu_process_pct", 5.0)],
    )
    kit = _make_kit(tmp_path, store)

    with pytest.raises(kit_module.TrendError, match="no-such-pkg"):
        kit.leak_trend("no-such-pkg")
    with pytest.raises(kit_module.TrendError, match="mem_pss_mb"):
        kit.leak_trend("com.sinosoft.trainapp")


def test_leak_trend_payload_includes_verdict_when_threshold_given(tmp_path):
    store = PerfStore(tmp_path / "data" / "mobile_perf.db")
    _seed_leak_runs(
        store,
        "com.sinosoft.trainapp",
        [
            ("r0", _day(0), "Xiaomi-2206123SC-Android14", "mem_pss_mb", 100.0),
            ("r1", _day(1), "Xiaomi-2206123SC-Android14", "mem_pss_mb", 110.0),
            ("r2", _day(2), "Xiaomi-2206123SC-Android14", "mem_pss_mb", 120.0),
        ],
    )
    kit = _make_kit(tmp_path, store)

    payload = kit.leak_trend("com.sinosoft.trainapp", slope_threshold=5.0)

    assert payload["verdict"]["verdict"] == VERDICT_FAILED
    assert payload["verdict"]["checks"][0]["slopeMbPerDay"] == pytest.approx(10.0)
