"""APP 抓包产物（HAR 1.2）：脱敏、条目构造、流式 JSONL 与原子合成。

本模块**只用标准库**，主进程与 mitmproxy addon 子进程都能加载。设计要点：

1. **默认脱敏、默认只记元数据**：HAR 含业务敏感信息，必须显式开启抓包；
   头、Cookie、URL 查询参数与 userinfo 默认替换为 ``[REDACTED]``；默认不写
   请求/响应正文，只记大小、MIME 与 sha256，正文需显式开启并按单条上限截断。
2. **Windows 耐久**：addon 逐行 append 已脱敏 entry 到 ``.jsonl`` 并 flush；
   主进程在代理进程终止后流式读取 JSONL、跳过坏行，写同目录 ``.tmp`` 后
   ``os.replace`` 原子落盘。不依赖 mitmproxy 的 ``done()``/``hardump``
   （Windows ``Popen.terminate()`` 不会执行它们）。
3. **不伪造**：``headersSize=-1``；``bodySize`` 记真实长度；``time`` 是
   timings 之和；不写 CONNECT 伪请求。
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote_plus, urlsplit, urlunsplit

REDACTED = "[REDACTED]"

# 默认脱敏头名单（大小写不敏感）
SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
        "x-auth-token",
        "x-access-token",
        "x-refresh-token",
        "x-csrf-token",
        "x-xsrf-token",
        "x-session-id",
        "token",
        "signature",
    }
)

# 默认脱敏查询参数名单（大小写不敏感）
SENSITIVE_QUERY_KEYS = frozenset(
    {
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "auth",
        "authorization",
        "session",
        "sessionid",
        "sid",
        "password",
        "passwd",
        "pwd",
        "secret",
        "api_key",
        "apikey",
        "key",
        "client_secret",
        "sign",
        "signature",
        "sig",
        "code",
        "ticket",
        "nonce",
        "state",
    }
)

DEFAULT_BODY_LIMIT_BYTES = 65536
DEFAULT_MAX_ENTRIES = 5000
DEFAULT_MAX_BYTES = 67108864  # 64 MiB
DEFAULT_MAX_HASH_BYTES = 8 * 1024 * 1024  # 8 MiB

# writer 旁路状态文件后缀（与 entries 同目录）：addon 进程被硬 terminate 后，
# 节流日志恢复不了节流窗口内（1~99 条）的精确计数，这份 O(1) 大小的状态文件
# 由常驻句柄在每次丢弃/写失败时就地重写并 flush，保存最终精确值；
# compose 侧优先读它（P0-03/P0-04）
STATE_SUFFIX = ".state.json"


def state_path_for(entries_path: str | os.PathLike[str]) -> Path:
    """entries JSONL 对应的 writer 状态文件路径（单一推导规则，双方共用）。"""
    return Path(str(entries_path) + STATE_SUFFIX)


def read_writer_state(entries_path: str | os.PathLike[str]) -> dict[str, Any] | None:
    """读取 writer 状态文件的精确计数；缺失/损坏返回 None（调用方退回日志推算）。"""
    try:
        raw = state_path_for(entries_path).read_text(encoding="utf-8")
        data = json.loads(raw)
        return {
            "droppedEntries": int(data["droppedEntries"]),
            "writerErrors": int(data["writerErrors"]),
            "truncatedReason": data.get("truncatedReason"),
        }
    except (OSError, ValueError, KeyError, TypeError):
        return None

# HAR 摘要状态（固定五态）
STATUS_NOT_ENABLED = "not_enabled"
STATUS_NOT_TESTED = "not_tested"
STATUS_COMPLETE = "complete"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
HAR_STATUSES = (
    STATUS_NOT_ENABLED,
    STATUS_NOT_TESTED,
    STATUS_COMPLETE,
    STATUS_PARTIAL,
    STATUS_FAILED,
)


@dataclass(frozen=True)
class CaptureOptions:
    """抓包选项：限额必须为正整数，非法值早失败（不带着错配置跑采集）。"""

    enabled: bool = False
    capture_bodies: bool = False
    body_limit_bytes: int = DEFAULT_BODY_LIMIT_BYTES
    max_entries: int = DEFAULT_MAX_ENTRIES
    max_bytes: int = DEFAULT_MAX_BYTES
    redact: bool = True
    max_hash_bytes: int = DEFAULT_MAX_HASH_BYTES

    def __post_init__(self) -> None:
        for name in ("body_limit_bytes", "max_entries", "max_bytes", "max_hash_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} 必须为正整数，收到 {value!r}")

    @property
    def body_mode(self) -> str:
        return "full" if self.capture_bodies else "metadata"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HarSummary:
    """HAR 产物摘要：可直接挂进 ``NetSession.as_dict()["har"]``。"""

    enabled: bool = False
    status: str = STATUS_NOT_ENABLED
    path: str | None = None
    entries_path: str | None = None
    entries: int = 0
    size_bytes: int = 0
    body_mode: str = "metadata"
    redacted: bool = True
    truncated: bool = False
    truncated_reason: str | None = None
    dropped_entries: int = 0
    # 日志事件数（节流观测）：har_dropped 只在首次与每累计 100 条时落事件，
    # 与 droppedEntries（真实条目数）不是同一个口径（P0-03）
    dropped_events: int = 0
    writer_errors: int = 0
    # 计数来源：writer_state=旁路状态文件的精确值；proxy_log=节流日志推算（下界）
    counters_source: str = "proxy_log"
    skipped_entries: int = 0
    https_coverage: str = "unknown"
    not_tested_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "status": self.status,
            "path": self.path,
            "entriesPath": self.entries_path,
            "entries": self.entries,
            "sizeBytes": self.size_bytes,
            "bodyMode": self.body_mode,
            "redacted": self.redacted,
            "truncated": self.truncated,
            "truncatedReason": self.truncated_reason,
            "droppedEntries": self.dropped_entries,
            "droppedEvents": self.dropped_events,
            "countersSource": self.counters_source,
            "skippedEntries": self.skipped_entries,
            "writerErrors": self.writer_errors,
            "httpsCoverage": self.https_coverage,
            "notTestedReason": self.not_tested_reason,
        }


# ---------- 脱敏 ----------


def _header_pairs(headers: Any) -> list[tuple[str, str]]:
    """把 headers 归一成有序 (name, value) 列表，保留重复头。"""
    if headers is None:
        return []
    if isinstance(headers, (list, tuple)):
        return [(str(name), str(value)) for name, value in headers]
    items = getattr(headers, "items", None)
    if items is None:
        return []
    try:
        return [(str(name), str(value)) for name, value in headers.items(multi=True)]
    except TypeError:
        return [(str(name), str(value)) for name, value in headers.items()]


def _cookie_pairs(value: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for chunk in str(value).split(";"):
        name, sep, item = chunk.strip().partition("=")
        if sep and name.strip():
            pairs.append((name.strip(), item.strip()))
    return pairs


def _scrub_cookies(cookies: list[dict[str, str]], redact: bool) -> list[dict[str, str]]:
    if not redact:
        return cookies
    return [{"name": item["name"], "value": REDACTED} for item in cookies]


def _cookies_from_headers(pairs: list[tuple[str, str]], header_name: str) -> list[dict[str, str]]:
    cookies: list[dict[str, str]] = []
    for name, value in pairs:
        if name.lower() != header_name:
            continue
        if header_name == "set-cookie":
            first = value.split(";", 1)[0]
            cookies.extend({"name": key, "value": item} for key, item in _cookie_pairs(first))
        else:
            cookies.extend({"name": key, "value": item} for key, item in _cookie_pairs(value))
    return cookies


_URL_HEADERS = frozenset({"location", "content-location", "referer", "refresh"})


def scrub_headers(
    pairs: list[tuple[str, str]], *, redact: bool = True
) -> list[dict[str, str]]:
    """头脱敏：敏感头值替换；URL 型头再跑一次 URL 脱敏，头名保留。"""
    cleaned: list[dict[str, str]] = []
    for name, value in pairs:
        lowered = name.lower()
        if redact and lowered in SENSITIVE_HEADERS:
            value = REDACTED
        elif redact and lowered in _URL_HEADERS:
            value = scrub_url(value, redact=True)
        cleaned.append({"name": name, "value": value})
    return cleaned


def _kv_segments(text: str) -> list[tuple[str, str, str]]:
    """(原始名段, 分隔符, 原始值段) 列表；保留顺序与重复项（查询串/fragment 通用）。"""
    segments: list[tuple[str, str, str]] = []
    for chunk in text.split("&"):
        if not chunk:
            continue
        name, sep, value = chunk.partition("=")
        segments.append((name, sep, value))
    return segments


def _redact_kv(text: str) -> str:
    """按查询参数口径脱敏 key=value&... 串；非敏感项与无值段原样保留。"""
    return "&".join(
        f"{name}={REDACTED}"
        if sep and unquote_plus(name).lower() in SENSITIVE_QUERY_KEYS
        else f"{name}{sep}{value}"
        for name, sep, value in _kv_segments(text)
    )


def _query_segments(url: str) -> list[tuple[str, str, str]]:
    """(原始名段, 分隔符, 原始值段) 列表；保留顺序与重复项。"""
    return _kv_segments(urlsplit(url).query)


def _query_string(url: str, *, redact: bool = True) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for raw_name, sep, raw_value in _query_segments(url):
        name = unquote_plus(raw_name)
        value = unquote_plus(raw_value) if sep else ""
        if redact and name.lower() in SENSITIVE_QUERY_KEYS:
            value = REDACTED
        result.append({"name": name, "value": value})
    return result


def scrub_url(url: str, *, redact: bool = True) -> str:
    """URL 脱敏：userinfo、敏感查询参数与敏感 fragment 参数替换，其余原样。

    fragment 也要按查询口径脱敏（P1-02）：OAuth 隐式流程的重定向把
    ``access_token`` 等放在 ``#access_token=...`` 里，只查 query 会原样落盘。
    非 key=value 形态的 fragment（如锚点 ``#section-2``）不改动。
    """
    if not redact or not url:
        return url
    parts = urlsplit(url)
    netloc = parts.netloc
    if "@" in netloc:
        _userinfo, _, host = netloc.rpartition("@")
        # userinfo 里用裸 REDACTED 而非 ``[REDACTED]``：方括号会让 urlsplit
        # 把 host 当 IPv6 字面量而抛错，产物应是可再次解析的合法 URL
        netloc = "REDACTED:REDACTED@" + host
    rebuilt_query = _redact_kv(parts.query)
    rebuilt_fragment = _redact_kv(parts.fragment)
    return urlunsplit(
        (parts.scheme, netloc, parts.path, rebuilt_query, rebuilt_fragment)
    )


# ---------- 条目构造 ----------


def _as_bytes(value: Any) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8")
    return bytes(value)


def _body_bytes(message: Any) -> bytes:
    if message is None:
        return b""
    raw = getattr(message, "raw_content", None)
    if raw is not None:
        return _as_bytes(raw)
    return _as_bytes(getattr(message, "content", None))


def _mime_type(pairs: list[tuple[str, str]]) -> str:
    for name, value in pairs:
        if name.lower() == "content-type":
            return value
    return ""


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_BINARY_MIME_PREFIXES = (
    "image/",
    "audio/",
    "video/",
    "font/",
    "application/octet-stream",
    "application/zip",
    "application/pdf",
)


def _looks_binary_mime(mime: str) -> bool:
    lowered = (mime or "").lower()
    return any(lowered.startswith(prefix) for prefix in _BINARY_MIME_PREFIXES)


def _content_payload(data: bytes, mime: str, options: CaptureOptions) -> dict[str, Any]:
    """HAR content：默认 size+mimeType+sha256；显式正文才写 text/base64。"""
    payload: dict[str, Any] = {"size": len(data), "mimeType": mime}
    if not data:
        return payload
    if not options.capture_bodies:
        if len(data) <= options.max_hash_bytes:
            payload["sha256"] = _sha256_hex(data)
        else:
            payload["_sha256Skipped"] = True
            payload["comment"] = "body_too_large_for_sha256"
        return payload

    truncated = len(data) > options.body_limit_bytes
    chunk = data[: options.body_limit_bytes]
    payload["encoding"] = "base64" if _looks_binary_mime(mime) else ""
    if payload["encoding"]:
        payload["text"] = base64.b64encode(chunk).decode("ascii")
    else:
        try:
            payload["text"] = chunk.decode("utf-8")
        except UnicodeDecodeError:
            payload["encoding"] = "base64"
            payload["text"] = base64.b64encode(chunk).decode("ascii")
    if not payload["encoding"]:
        del payload["encoding"]
    if truncated:
        payload["_truncated"] = True
        payload["comment"] = "body_truncated"
    return payload


def _http_version(message: Any, default: str) -> str:
    version = getattr(message, "http_version", None)
    if isinstance(version, bytes):
        return version.decode("ascii", "replace")
    if version:
        return str(version)
    return default


def _iso_timestamp(value: Any) -> str:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = time.time()
    moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _request_body(request: Any) -> bytes:
    return _body_bytes(request)


def build_entry(
    flow: Any, options: CaptureOptions, *, error: str | None = None
) -> dict[str, Any] | None:
    """把 duck-typed flow（request/response/error）构造成 HAR 1.2 entry。

    不 import mitmproxy：真实 flow 与测试替身都只需具备同名属性。
    """
    request = getattr(flow, "request", None)
    if request is None:
        return None
    response = getattr(flow, "response", None)

    raw_url = str(getattr(request, "url", None) or _compose_url(request))
    request_headers = _header_pairs(getattr(request, "headers", None))
    response_headers = _header_pairs(getattr(response, "headers", None)) if response else []
    request_body = _request_body(request)
    request_mime = _mime_type(request_headers)

    status = int(getattr(response, "status_code", 0) or 0) if response else 0
    response_body = _body_bytes(response) if response else b""
    response_mime = _mime_type(response_headers)

    start = getattr(request, "timestamp_start", None)
    end = getattr(response, "timestamp_end", None) if response else None
    if end is None:
        end = getattr(flow, "timestamp_end", None)
    try:
        duration_ms = max(0.0, (float(end) - float(start)) * 1000.0)
    except (TypeError, ValueError):
        duration_ms = 0.0
    duration_ms = round(duration_ms, 3)

    request_har: dict[str, Any] = {
        "method": str(getattr(request, "method", "") or ""),
        "url": scrub_url(raw_url, redact=options.redact),
        "httpVersion": _http_version(request, "HTTP/1.1"),
        "cookies": _scrub_cookies(
            _cookies_from_headers(request_headers, "cookie"), options.redact
        ),
        "headers": scrub_headers(request_headers, redact=options.redact),
        "queryString": _query_string(raw_url, redact=options.redact),
        "headersSize": -1,
        "bodySize": len(request_body),
    }
    if request_body:
        request_har["postData"] = {
            "mimeType": request_mime,
            **_content_payload(request_body, request_mime, options),
        }

    if response is not None:
        content = _content_payload(response_body, response_mime, options)
        status_text = http.client.responses.get(status, "")
        response_har: dict[str, Any] = {
            "status": status,
            "statusText": status_text,
            "httpVersion": _http_version(response, "HTTP/1.1"),
            "cookies": _scrub_cookies(
                _cookies_from_headers(response_headers, "set-cookie"), options.redact
            ),
            "headers": scrub_headers(response_headers, redact=options.redact),
            "content": content,
            "redirectURL": _redirect_url(response_headers, options.redact),
            "headersSize": -1,
            "bodySize": len(response_body),
        }
    else:
        response_har = {
            "status": 0,
            "statusText": "",
            "httpVersion": "",
            "cookies": [],
            "headers": [],
            "content": {"size": 0, "mimeType": ""},
            "redirectURL": "",
            "headersSize": -1,
            "bodySize": 0,
        }

    entry: dict[str, Any] = {
        "startedDateTime": _iso_timestamp(start),
        "time": duration_ms,
        "request": request_har,
        "response": response_har,
        "cache": {},
        "timings": {"send": 0.0, "wait": duration_ms, "receive": 0.0},
    }
    if error or response is None:
        entry["_error"] = str(error or getattr(flow, "error", "") or "no response")
    return entry


def _compose_url(request: Any) -> str:
    """替身/极端情况下 request 没有 url 属性时按组件拼；不猜 scheme。"""
    scheme = str(getattr(request, "scheme", "") or "http")
    host = str(getattr(request, "host", "") or "")
    port = getattr(request, "port", None)
    default_port = 443 if scheme == "https" else 80
    authority = host if port in (None, default_port) else f"{host}:{port}"
    return f"{scheme}://{authority}{getattr(request, 'path', '/') or '/'}"


def _redirect_url(pairs: list[tuple[str, str]], redact: bool) -> str:
    for name, value in pairs:
        if name.lower() == "location":
            return scrub_url(value, redact=redact)
    return ""


# ---------- 流式 JSONL writer（addon 子进程内使用） ----------


class HarWriter:
    """逐行 append + flush 的 JSONL writer；超限丢弃，写失败只计数不抛错。"""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_bytes: int = DEFAULT_MAX_BYTES,
        on_drop: Any = None,
        on_error: Any = None,
        state_path: str | os.PathLike[str] | None = None,
    ):
        for name, value in (("max_entries", max_entries), ("max_bytes", max_bytes)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} 必须为正整数，收到 {value!r}")
        self.path = Path(path)
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._on_drop = on_drop
        self._on_error = on_error
        # None = 默认按 entries 路径派生；显式传空串关闭（纯内存/临时用法）
        self._state_path: Path | None = (
            state_path_for(self.path)
            if state_path is None
            else (Path(state_path) if state_path else None)
        )
        self.entries = 0
        self.dropped_entries = 0
        self.writer_errors = 0
        self.truncated_reason: str | None = None
        self.bytes_written = 0
        self._closed = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")
        # 旁路状态文件用常驻句柄（P1-04 复核性能缺口）：首次丢弃/失败时懒打开
        self._state_handle = None

    def _persist_state(self) -> None:
        """把精确计数写入常驻句柄的旁路状态文件；失败静默（节流日志是兜底证据）。

        每次丢弃/写失败都 seek+write+truncate+flush 同一句柄：flush 后的内容
        已在 OS 缓存，进程被硬杀不丢——计数精度边界只剩整机断电/OS 崩溃可能
        丢最后一次 flush 前的增量，此时 compose 退回节流日志推算并把
        countersSource 如实标为 proxy_log（下界）。
        修复前每条丢弃走"临时文件 + os.replace"（Windows 实测 ~1.16 ms/条、
        5000 条丢弃额外 ~5.8 s），限额触顶后高流量会放大该开销。
        """
        if self._state_path is None:
            return
        payload = json.dumps(
            {
                "droppedEntries": self.dropped_entries,
                "writerErrors": self.writer_errors,
                "truncatedReason": self.truncated_reason,
            },
            ensure_ascii=False,
        )
        try:
            if self._state_handle is None:
                self._state_handle = self._state_path.open("w", encoding="utf-8")
            self._state_handle.seek(0)
            self._state_handle.write(payload)
            self._state_handle.truncate()
            self._state_handle.flush()
        except (OSError, ValueError):
            self._state_handle = None  # 句柄失效即弃用，避免每条都撞同一错误

    def write(self, entry: dict[str, Any]) -> bool:
        """写一条 entry；被限额丢弃或写失败返回 False，绝不向上抛。"""
        if self._closed:
            return False
        try:
            line = json.dumps(entry, ensure_ascii=False) + "\n"
            encoded = line.encode("utf-8")
        except (TypeError, ValueError) as exc:
            self.writer_errors += 1
            if self._on_error is not None:
                self._on_error(str(exc))
            self._persist_state()
            return False
        if self.entries + self.dropped_entries >= self.max_entries:
            return self._drop("max_entries")
        if self.bytes_written + len(encoded) > self.max_bytes:
            return self._drop("max_bytes")
        try:
            self._handle.write(line)
            self._handle.flush()
        except (OSError, ValueError) as exc:
            # ValueError：底层句柄被外部关闭（进程收尾/管道失效），同样只计数
            self.writer_errors += 1
            if self._on_error is not None:
                self._on_error(str(exc))
            self._persist_state()
            return False
        self.entries += 1
        self.bytes_written += len(encoded)
        return True

    def _drop(self, reason: str) -> bool:
        self.dropped_entries += 1
        if self.truncated_reason is None:
            self.truncated_reason = reason
        if self._on_drop is not None:
            self._on_drop(reason)
        self._persist_state()
        return False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._handle.close()
        except OSError:
            pass
        if self._state_handle is not None:
            try:
                self._state_handle.close()
            except OSError:
                pass
            self._state_handle = None

    def __enter__(self) -> HarWriter:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False


# ---------- 原子合成 HAR 1.2 ----------


def compose_har(
    entries_path: str | os.PathLike[str],
    har_path: str | os.PathLike[str],
    *,
    meta: dict[str, Any] | None = None,
) -> HarSummary:
    """流式读 JSONL、跳过坏行，写同目录 ``.tmp`` 后 ``os.replace`` 原子落盘。

    无有效条目不创建 ``app.har``（避免用空文件冒充"抓到了"）；返回摘要里的
    ``meta`` 字段（status/httpsCoverage/notTestedReason/限额统计）由调用方给出。
    """
    meta = dict(meta or {})
    entries_path = Path(entries_path)
    har_path = Path(har_path)
    skipped = 0
    entries = 0
    tmp_path = har_path.with_name(har_path.name + ".tmp")
    summary = HarSummary(
        enabled=bool(meta.get("enabled", True)),
        status=str(meta.get("status") or STATUS_NOT_TESTED),
        entries_path=str(entries_path),
        body_mode=str(meta.get("bodyMode") or "metadata"),
        redacted=bool(meta.get("redacted", True)),
        dropped_entries=int(meta.get("droppedEntries") or 0),
        dropped_events=int(meta.get("droppedEvents") or 0),
        writer_errors=int(meta.get("writerErrors") or 0),
        counters_source=str(meta.get("countersSource") or "proxy_log"),
        truncated=bool(meta.get("truncated", False)),
        truncated_reason=meta.get("truncatedReason"),
        https_coverage=str(meta.get("httpsCoverage") or "unknown"),
        not_tested_reason=meta.get("notTestedReason"),
    )

    if not entries_path.is_file():
        summary.entries = 0
        summary.skipped_entries = 0
        return summary

    try:
        har_path.parent.mkdir(parents=True, exist_ok=True)
        with tmp_path.open("w", encoding="utf-8") as out:
            out.write(
                '{"log": {"version": "1.2", "creator": {"name": "mobile_perf", '
                '"version": "0.1.0"}, "pages": [], "entries": ['
            )
            with entries_path.open(encoding="utf-8", errors="replace") as source:
                for line in source:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        skipped += 1
                        continue
                    if (
                        not isinstance(entry, dict)
                        or not isinstance(entry.get("request"), dict)
                        or not isinstance(entry.get("response"), dict)
                    ):
                        # 合法 JSON 但不是 HAR entry（手工拼接/半截写入）同样跳过
                        skipped += 1
                        continue
                    if entries:
                        out.write(",\n")
                    out.write(json.dumps(entry, ensure_ascii=False))
                    entries += 1
            out.write("]}}")
        if entries == 0:
            tmp_path.unlink(missing_ok=True)
            summary.entries = 0
            summary.skipped_entries = skipped
            summary.path = None
            # 没有有效条目就不可能"完整/部分"：如实降为 not_tested
            summary.status = STATUS_NOT_TESTED
            return summary
        os.replace(tmp_path, har_path)
    except (OSError, TypeError, ValueError):
        tmp_path.unlink(missing_ok=True)
        summary.status = STATUS_FAILED
        summary.entries = 0
        summary.skipped_entries = skipped
        summary.writer_errors += 1
        summary.path = None
        if not summary.not_tested_reason:
            summary.not_tested_reason = "HAR 合成失败（读写错误）"
        return summary

    summary.entries = entries
    summary.skipped_entries = skipped
    summary.path = str(har_path)
    summary.size_bytes = har_path.stat().st_size
    return summary
