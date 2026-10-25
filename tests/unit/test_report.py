"""report 骨架的单元测试：报告聚合与落盘（M3 补齐图表数据）。"""

import json

from mobile_perf import report


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


def test_write_report_round_trip(tmp_path):
    built = report.build_report(meta=_meta(), record=_record(), artifacts=[])

    path = report.write_report(tmp_path, built)

    assert path == tmp_path / "report.json"
    assert json.loads(path.read_text(encoding="utf-8"))["runId"] == "run-x"
