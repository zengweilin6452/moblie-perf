"""mitmproxy 直通整形 addon（工单 17）——**只由 mitmdump 子进程加载，本包主进程不 import**。

mitmproxy 要求 Python ≥3.12，而本项目运行时 ≥3.10，故它跑在独立 venv 的解释器上
（与 ADR-0002 的 Harness 子进程边界同构）。本模块顶部 import 了 mitmproxy，
在主进程 import 会污染 sys.path，`netprof.py` 因此只用 subprocess 拉起它。

环境变量（由 ``netprof.NetProfiler`` 注入，不经命令行，避免 Windows 引号问题）：

- ``MOBILE_PERF_NETPARAMS``：JSON，整形参数 ``latency_ms/down_kbps/up_kbps/loss_pct``；
- ``MOBILE_PERF_NETLOG``：JSON Lines 输出路径。这份日志是**代理生效的唯一证据**
  ——被测 App 有本地缓存与多域名兜底，页面能打开不代表代理生效。

整形口径（逐条写进日志首行 ``params``，供报告复述）：

- **延迟**：每条转发消息各加一次固定延迟，双向各算一次；
- **带宽**：按消息字节数 / 目标速率追加排队延迟（下行用 ``down_kbps``，上行用 ``up_kbps``）；
- **丢包**：TCP 流级别**无法真实丢包**——丢字节会破坏流完整性，客户端只会看到协议
  错误而不是重传。故按 ``loss_pct`` 概率追加 2× 延迟的停顿，近似重传退避；
  日志 ``lossMode="stall_emulation"`` 如实标注这是近似而非真丢包。

整形与连接耗时**只作用于 TLS 隧道**（CONNECT 后强制裸 TCP 层，不解密）；明文 HTTP
请求仅记录事件、不整形——实测被测 App 对外全为 TCP/443。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time

from mitmproxy.proxy.layers import TCPLayer

_PARAMS = json.loads(os.environ.get("MOBILE_PERF_NETPARAMS") or "{}")
_LOG_PATH = os.environ.get("MOBILE_PERF_NETLOG") or ""

_LATENCY_S = float(_PARAMS.get("latency_ms") or 0) / 1000.0
_DOWN_BPS = float(_PARAMS.get("down_kbps") or 0) * 1000.0 / 8.0
_UP_BPS = float(_PARAMS.get("up_kbps") or 0) * 1000.0 / 8.0
_LOSS = float(_PARAMS.get("loss_pct") or 0) / 100.0
_RANDOM = random.Random()

# 走了 CONNECT 的客户端连接：只有它们需要在 next_layer 里强制裸 TCP（不做 TLS 解密）
_TUNNEL_CLIENTS: set[str] = set()


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


def load(loader) -> None:
    _emit(
        "params",
        latencyMs=_LATENCY_S * 1000.0,
        downKbps=_DOWN_BPS * 8.0 / 1000.0,
        upKbps=_UP_BPS * 8.0 / 1000.0,
        lossPct=_LOSS * 100.0,
        lossMode="stall_emulation" if _LOSS else "none",
    )


def next_layer(data) -> None:
    """CONNECT 隧道建立后强制裸 TCP 层：不解密、只做连接级整形。"""
    context = data.context
    if context.server.address is None:
        return  # 还在决定客户端侧协议（HTTP 层负责解析 CONNECT）
    if context.client.id not in _TUNNEL_CLIENTS:
        return  # 明文 HTTP 走默认 HTTP 层，不在本 addon 的整形范围
    data.layer = TCPLayer(context)


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


def server_connect_error(data) -> None:
    _emit("server_connect_error", cid=data.client.id, error=str(data.server.error))


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
