"""采集会话生命周期：启动 → 打标 → 停止，时间轴以会话起点为零点。

会话不直接知道 Harness：start/stop 是注入的调用（由 kit 绑定 solo 适配层），
便于单元测试与将来替换启动方式。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Mark:
    """宿主在采集会话内声明的时刻点，elapsed_s 相对会话开始。"""

    label: str
    elapsed_s: float


@dataclass
class SessionRecord:
    """一次采集会话的完整记录，供归档、入库与报告消费。"""

    session_id: str | None
    target: str
    items: list[str]
    serial: str
    started_at: str
    stopped_at: str
    duration_s: float
    marks: list[Mark] = field(default_factory=list)
    stop_payload: dict[str, Any] = field(default_factory=dict)

    def marks_as_list(self) -> list[dict[str, Any]]:
        return [{"label": m.label, "elapsedS": round(m.elapsed_s, 3)} for m in self.marks]


class CollectSession:
    """一次端侧性能采集的生命周期。

    start/stop 签名约定：
    - start() -> str：发起采集并返回端侧 sessionId；
    - stop(session_id, output_dir) -> dict：停止采集并 pull 产物，返回 Harness 载荷。
    """

    def __init__(
        self,
        *,
        target: str,
        items: list[str],
        serial: str,
        start: Callable[[], str | None],
        stop: Callable[[str | None, str], dict[str, Any]],
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], str] = lambda: datetime.now().astimezone().isoformat(timespec="seconds"),
    ):
        self.target = target
        self.items = list(items)
        self.serial = serial
        self._start_call = start
        self._stop_call = stop
        self._clock = clock
        self._now = now
        self.session_id: str | None = None
        self.started_at: str | None = None
        self._started_mono: float | None = None
        self.marks: list[Mark] = []

    def _require_started(self, action: str) -> float:
        if self._started_mono is None:
            raise RuntimeError(f"cannot {action} before start")
        return self._started_mono

    def start(self) -> str | None:
        if self._started_mono is not None:
            raise RuntimeError("session already started")
        self._started_mono = self._clock()
        self.started_at = self._now()
        self.session_id = self._start_call()
        return self.session_id

    def mark(self, label: str) -> Mark:
        self._require_started("mark")
        mark = Mark(label=label, elapsed_s=self._clock() - self._started_mono)
        self.marks.append(mark)
        return mark

    def stop(self, output_dir: str) -> SessionRecord:
        self._require_started("stop")
        stopped_mono = self._clock()
        payload = self._stop_call(self.session_id, output_dir)
        return SessionRecord(
            session_id=self.session_id,
            target=self.target,
            items=self.items,
            serial=self.serial,
            started_at=self.started_at or "",
            stopped_at=self._now(),
            duration_s=stopped_mono - self._started_mono,
            marks=list(self.marks),
            stop_payload=payload,
        )
