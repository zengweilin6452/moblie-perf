"""HAR 1.2 写入链路（纯标准库）：结构、脱敏、正文口径、限额与原子合成。

全部使用替身 flow 对象，不 import mitmproxy。
"""

import json
from types import SimpleNamespace

from mobile_perf import har_writer


class FakeHeaders:
    """保留重复头的 headers 替身（mitmproxy Headers 的 items(multi=True) 同形）。"""

    def __init__(self, pairs):
        self._pairs = list(pairs)

    def items(self, multi=False):
        return list(self._pairs)


def _flow(
    *,
    url="https://api.test/v1/list?page=2&token=PROBE_SECRET",
    request_headers=None,
    response_headers=None,
    request_body=b"",
    response_body=b'{"ok":true}',
    status=200,
):
    request = SimpleNamespace(
        method="GET",
        url=url,
        headers=FakeHeaders(
            request_headers
            if request_headers is not None
            else [
                ("Host", "api.test"),
                ("Authorization", "Bearer PROBE_SECRET"),
                ("Cookie", "sid=PROBE_SECRET; theme=dark"),
            ]
        ),
        timestamp_start=100.0,
        raw_content=request_body,
    )
    response = SimpleNamespace(
        status_code=status,
        headers=FakeHeaders(
            response_headers
            if response_headers is not None
            else [
                ("Content-Type", "application/json"),
                ("Set-Cookie", "session=PROBE_SECRET; Path=/; HttpOnly"),
            ]
        ),
        timestamp_end=100.25,
        raw_content=response_body,
    )
    return SimpleNamespace(request=request, response=response, error=None)


def test_build_entry_has_har_12_shape_and_timings_sum_to_time():
    entry = har_writer.build_entry(_flow(), har_writer.CaptureOptions(enabled=True))

    assert set(entry) >= {"startedDateTime", "time", "request", "response", "timings"}
    assert entry["time"] == sum(entry["timings"].values())
    assert entry["timings"]["wait"] == 250.0
    assert entry["request"]["method"] == "GET"
    assert entry["request"]["headersSize"] == -1
    assert entry["response"]["headersSize"] == -1
    assert entry["response"]["status"] == 200
    assert entry["startedDateTime"].endswith("Z")


def test_build_entry_redacts_headers_cookies_query_and_userinfo():
    flow = _flow(
        url="https://user:PROBE_SECRET@api.test/v1/list?page=2&token=PROBE_SECRET&x=1"
    )

    entry = har_writer.build_entry(flow, har_writer.CaptureOptions(enabled=True))
    dumped = json.dumps(entry, ensure_ascii=False)

    assert "PROBE_SECRET" not in dumped
    assert "user" not in entry["request"]["url"].split("@", 1)[0]
    assert entry["request"]["url"].startswith("https://REDACTED:REDACTED@api.test/")
    headers = {item["name"].lower(): item["value"] for item in entry["request"]["headers"]}
    assert headers["authorization"] == har_writer.REDACTED
    assert headers["cookie"] == har_writer.REDACTED
    assert {item["name"] for item in entry["request"]["cookies"]} == {"sid", "theme"}
    assert all(item["value"] == har_writer.REDACTED for item in entry["request"]["cookies"])
    query = {item["name"]: item["value"] for item in entry["request"]["queryString"]}
    assert query["page"] == "2"
    assert query["token"] == har_writer.REDACTED
    assert "token=[REDACTED]" in entry["request"]["url"]
    set_cookie = entry["response"]["cookies"][0]
    assert set_cookie == {"name": "session", "value": har_writer.REDACTED}


def test_url_valued_headers_are_scrubbed_without_losing_header_names():
    entry = har_writer.build_entry(
        _flow(
            request_headers=[
                ("Referer", "https://api.test/came?token=PROBE_SECRET&page=2"),
                ("X-Trace-Id", "trace-42"),
            ],
            response_headers=[
                ("Location", "https://api.test/next?token=PROBE_SECRET"),
                ("Content-Location", "https://api.test/self?code=PROBE_SECRET"),
            ],
        ),
        har_writer.CaptureOptions(enabled=True),
    )
    dumped = json.dumps(entry, ensure_ascii=False)

    assert "PROBE_SECRET" not in dumped
    request_headers = {item["name"]: item["value"] for item in entry["request"]["headers"]}
    response_headers = {item["name"]: item["value"] for item in entry["response"]["headers"]}
    assert "token=[REDACTED]" in request_headers["Referer"]
    assert "page=2" in request_headers["Referer"]
    assert "token=[REDACTED]" in response_headers["Location"]
    assert "code=[REDACTED]" in response_headers["Content-Location"]
    assert "token=[REDACTED]" in entry["response"]["redirectURL"]


def test_default_metadata_mode_records_size_mime_and_sha256_without_body():
    entry = har_writer.build_entry(_flow(), har_writer.CaptureOptions(enabled=True))

    content = entry["response"]["content"]
    assert content["size"] == len(b'{"ok":true}')
    assert content["mimeType"] == "application/json"
    assert len(content["sha256"]) == 64
    assert "text" not in content


def test_large_body_skips_sha256_and_marks_it():
    options = har_writer.CaptureOptions(enabled=True, max_hash_bytes=4)

    entry = har_writer.build_entry(_flow(response_body=b"0123456789"), options)

    content = entry["response"]["content"]
    assert content["size"] == 10
    assert "sha256" not in content
    assert content["_sha256Skipped"] is True


def test_bodies_enabled_truncates_and_marks_text():
    options = har_writer.CaptureOptions(
        enabled=True, capture_bodies=True, body_limit_bytes=5
    )

    entry = har_writer.build_entry(_flow(response_body=b"0123456789"), options)

    content = entry["response"]["content"]
    assert content["text"] == "01234"
    assert content["_truncated"] is True


def test_binary_body_is_base64_when_bodies_enabled():
    options = har_writer.CaptureOptions(enabled=True, capture_bodies=True)

    entry = har_writer.build_entry(_flow(response_body=b"\xff\x00\xfe"), options)

    content = entry["response"]["content"]
    assert content["encoding"] == "base64"
    assert content["text"] == "/wD+"


def test_binary_mime_forces_base64_even_for_ascii_bytes():
    options = har_writer.CaptureOptions(enabled=True, capture_bodies=True)
    flow = _flow(
        response_headers=[("Content-Type", "application/octet-stream")],
        response_body=b"hello",
    )

    content = har_writer.build_entry(flow, options)["response"]["content"]

    assert content["encoding"] == "base64"
    assert content["text"] == "aGVsbG8="


def test_error_flow_entry_has_status_zero_and_error_text():
    flow = SimpleNamespace(
        request=_flow().request,
        response=None,
        error="connection reset by peer",
    )

    entry = har_writer.build_entry(flow, har_writer.CaptureOptions(enabled=True))

    assert entry["response"]["status"] == 0
    assert entry["_error"] == "connection reset by peer"
    assert entry["time"] == sum(entry["timings"].values())


def test_writer_enforces_max_entries_and_reports_reason(tmp_path):
    path = tmp_path / "entries.jsonl"
    dropped = []
    writer = har_writer.HarWriter(
        path, max_entries=2, on_drop=lambda reason: dropped.append(reason)
    )

    results = [writer.write({"n": index}) for index in range(3)]
    writer.close()

    assert results == [True, True, False]
    assert writer.entries == 2
    assert writer.dropped_entries == 1
    assert writer.truncated_reason == "max_entries"
    assert dropped == ["max_entries"]
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_writer_enforces_max_bytes_without_raising(tmp_path):
    writer = har_writer.HarWriter(tmp_path / "entries.jsonl", max_bytes=10)

    assert writer.write({"payload": "x" * 100}) is False
    writer.close()

    assert writer.dropped_entries == 1
    assert writer.truncated_reason == "max_bytes"


def test_writer_swallows_write_errors_and_counts_them(tmp_path):
    errors = []
    writer = har_writer.HarWriter(
        tmp_path / "entries.jsonl", on_error=lambda message: errors.append(message)
    )
    writer._handle.close()  # 模拟底层句柄失效

    assert writer.write({"n": 1}) is False
    writer.close()

    assert writer.writer_errors == 1
    assert errors


def test_compose_skips_bad_lines_and_writes_valid_har(tmp_path):
    entries_path = tmp_path / "app.har.entries.jsonl"
    har_path = tmp_path / "app.har"
    entry = {"time": 1.0, "request": {}, "response": {}, "timings": {}}
    entries_path.write_text(
        "not json\n" + json.dumps(entry) + "\n" + json.dumps(["not", "dict"]) + "\n",
        encoding="utf-8",
    )

    summary = har_writer.compose_har(
        entries_path,
        har_path,
        meta={"status": har_writer.STATUS_COMPLETE, "bodyMode": "metadata"},
    )

    payload = json.loads(har_path.read_text(encoding="utf-8"))
    assert payload["log"]["version"] == "1.2"
    assert payload["log"]["creator"]["name"] == "mobile_perf"
    assert payload["log"]["pages"] == []
    assert payload["log"]["entries"] == [entry]
    assert summary.entries == 1
    assert summary.skipped_entries == 2
    assert summary.status == "complete"
    assert summary.size_bytes == har_path.stat().st_size
    assert not (tmp_path / "app.har.tmp").exists()


def test_compose_without_valid_entries_creates_no_har(tmp_path):
    entries_path = tmp_path / "app.har.entries.jsonl"
    har_path = tmp_path / "app.har"
    entries_path.write_text("bad line\n{} extra\n", encoding="utf-8")

    summary = har_writer.compose_har(
        entries_path,
        har_path,
        meta={"status": har_writer.STATUS_NOT_TESTED, "notTestedReason": "零 HTTP 请求"},
    )

    assert not har_path.exists()
    assert summary.entries == 0
    assert summary.path is None
    assert summary.status == "not_tested"
    assert summary.not_tested_reason == "零 HTTP 请求"
    assert not (tmp_path / "app.har.tmp").exists()


def test_compose_missing_entries_file_reports_not_tested_without_file(tmp_path):
    summary = har_writer.compose_har(
        tmp_path / "absent.jsonl",
        tmp_path / "app.har",
        meta={"status": har_writer.STATUS_NOT_TESTED},
    )

    assert summary.entries == 0
    assert summary.path is None
    assert not (tmp_path / "app.har").exists()


def test_har_summary_supports_all_fixed_statuses():
    assert set(har_writer.HAR_STATUSES) == {
        "not_enabled",
        "not_tested",
        "complete",
        "partial",
        "failed",
    }
    for status in har_writer.HAR_STATUSES:
        assert har_writer.HarSummary(status=status).status == status


def test_har_summary_dict_covers_all_contract_fields():
    summary = har_writer.HarSummary(
        enabled=True,
        status=har_writer.STATUS_PARTIAL,
        path="runs/x/network/app.har",
        entries_path="runs/x/network/app.har.entries.jsonl",
        entries=3,
        size_bytes=100,
        body_mode="metadata",
        redacted=True,
        truncated=True,
        truncated_reason="max_entries",
        dropped_entries=2,
        skipped_entries=1,
        writer_errors=0,
        https_coverage="none",
        not_tested_reason="HTTPS 未解密",
    )

    payload = summary.as_dict()

    assert set(payload) == {
        "enabled",
        "status",
        "path",
        "entriesPath",
        "entries",
        "sizeBytes",
        "bodyMode",
        "redacted",
        "truncated",
        "truncatedReason",
        "droppedEntries",
        "skippedEntries",
        "writerErrors",
        "httpsCoverage",
        "notTestedReason",
    }
    assert payload["status"] == "partial"
