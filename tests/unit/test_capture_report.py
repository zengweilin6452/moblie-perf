"""报告与 HAR 摘要：含 har 的渲染、旧 run 零新增、相对链接与重组重放。"""

import json

from mobile_perf import report
from mobile_perf.store import PerfStore


def _meta():
    return {
        "runId": "run-har-report",
        "target": "tv.danmaku.bili",
        "items": ["CPU"],
        "serial": "749622c9",
        "deviceProfile": "Xiaomi-Android14",
        "hostRunId": "run-har-report",
        "startedAt": "2026-11-07T00:00:00",
        "stoppedAt": "2026-11-07T00:00:30",
        "durationS": 30.0,
    }


def _record():
    return {"sessionId": "sid-1", "marks": []}


def _network(har=None):
    payload = {
        "profile": {"name": "weak", "latencyMs": 400.0, "downKbps": 400.0,
                    "upKbps": 200.0, "lossPct": 2.0},
        "tier": "decrypt",
        "applied": True,
        "effective": True,
        "evidence": "observed",
        "trust": "trusted",
        "proxy": {"connections": 1, "hosts": ["api.test"], "errors": 0},
        "setupErrors": [],
        "restored": {"deviceProxy": True, "reverse": True, "proxyProcess": True},
    }
    if har is not None:
        payload["har"] = har
    return payload


def _har(**overrides):
    payload = {
        "enabled": True,
        "status": "complete",
        "path": "/repo/runs/run-har-report/network/app.har",
        "entriesPath": "/repo/runs/run-har-report/network/app.har.entries.jsonl",
        "entries": 12,
        "sizeBytes": 4096,
        "bodyMode": "metadata",
        "redacted": True,
        "truncated": False,
        "truncatedReason": None,
        "droppedEntries": 0,
        "skippedEntries": 0,
        "writerErrors": 0,
        "httpsCoverage": "full",
        "notTestedReason": None,
    }
    payload.update(overrides)
    return payload


def test_html_renders_har_summary_and_relative_link(tmp_path):
    built = report.build_report(
        meta=_meta(), record=_record(), artifacts=[], network=_network(_har())
    )

    html = report.write_html(tmp_path, built).read_text(encoding="utf-8")

    assert built["network"]["har"]["entries"] == 12
    assert "HAR / HAR" in html
    assert "完整 / complete" in html
    assert 'href="network/app.har"' in html
    assert "仅元数据 / metadata" in html
    assert "是 / yes" in html
    assert "完整 / full" in html


def test_partial_har_shows_https_gap_redaction_warning_and_truncation(tmp_path):
    built = report.build_report(
        meta=_meta(),
        record=_record(),
        artifacts=[],
        network=_network(
            _har(
                status="partial",
                redacted=False,
                truncated=True,
                truncatedReason="max_entries",
                httpsCoverage="none",
                notTestedReason="HTTPS 未解密：App 不信任代理 CA",
            )
        ),
    )

    html = report.write_html(tmp_path, built).read_text(encoding="utf-8")

    assert "部分 / partial" in html
    assert "⚠️ 否 / no（危险" in html
    assert "max_entries" in html
    assert "无（仅明文 HTTP）/ none" in html
    assert "HTTPS 未解密" in html


def test_network_markup_without_har_is_unchanged():
    built = report.build_report(
        meta=_meta(), record=_record(), artifacts=[], network=_network()
    )

    markup = report._network_markup(built)

    assert "HAR" not in markup
    assert "network/app.har" not in markup
    assert "网络剖面" in markup


def test_assemble_run_report_replays_har_summary_from_meta(tmp_path):
    run_id = "run-har-report"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                **_meta(),
                "runId": run_id,
                "network": _network(_har()),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = PerfStore(tmp_path / "data" / "mobile_perf.db")
    store.record_session(
        {"run_id": run_id, "target": "tv.danmaku.bili", "run_dir": str(run_dir)}
    )

    built = report.assemble_run_report(run_id, runs_dir=tmp_path / "runs", store=store)

    assert built["network"]["har"]["status"] == "complete"
    assert built["network"]["har"]["entries"] == 12


def test_har_summary_contains_no_body_or_secret_values():
    """正文里真实存在的敏感串不会出现在摘要/报告里（不是空断言）。"""
    from types import SimpleNamespace

    from mobile_perf import har_writer

    flow = SimpleNamespace(
        request=SimpleNamespace(
            method="POST",
            url="https://api.test/a?token=PROBE_SECRET",
            headers=[("Content-Type", "text/plain")],
            timestamp_start=100.0,
            raw_content=b"PROBE_SECRET",
        ),
        response=SimpleNamespace(
            status_code=200,
            headers=[("Content-Type", "text/plain")],
            timestamp_end=100.1,
            raw_content=b"PROBE_SECRET",
        ),
        error=None,
    )
    entry = har_writer.build_entry(
        flow, har_writer.CaptureOptions(enabled=True, capture_bodies=True)
    )
    # 前提：显式抓正文时 entry 里确实有敏感串；摘要与报告不得携带它
    assert entry["response"]["content"]["text"] == "PROBE_SECRET"

    summary = har_writer.HarSummary(
        enabled=True,
        status=har_writer.STATUS_COMPLETE,
        path="/repo/runs/run-har-report/network/app.har",
        entries=1,
        body_mode="full",
    ).as_dict()
    built = report.build_report(
        meta=_meta(), record=_record(), artifacts=[], network=_network(summary)
    )

    dumped = json.dumps(built, ensure_ascii=False)

    assert "PROBE_SECRET" not in dumped
    assert "text" not in built["network"]["har"]
    assert "request" not in built["network"]["har"]
