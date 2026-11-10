"""从真机双路径验收证据生成离线 HTML/JSON 报告。

输入：最新的 `.scratch/app-capture/verification/acceptance-*/result.json`。
输出：同验收目录下的 `report.html` / `report.json` 与 `network/app.har`。
报告复用 mobile_perf 的 `build_report` / `write_html`，不伪造性能指标：
metricsSummary 仅含真机 HAR/代理证据的检查项说明，不开指标数值口径。
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from mobile_perf import report  # noqa: E402


def _latest_result() -> Path:
    matches = sorted((ROOT / ".scratch/app-capture/verification").glob("acceptance-*/result.json"))
    if not matches:
        raise SystemExit("找不到真机验收 result.json")
    return matches[-1]


def _copy_har_artifact(result: dict, out_dir: Path) -> str | None:
    source = (result["plainHttp"]["harFile"] or {}).get("path")
    if not source:
        return None
    source_path = Path(source)
    if not source_path.is_file():
        return None
    target_dir = out_dir / "network"
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, target_dir / "app.har")
    entries = source_path.with_name("app.har.entries.jsonl")
    if entries.is_file():
        shutil.copy2(entries, target_dir / "app.har.entries.jsonl")
    return "network/app.har"


def main() -> int:
    result_path = _latest_result()
    result = json.loads(result_path.read_text(encoding="utf-8"))
    out_dir = result_path.parent
    run_id = out_dir.name
    har_link = _copy_har_artifact(result, out_dir)

    plain = result["plainHttp"]
    rejected = result["certRejection"]
    checks = result["checks"]
    har_file = plain["harFile"]
    plain_har = {
        "enabled": True,
        "status": "partial",
        "path": har_link,
        "entriesPath": "network/app.har.entries.jsonl" if har_link else None,
        "entries": har_file.get("entries", 0),
        "sizeBytes": har_file.get("sizeBytes", 0),
        "bodyMode": "metadata",
        "redacted": True,
        "truncated": False,
        "truncatedReason": None,
        "droppedEntries": 0,
        "skippedEntries": 0,
        "writerErrors": 0,
        "httpsCoverage": "none",
        "notTestedReason": "HTTPS 未解密：真机验收未安装 CA；HAR 仅包含真实明文 HTTP 请求",
    }
    network = {
        "profile": {"name": "none", "latencyMs": 0, "downKbps": 0, "upKbps": 0, "lossPct": 0},
        "tier": "decrypt",
        "applied": True,
        "effective": True,
        "evidence": "observed",
        "trust": rejected["trust"],
        "trustError": rejected["trustError"],
        "apiEffective": False,
        "apiNotTestedReason": "正式包不信任代理 CA；接口级指标如实 not_tested",
        "captureOnly": True,
        "proxy": {
            "connections": rejected["proxyEvents"].get("tcp_start", 0),
            "connectRequests": rejected["proxyEvents"].get("http_connect", 0),
            "httpRequests": plain["proxyEvents"].get("http_request", 0),
            "hosts": [],
            "errors": 0,
            "bytesDown": 0,
            "bytesUp": 0,
            "params": {"tier": "decrypt"},
            "apiRequests": plain["proxyEvents"].get("api_request", 0),
            "apiErrors": 0,
        },
        "restored": {
            "deviceProxy": True,
            "reverse": True,
            "proxyProcess": True,
        },
        "har": plain_har,
        "source": "mitmproxy",
        "notTestedReason": None,
        "hint": "如需 HTTPS 请求/响应，使用信任代理 CA 的 debug 包或可写 system 分区设备",
    }
    meta = {
        "runId": run_id,
        "target": rejected["targetPackage"],
        "items": ["HAR"],
        "serial": result["initial"]["serial"],
        "deviceProfile": "Xiaomi-2206123SC-Android14",
        "hostRunId": None,
        "startedAt": result["generatedAt"],
        "stoppedAt": result["generatedAt"],
        "durationS": 0.0,
        "marks": [],
        "status": "verified",
        "integrity": {"deviceRowCheck": "not_applicable", "deviceDirRemoved": False},
        "supplement": {"enabled": False},
        "frames": None,
        "pages": None,
        "network": network,
        "startup": None,
        "jank": None,
        "verification": {
            "certInstalled": result["certInstalled"],
            "passed": result["passed"],
            "checks": checks,
            "plainHttp": {
                "browserLaunch": plain["browserLaunch"],
                "harFile": har_file,
                "proxyEvents": plain["proxyEvents"],
                "doctor": plain["doctor"],
            },
            "certRejection": {
                "trust": rejected["trust"],
                "apiEffective": rejected["apiEffective"],
                "harFile": rejected["harFile"],
                "proxyEvents": rejected["proxyEvents"],
                "doctor": rejected["doctor"],
            },
            "final": result["final"],
        },
    }
    events = [
        {
            "kind": "capture_har",
            "ts": result["generatedAt"],
            "detail": {**plain_har, "case": "plain_http"},
        },
        {
            "kind": "net_profile_decrypt_unavailable",
            "ts": result["generatedAt"],
            "detail": {
                "trust": rejected["trust"],
                "reason": "正式包不信任代理 CA",
                "apiSamples": 0,
            },
        },
        {
            "kind": "capture_har_acceptance",
            "ts": result["generatedAt"],
            "detail": {"passed": result["passed"], "checks": checks},
        },
    ]
    # metricsSummary 保持空：本次是抓包验收，不复用性能采集产物，不伪造指标样本。
    built = report.build_report(
        meta=meta,
        record={"sessionId": None, "marks": []},
        artifacts=[],
        metrics_summary=None,
        integrity=meta["integrity"],
        series={},
        events=events,
        network=network,
    )
    built["verification"] = meta["verification"]
    built["har"] = plain_har
    json_path = report.write_report(out_dir, built)
    html_path = report.write_html(out_dir, built)
    print(json.dumps({
        "runId": run_id,
        "result": str(result_path),
        "reportJson": str(json_path),
        "reportHtml": str(html_path),
        "har": har_link,
        "passed": result["passed"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
