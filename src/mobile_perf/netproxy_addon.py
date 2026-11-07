"""mitmproxy 弱网 addon（工单 17 直通档 / 工单 18 解密档）——只由 mitmdump 子进程加载。

本包主进程**不** import 本模块。

mitmproxy 要求 Python ≥3.12，而本项目运行时 ≥3.10，故它跑在独立 venv 的解释器上
（与 ADR-0002 的 Harness 子进程边界同构）。本模块顶部 import 了 mitmproxy，
在主进程 import 会污染 sys.path，`netprof.py` 因此只用 subprocess 拉起它。

环境变量（由 ``netprof.NetProfiler`` 注入，不经命令行，避免 Windows 引号问题）：

- ``MOBILE_PERF_NETPARAMS``：JSON，整形参数 ``latency_ms/down_kbps/up_kbps/loss_pct``；
- ``MOBILE_PERF_NETTIER``：``passthrough``（默认，不解密）或 ``decrypt``（解 TLS）；
- ``MOBILE_PERF_NETLOG``：JSON Lines 输出路径。这份日志是**代理生效的唯一证据**
  ——被测 App 有本地缓存与多域名兜底，页面能打开不代表代理生效。

整形口径（逐条写进日志首行 ``params``，供报告复述）：

- **延迟**：每条转发消息各加一次固定延迟，双向各算一次；
- **带宽**：按消息字节数 / 目标速率追加排队延迟（下行用 ``down_kbps``，上行用 ``up_kbps``）；
- **丢包**：TCP 流级别**无法真实丢包**——丢字节会破坏流完整性，客户端只会看到协议
  错误而不是重传。故按 ``loss_pct`` 概率追加 2× 延迟的停顿，近似重传退避；
  日志 ``lossMode="stall_emulation"`` 如实标注这是近似而非真丢包。

两个档位的分工：

- **直通档**：CONNECT 后强制裸 TCP 层，不解密。整形与连接耗时只作用于 TLS 隧道；
  明文 HTTP 请求仅记录事件、不整形（实测被测 App 对外全为 TCP/443）。
- **解密档**：不强制裸 TCP，让 mitmproxy 做 TLS 中间人，从而拿到 URL/状态码/响应
  大小，产出接口级指标。**先探测再决定**：监听首个客户端 TLS 握手结果，一旦判定
  App 不信任代理 CA，立即把后续连接降为直通（保住连接级指标），接口级指标由
  `netprof` 标 not_tested——不重试、不猜测。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import random
import sys
import time
from pathlib import Path

from mitmproxy.proxy.layers import TCPLayer

_PARAMS = json.loads(os.environ.get("MOBILE_PERF_NETPARAMS") or "{}")
_LOG_PATH = os.environ.get("MOBILE_PERF_NETLOG") or ""
_TIER = os.environ.get("MOBILE_PERF_NETTIER") or "passthrough"

# HAR 抓包（默认关闭）：配置经单行 JSON 环境变量传入，addon 按同目录文件加载
# ``har_writer``（不依赖主包已安装，也不让主进程 import mitmproxy）。
_HAR_OPTIONS = None
_HAR_MODULE = None
_HAR_WRITER = None

_LATENCY_S = float(_PARAMS.get("latency_ms") or 0) / 1000.0
_DOWN_BPS = float(_PARAMS.get("down_kbps") or 0) * 1000.0 / 8.0
_UP_BPS = float(_PARAMS.get("up_kbps") or 0) * 1000.0 / 8.0
_LOSS = float(_PARAMS.get("loss_pct") or 0) / 100.0
_RANDOM = random.Random()

# 走了 CONNECT 的客户端连接：只有它们需要在 next_layer 里强制裸 TCP（不做 TLS 解密）
_TUNNEL_CLIENTS: set[str] = set()

# 信任探测结论：None=未判定，True=解密可用，False=App 不信任代理 CA。
# 判定为 False 之后，后续 CONNECT 一律降为直通，保证连接级指标仍可用。
_DECRYPT_OK: bool | None = None
_PROBE_EMITTED = False

# 证书类失败的错误文本特征（mitmproxy 12.x 的 tls.py 会把这些原样放进 conn.error）
_CERT_ERROR_MARKERS = (
    "certificate verify failed",
    "unknown ca",
    "bad certificate",
    "certificate unknown",
)


def _emit(event: str, **fields) -> None:
    """追加一条 JSON 行并立即落盘（进程被 kill 也不丢已发生的事件）。"""
    if not _LOG_PATH:
        return
    record = {"t": round(time.time(), 6), "ev": event, **fields}
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _load_har_writer():
    """按同目录文件加载 har_writer.py；不要求主包已安装。

    Python 3.12 的 ``dataclasses`` 在处理 ``from __future__ import annotations``
    的类注解时会回查 ``sys.modules[cls.__module__]``；动态模块必须先注册，否则
    ``exec_module`` 在 dataclass 装饰器处抛 AttributeError，真实 mitmdump 子进程
    会静默丢掉整个抓包能力。失败时撤销注册，避免留下半初始化模块。
    """
    path = Path(__file__).resolve().parent / "har_writer.py"
    spec = importlib.util.spec_from_file_location("mobile_perf_har_writer", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 HAR writer: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return module


def _init_har() -> None:
    """初始化 HAR 写入链路；任何异常都如实记 har_error，不阻断代理转发。"""
    global _HAR_OPTIONS, _HAR_MODULE, _HAR_WRITER
    raw = os.environ.get("MOBILE_PERF_HAR") or ""
    if not raw:
        return
    try:
        config = json.loads(raw)
        entries = config.get("entriesPath")
        env_entries = os.environ.get("MOBILE_PERF_HAR_ENTRIES") or ""
        if env_entries and entries and env_entries != entries:
            raise ValueError("MOBILE_PERF_HAR_ENTRIES 与 entriesPath 不一致")
        entries = entries or env_entries
        if not entries:
            raise ValueError("HAR 配置缺少 entriesPath")
        module = _load_har_writer()
        options = module.CaptureOptions(
            enabled=True,
            capture_bodies=bool(config.get("captureBodies")),
            body_limit_bytes=int(config.get("bodyLimitBytes") or 65536),
            max_entries=int(config.get("maxEntries") or 5000),
            max_bytes=int(config.get("maxBytes") or 67108864),
            redact=bool(config.get("redact", True)),
            max_hash_bytes=int(config.get("maxHashBytes") or 8 * 1024 * 1024),
        )
        dropped_reasons: set[str] = set()

        def on_drop(reason: str) -> None:
            # 达到上限后可能持续丢包；只把首个原因写入唯一生效证据日志，
            # 避免重流量下把 net_proxy.jsonl 灌爆。
            if reason not in dropped_reasons:
                dropped_reasons.add(reason)
                _emit("har_dropped", reason=reason, truncated=True)

        writer = module.HarWriter(
            entries,
            max_entries=options.max_entries,
            max_bytes=options.max_bytes,
            on_drop=on_drop,
            on_error=lambda message: _emit("har_error", error=message),
        )
    except Exception as exc:  # noqa: BLE001 - 抓包失败不能阻断弱网与采集
        _emit("har_error", error=f"init: {exc}")
        return
    _HAR_OPTIONS = options
    _HAR_MODULE = module
    _HAR_WRITER = writer


def _write_har(flow, *, error: str | None = None, websocket: bool = False) -> None:
    """写一条已脱敏 HAR entry；异常统一捕获并 emit har_error，不中断转发。"""
    if _HAR_WRITER is None or _HAR_MODULE is None or _HAR_OPTIONS is None:
        return
    request = getattr(flow, "request", None)
    if request is not None and str(getattr(request, "method", "")).upper() == "CONNECT":
        return  # CONNECT 是隧道建立，不是请求；绝不写成伪请求
    try:
        entry = _HAR_MODULE.build_entry(flow, _HAR_OPTIONS, error=error)
        if entry is None:
            return
        if websocket:
            entry["_websocket"] = True
        _HAR_WRITER.write(entry)
    except Exception as exc:  # noqa: BLE001 - 单条失败只记事件
        _emit("har_error", error=str(exc))


def load(loader) -> None:
    _emit(
        "params",
        latencyMs=_LATENCY_S * 1000.0,
        downKbps=_DOWN_BPS * 8.0 / 1000.0,
        upKbps=_UP_BPS * 8.0 / 1000.0,
        lossPct=_LOSS * 100.0,
        lossMode="stall_emulation" if _LOSS else "none",
        tier=_TIER,
        harEnabled=bool(_HAR_WRITER),
    )


# 模块加载即初始化（配置缺失时什么也不做）
_init_har()


def next_layer(data) -> None:
    """CONNECT 隧道建立后决定走裸 TCP（直通）还是 TLS 中间人（解密）。

    直通档：强制裸 TCP，只做连接级整形，拿不到 URL。
    解密档：先放行给 mitmproxy 的 TLS 层；只有信任探测已判定"App 不信任代理 CA"
    时才改走裸 TCP——这样本 run 仍有连接级指标，接口级指标由 netprof 标 not_tested。
    """
    context = data.context
    if context.server.address is None:
        return  # 还在决定客户端侧协议（HTTP 层负责解析 CONNECT）
    if context.client.id not in _TUNNEL_CLIENTS:
        return  # 明文 HTTP 走默认 HTTP 层，不在本 addon 的整形范围
    if _TIER != "decrypt" or _DECRYPT_OK is False:
        data.layer = TCPLayer(context)


def _looks_like_cert_rejection(error: str) -> bool:
    lowered = (error or "").lower()
    return any(marker in lowered for marker in _CERT_ERROR_MARKERS)


def _emit_probe(ok: bool, error: str | None, host: str | None) -> None:
    """信任探测结论只记一次（首个结论即定论，后续握手不再改写）。"""
    global _PROBE_EMITTED
    if _PROBE_EMITTED:
        return
    _PROBE_EMITTED = True
    _emit(
        "trust_probe",
        ok=ok,
        error=error,
        host=host,
        certRejected=bool(error and _looks_like_cert_rejection(error)),
    )


def tls_established_client(data) -> None:
    """客户端 TLS 握手成功：解密档可用（App 信任了代理 CA）。"""
    global _DECRYPT_OK
    if _TIER != "decrypt" or _DECRYPT_OK is not None:
        return
    _DECRYPT_OK = True
    _emit_probe(True, None, getattr(data.context.server, "address", (None, 0))[0])


def tls_failed_client(data) -> None:
    """客户端 TLS 握手失败：判定是否证书类失败，并据此锁定本 run 的档位能力。

    证书类失败 → App 不信任代理 CA（Android 7+ 对 targetSdk ≥24 的 App 默认只信任
    系统 CA）。此时**不重试**：后续 CONNECT 全部降为直通，接口级指标如实 not_tested。

    非证书类失败（网络抖动、协议版本不一致等）不锁定档位——那不是"App 不信任"的
    证据，锁定它会把一次偶发失败误判成本 run 的永久结论。
    """
    global _DECRYPT_OK
    if _TIER != "decrypt":
        return
    error = str(getattr(data.conn, "error", "") or "")
    host = getattr(data.context.server, "address", (None, 0))[0]
    if not _looks_like_cert_rejection(error):
        _emit("tls_client_error", error=error, host=host)
        return
    if _DECRYPT_OK is None:
        _DECRYPT_OK = False
    _emit_probe(False, error, host)


def _api_url(flow) -> tuple[str, str]:
    """(host, method + 去查询串的路径)：查询串常带 id/时间戳，全留着会把 URL 打散。"""
    request = flow.request
    path = request.path.split("?", 1)[0] or "/"
    return request.host, f"{request.method} {path}"


def response(flow) -> None:
    """HAR 条目在所有档位都写；接口级明细仍只在解密档产出。

    明文 HTTP 不需要 App 信任代理 CA，因此即使 ``passthrough`` 或证书被拒绝，
    只要 mitmproxy 解出了 HTTP 层，就应留下真实请求/响应。
    """
    _write_har(flow)
    if _TIER != "decrypt":
        return
    request, resp = flow.request, flow.response
    host, route = _api_url(flow)
    ended = resp.timestamp_end or time.time()
    duration_ms = max(0.0, (ended - request.timestamp_start) * 1000.0)
    body = resp.raw_content or b""
    _emit(
        "api_request",
        fid=flow.id,
        host=host,
        method=request.method,
        route=route,
        status=resp.status_code,
        durationMs=round(duration_ms, 3),
        sizeBytes=len(body),
    )


def server_connect_error(data) -> None:
    """连接失败也算一次接口错误（拿不到状态码，按错误计入错误率）。"""
    _emit("server_connect_error", cid=data.client.id, error=str(data.server.error))
    if _TIER == "decrypt":
        _emit(
            "api_error",
            cid=data.client.id,
            host=str((data.server.address or ("", 0))[0]),
            error=str(data.server.error),
        )


def _delay_for(size: int, from_client: bool) -> float:
    delay = _LATENCY_S
    rate = _UP_BPS if from_client else _DOWN_BPS
    if rate > 0 and size > 0:
        delay += size / rate
    if _LOSS > 0 and _RANDOM.random() < _LOSS:
        delay += _LATENCY_S * 2  # 近似 TCP 重传退避（真丢包在 TCP 流级别不可模拟）
    return delay


def client_connected(client) -> None:
    _emit("client_connected", cid=client.id)


def client_disconnected(client) -> None:
    _emit("client_disconnected", cid=client.id, error=str(client.error))


def http_connect(flow) -> None:
    _TUNNEL_CLIENTS.add(flow.client_conn.id)
    _emit(
        "http_connect",
        cid=flow.client_conn.id,
        host=flow.request.host,
        port=flow.request.port,
    )


def tcp_start(flow) -> None:
    address = flow.server_conn.address or ("", 0)
    _emit(
        "tcp_start",
        fid=flow.id,
        cid=flow.client_conn.id,
        host=str(address[0]),
        port=int(address[1]),
    )


async def tcp_message(flow) -> None:
    message = flow.messages[-1]
    delay = _delay_for(len(message.content), message.from_client)
    if delay > 0:
        await asyncio.sleep(delay)
    _emit(
        "tcp_message",
        fid=flow.id,
        cid=flow.client_conn.id,
        from_client=bool(message.from_client),
        size=len(message.content),
    )


def tcp_end(flow) -> None:
    _emit("tcp_end", fid=flow.id, cid=flow.client_conn.id)


def tcp_error(flow) -> None:
    _emit("tcp_error", fid=flow.id, cid=flow.client_conn.id, error=str(flow.error))


def server_connect(data) -> None:
    address = data.server.address or ("", 0)
    _emit("server_connect", cid=data.client.id, host=str(address[0]), port=int(address[1]))


def server_connected(data) -> None:
    _emit("server_connected", cid=data.client.id)


def server_disconnected(data) -> None:
    _emit("server_disconnected", cid=data.client.id)


def request(flow) -> None:
    _emit(
        "http_request",
        fid=flow.id,
        cid=flow.client_conn.id,
        method=flow.request.method,
        host=flow.request.host,
        path=flow.request.path,
    )


def error(flow) -> None:
    _emit("error", fid=flow.id, error=str(flow.error))
    # 错误 flow 也要有条目：response.status=0 且 _error 非空，绝不静默丢样本
    _write_har(flow, error=str(flow.error))


def websocket_end(flow) -> None:
    """WebSocket 正常关闭时落一条条目（未关闭的长连接不在保证覆盖内）。"""
    _write_har(flow, websocket=True)
