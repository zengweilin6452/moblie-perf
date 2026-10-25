"""DeviceGate 短事务设备准入（ADR-0001）：借用于设备命令窗口，采集进行中不持锁。

默认实现 FileLockGate 按 moblie-auto 锁文件协议对等参与（不 import 其代码）：
锁文件 `~/.mobile-auto/leases/<sha256(serial)[:24]>.lock`，Windows 上以 msvcrt
对 1 字节区域加非阻塞锁；伴生 <digest>.holder.json 记 pid/hostname/purpose，
仅持锁者写入、释放不擦除。获取失败立即抛 GateBusyError——不内置等待重试，
重试是宿主策略。非 Windows 文件锁不可用：passthrough 并发 warning 事件。

嵌入模式重入：获取失败但 holder.json 的 pid 为本进程时放行（宿主侧——例如
持有整生命周期 DeviceLease 的 moblie-auto——已保证串行化，本组件不再持锁）。
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

DEFAULT_LEASE_DIR = Path.home() / ".mobile-auto" / "leases"

WarnEvent = dict
WarningHandler = Callable[[WarnEvent], None]


class GateBusyError(RuntimeError):
    """设备准入失败：锁被其他进程持有；fast-fail，不排队、不重试。"""


class DeviceGate(Protocol):
    """短事务设备准入协议：返回的上下文管理器退出即归还设备权。"""

    def borrow(
        self, serial: str, *, purpose: str, timeout_s: float = 10.0
    ) -> AbstractContextManager[None]: ...


def _warn_to_stderr(event: WarnEvent) -> None:
    print(
        f"[mobile-perf] warning: {event['event']} serial={event['serial']} "
        f"reason={event['reason']}",
        file=sys.stderr,
    )


def _digest(serial: str) -> str:
    return hashlib.sha256(serial.encode("utf-8")).hexdigest()[:24]


def _read_holder_pid(holder_path: Path) -> int | None:
    try:
        return int(json.loads(holder_path.read_text(encoding="utf-8"))["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


class _HeldToken(AbstractContextManager[None]):
    """一次成功借用的持有令牌：退出解锁并关闭句柄，幂等。"""

    def __init__(self, gate: FileLockGate):
        self._gate = gate

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc_info) -> None:
        self._gate._release()


class FileLockGate:
    """moblie-auto 锁文件协议的默认实现（ADR-0001：独立 CLI 模式）。"""

    def __init__(
        self,
        lease_dir: str | Path | None = None,
        *,
        serial: str | None = None,
        on_warning: WarningHandler | None = None,
        platform: str | None = None,
        pid: int | None = None,
    ):
        self._dir = Path(lease_dir) if lease_dir is not None else DEFAULT_LEASE_DIR
        self._serial = serial
        self._on_warning: WarningHandler = on_warning or _warn_to_stderr
        self._platform = platform if platform is not None else os.name
        self._pid = os.getpid() if pid is None else pid
        self._mutex = threading.Lock()  # 进程内并发保护（同 DeviceLease）
        self._handle = None  # OS 句柄;非空即本实例持有

    @property
    def path(self) -> Path:
        return self._dir / f"{_digest(self._require_serial())}.lock"

    @property
    def holder_path(self) -> Path:
        return self._dir / f"{_digest(self._require_serial())}.holder.json"

    def _require_serial(self) -> str:
        if self._serial is None:
            raise ValueError("gate 未绑定设备 serial：构造时传入或先 borrow")
        return self._serial

    @property
    def held(self) -> bool:
        return self._handle is not None

    def borrow(
        self, serial: str, *, purpose: str, timeout_s: float = 10.0
    ) -> AbstractContextManager[None]:
        """短事务借用；timeout_s 保留给宿主策略，本实现不等待（非阻塞获取）。"""
        self._serial = serial  # 借用期 serial 即当前绑定（单设备使用约定）
        if self._platform != "nt":
            self._on_warning(
                {
                    "event": "gate_passthrough",
                    "serial": serial,
                    "reason": "file lock unavailable on non-windows",
                }
            )
            return nullcontext()
        return self._borrow_windows(serial, purpose)

    def _borrow_windows(self, serial: str, purpose: str) -> AbstractContextManager[None]:
        import msvcrt

        with self._mutex:
            if self._handle is not None:
                return nullcontext()  # 本实例已持有：重入放行
            self._dir.mkdir(parents=True, exist_ok=True)
            handle = open(self.path, "a+b")
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                handle.close()
                if _read_holder_pid(self.holder_path) == self._pid:
                    return nullcontext()  # 嵌入模式：本进程已持锁，重入放行
                raise GateBusyError(
                    f"设备 {serial} 正被其他进程占用（purpose={purpose}），"
                    "fast-fail 不重试"
                ) from None
            self._handle = handle
            self._write_holder(purpose)
        return _HeldToken(self)

    def _write_holder(self, purpose: str) -> None:
        """覆写伴生持有者 JSON；仅持锁者写入（单写者），尽力而为。"""
        try:
            self.holder_path.write_text(
                json.dumps(
                    {
                        "pid": self._pid,
                        "hostname": socket.gethostname(),
                        "worker": "",
                        "purpose": purpose,
                        "acquired_at": datetime.now(timezone.utc).isoformat(
                            timespec="milliseconds"
                        ),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _release(self) -> None:
        import msvcrt

        with self._mutex:
            if self._handle is None:
                return
            try:
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass  # 区域已被 OS 随句柄处置时忽略;句柄关闭兜底
            finally:
                self._handle.close()
                self._handle = None


__all__ = ["DeviceGate", "FileLockGate", "GateBusyError"]
