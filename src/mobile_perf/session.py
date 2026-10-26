"""采集会话生命周期：启动 → 打标 → 停止，时间轴以会话起点为零点。

会话不直接知道 Harness：start/stop 是注入的调用（由 kit 绑定 solo 适配层），
便于单元测试与将来替换启动方式。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import startup as startup_module
from .supplement import DEFAULT_INTERVAL_S


@dataclass(frozen=True)
class SessionSpec:
    """一次采集会话的声明（设计方案 §6.2 库 API 主形态）。

    target 为被测应用包名；items 为 SoloPi 采集项（GLOSSARY：Collect Items，
    API 参数名沿用上游契约，由指标字典翻译为 canonical key）。host_run_id 未传
    时由 kit 自动生成；runs_dir/supplement 与 kit.collect 同名参数语义一致。
    start_mode 为启动编排模式（ticket 12）：None=不编排；cold=窗口内
    force-stop 后拉起；warm=退后台后再拉起（GLOSSARY：启动模式）。
    """

    target: str
    items: list[str]
    host_run_id: str | None = None
    runs_dir: str | Path | None = None
    supplement: bool = True
    supplement_interval_s: float = DEFAULT_INTERVAL_S
    start_mode: str | None = None

    def __post_init__(self):
        if self.start_mode is not None and self.start_mode not in startup_module.STARTUP_MODES:
            raise ValueError(
                f"start_mode 只支持 {', '.join(startup_module.STARTUP_MODES)} 或 None，"
                f"收到 {self.start_mode!r}"
            )


MARK_KIND_MARK = "mark"
MARK_KIND_PAGE = "page"


@dataclass(frozen=True)
class Mark:
    """宿主在采集会话内声明的时刻点，elapsed_s 相对会话开始。

    ``kind`` 区分语义（工单 19）：普通打标是 ``mark``；``page`` 是**页面起点**，
    整页加载耗时以它为计时原点。两者共用同一条统一时间轴，只是消费方式不同——
    普通打标只进时间轴，页面打标还会参与 ``page_load_ms`` 判定。
    """

    label: str
    elapsed_s: float
    kind: str = MARK_KIND_MARK

    @property
    def is_page(self) -> bool:
        return self.kind == MARK_KIND_PAGE


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
        """打标列表（报告形状）。普通打标只给 label/elapsedS（既有契约不变）；
        页面打标额外带 ``kind=page``——用增字段而非改字段，旧消费方不受影响。"""
        items: list[dict[str, Any]] = []
        for mark in self.marks:
            item: dict[str, Any] = {"label": mark.label, "elapsedS": round(mark.elapsed_s, 3)}
            if mark.is_page:
                item["kind"] = mark.kind
            items.append(item)
        return items


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

    def mark_page(self, page: str) -> Mark:
        """声明一个**页面起点**（工单 19）：整页加载耗时从这一刻算起。

        与 ``mark`` 的区别只在语义与消费方式：本方法落 ``kind=page``，
        编排层据此把该时刻作为 ``page_load_ms`` 的计时原点。同一页面名可多次
        打标（如同一页面重进），各自独立出一条结论。
        """
        if not page:
            raise ValueError("page 不能为空")
        self._require_started("mark_page")
        mark = Mark(
            label=page,
            elapsed_s=self._clock() - self._started_mono,
            kind=MARK_KIND_PAGE,
        )
        self.marks.append(mark)
        return mark

    def elapsed_s(self) -> float | None:
        """当前时刻相对会话开始的秒数（统一时间轴读数）；未开始返回 None。"""
        if self._started_mono is None:
            return None
        return self._clock() - self._started_mono

    def page_marks(self) -> list[Mark]:
        """本会话声明的全部页面起点（按声明顺序）。"""
        return [mark for mark in self.marks if mark.is_page]

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
