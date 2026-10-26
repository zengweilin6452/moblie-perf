"""逐帧采集（ticket 15）：从 ``dumpsys gfxinfo <pkg> framestats`` 解析真逐帧耗时。

与工单 14 的卡顿分级（窗口级口径，源自 SoloPi CSV 的每采样窗口最长延迟）并存：
本模块给出**逐帧**的 P50/P90/P99 与超时帧占比，粒度更细但两者不可互算。

真实输出形状（tests/fixtures/gfxinfo/framestats_real.txt 真机样本校准）：

- 正文中可有多个 ``---PROFILEDATA---`` 分段，每段紧跟一行 CSV 表头与若干数据行；
- 时间戳均为**纳秒**；单帧耗时 = ``FrameCompleted - IntendedVsync``；
- ``FrameCompleted`` 为 0 表示该帧尚未完成，跳过（否则会算出负值或巨值）；
- 同一帧可能在多段中重复出现（实测同一次 dump 内以 ``FrameTimelineVsyncId``
  去重后 120 行全唯一），故以该列作去重键。

**表头不可信（真机实测）**：``dumpsys gfxinfo`` 打印的表头把 ``FrameInterval``
与 ``FrameStartTime`` 写反了——表头第 10 列声称是 ``FrameInterval``，实际该位置
是 ``FrameStartTime`` 的纳秒时间戳，真正的周期值在表头第 11 列（``FrameStartTime``）。
数据本身严格按 AOSP ``FrameInfoIndex`` 的列序排列。因此本模块**按位置解析**
（见 ``_COL``），并对取到的周期值做量级校验：超过 1 s 视为取错列，回退到相邻列
找周期量级的值，仍找不到才判为无周期信息。

轮询策略：会话开始时 ``gfxinfo <pkg> reset`` 清零窗口，之后每轮读一次并增量
累积（按 vsync id 去重），会话结束取全量。``dumpsys`` 不触发渲染，间隔 ≥1 s
对被测 App 无实质影响。

**帧完成时刻与时钟域**（工单 19 起需要）：``FrameCompleted`` 是设备
``CLOCK_MONOTONIC`` 纳秒值，与宿主时间轴不同源。故 ``FrameSample.completed_s``
保留设备时钟域原值；换算靠 ``measure_clock_offset`` 做**括号式**测量（读
``/proc/uptime`` 前后各取一次宿主时刻取中点，消掉 adb 往返的一半），
会话结束时测一次即可——本会话内两条时钟的漂移远小于本指标的判据粒度。
"""

from __future__ import annotations

import csv
import io
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from . import metrics as metrics_module

# framestats 分段标记与表头首列（真实输出以此二者定位数据区）
_SECTION_MARKER = "---PROFILEDATA---"
_HEADER_FIRST_COLUMN = "Flags"

_NS_PER_MS = 1_000_000.0

# 数据列位置（AOSP FrameInfoIndex 顺序，真机实测数据严格按此排列）。
# 不用表头名索引：dumpsys 打印的表头把 FrameInterval/FrameStartTime 写反了。
_COL = {
    "flags": 0,
    "vsync_id": 1,
    "intended_vsync": 2,
    "frame_completed": 16,
    "frame_interval": 11,  # 表头第 10 列声称是它，实为 FrameStartTime 时间戳
}

# 周期值的合法上界（毫秒）：超过即认为取错列（时间戳量级为 1e10 ms）
_MAX_INTERVAL_MS = 1000.0

# 超时帧判据：帧耗时 > 刷新周期 × 该倍数（Android 侧 Janky 的常用近似）
JANK_MULTIPLIER = 1.5

# 入库 canonical key（metrics.METRICS 中登记）
METRIC_P50 = "app_frame_p50_ms"
METRIC_P90 = "app_frame_p90_ms"
METRIC_P99 = "app_frame_p99_ms"
METRIC_JANK_RATIO = "app_frame_jank_ratio_pct"
METRIC_COUNT = "app_frame_count"
STAT_METRIC_KEYS = (METRIC_P50, METRIC_P90, METRIC_P99, METRIC_JANK_RATIO, METRIC_COUNT)

DEFAULT_INTERVAL_S = 2.0
SHELL_TIMEOUT_S = 20.0

# 设备 uptime 读取命令（/proc/uptime 首列即 CLOCK_BOOTTIME 秒）
UPTIME_PATH = "/proc/uptime"


@dataclass(frozen=True)
class FrameSample:
    """一帧的耗时与对应刷新周期（毫秒）；vsync_id 用于跨轮去重。

    ``completed_s`` 为该帧的 ``FrameCompleted`` 换算成的**设备 uptime 秒**
    （工单 19：整页耗时需要帧时刻，不只是帧时长）。它仍在设备时钟域，
    换算到会话时间轴是编排层的事（见模块文档与 ``pageflow.clock_offset_s``）。
    """

    vsync_id: str
    duration_ms: float
    interval_ms: float | None
    completed_s: float | None = None


def parse_framestats(text: str) -> list[FrameSample]:
    """解析 framestats 文本为逐帧样本（未完成帧跳过，跨段按 vsync id 去重）。

    返回顺序与输出顺序一致；同 id 保留首次出现的那一帧。
    """
    samples: list[FrameSample] = []
    seen: set[str] = set()
    for block in (text or "").split(_SECTION_MARKER):
        rows = _iter_block_rows(block)
        for row in rows:
            sample = _row_to_sample(row)
            if sample is None or sample.vsync_id in seen:
                continue
            seen.add(sample.vsync_id)
            samples.append(sample)
    return samples


def _iter_block_rows(block: str) -> Iterable[list[str]]:
    """在一个 PROFILEDATA 分段内定位表头并逐行产出**按位置的列值列表**。

    表头只用于确认这是 framestats 数据区（首列是 ``Flags``），列名本身不参与
    取值——真机表头的 FrameInterval/FrameStartTime 顺序与实际数据相反。
    """
    lines = block.strip().splitlines()
    if not lines or not lines[0].lstrip().startswith(_HEADER_FIRST_COLUMN):
        return []
    rows: list[list[str]] = []
    for raw in csv.reader(io.StringIO("\n".join(lines[1:]))):
        if len(raw) <= _COL["frame_completed"]:
            continue
        if not raw[_COL["flags"]].strip().lstrip("-").isdigit():
            continue
        rows.append([cell.strip() for cell in raw])
    return rows


def _row_to_sample(row: list[str]) -> FrameSample | None:
    """把一行按位置取值的 CSV 转成 FrameSample；非数值/未完成帧返回 None。"""
    try:
        completed = int(row[_COL["frame_completed"]])
        intended = int(row[_COL["intended_vsync"]])
    except (IndexError, TypeError, ValueError):
        return None
    if completed <= 0 or intended <= 0:
        return None  # 未完成帧（FrameCompleted=0）或字段缺省
    duration_ms = (completed - intended) / _NS_PER_MS
    if duration_ms < 0:
        return None
    return FrameSample(
        vsync_id=row[_COL["vsync_id"]],
        duration_ms=duration_ms,
        interval_ms=_interval_ms(row),
        completed_s=completed / 1e9,
    )


def _interval_ms(row: list[str]) -> float | None:
    """取该帧的刷新周期（毫秒）；取不到或量级不合理时回退到相邻列。"""
    for index in (_COL["frame_interval"], _COL["frame_interval"] - 1, _COL["frame_interval"] + 1):
        try:
            value = int(row[index]) / _NS_PER_MS
        except (IndexError, TypeError, ValueError):
            continue
        if 0 < value <= _MAX_INTERVAL_MS:
            return value
    return None


def frame_stats(samples: list[FrameSample]) -> dict[str, Any]:
    """把逐帧样本聚成统计结论；无样本返回空字典（不硬造 0 值）。

    jank 判据用**逐帧** FrameInterval（缺失时退回全体中位数）；每帧与其自身
    刷新周期比较，避免可变刷新率下用单一周期误判。
    """
    if not samples:
        return {}
    durations = sorted(sample.duration_ms for sample in samples)
    intervals = [sample.interval_ms for sample in samples if sample.interval_ms]
    fallback_interval = metrics_module.percentile(sorted(intervals), 0.5) if intervals else None
    janky = 0
    for sample in samples:
        period = sample.interval_ms or fallback_interval
        if period is not None and sample.duration_ms > period * JANK_MULTIPLIER:
            janky += 1
    count = len(durations)
    return {
        "count": count,
        "p50": metrics_module.percentile(durations, 0.5),
        "p90": metrics_module.percentile(durations, 0.9),
        "p99": metrics_module.percentile(durations, 0.99),
        "jankRatioPct": janky / count * 100.0,
        "jankCount": janky,
        "refreshHz": (1000.0 / fallback_interval) if fallback_interval else None,
    }


def aggregate_from_samples(samples: list[FrameSample]) -> list[dict[str, Any]]:
    """把逐帧样本聚合成 5 个可入库行；无样本返回空列表。

    P50/P90/P99 与超时帧占比是会话级单样本结论（聚合字段填单值），
    ``app_frame_count`` 为实际采到的帧数，同时充当 judge 的样本下限依据。
    """
    stats = frame_stats(samples)
    if not stats:
        return []
    return [
        metrics_module.aggregate_row(METRIC_P50, [stats["p50"]]),
        metrics_module.aggregate_row(METRIC_P90, [stats["p90"]]),
        metrics_module.aggregate_row(METRIC_P99, [stats["p99"]]),
        metrics_module.aggregate_row(METRIC_JANK_RATIO, [stats["jankRatioPct"]]),
        metrics_module.aggregate_row(METRIC_COUNT, [float(stats["count"])]),
    ]


def write_frames_csv(path, samples: list[FrameSample]) -> Any:
    """把逐帧样本落盘为 artifacts CSV（vsync_id/耗时/刷新周期），供报告重建。"""
    from pathlib import Path

    path = Path(path)
    if not samples:
        return path
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["vsync_id", "duration_ms", "interval_ms", "completed_s"])
        for sample in samples:
            writer.writerow(
                [
                    sample.vsync_id,
                    round(sample.duration_ms, 3),
                    "" if sample.interval_ms is None else round(sample.interval_ms, 3),
                    "" if sample.completed_s is None else round(sample.completed_s, 6),
                ]
            )
    return path


# ---------- 轮询器：独立线程旁路采集，不持有 gate 设备权 ----------

DeviceCaller = Callable[..., Any]  # call_device(args, timeout_s) -> .stdout/.returncode


@dataclass
class FrameResult:
    """一次逐帧轮询的结论：样本、轮询/失败计数与 reset 是否成功。"""

    samples: list[FrameSample] = field(default_factory=list)
    rounds: int = 0
    errors: int = 0
    reset_ok: bool = False


class FramePoller:
    """逐帧轮询器：reset 清零后按间隔增量读取 framestats。

    与 SupplementPoller 同构（独立线程、单次 adb 短事务、不持 gate 设备权）；
    跨轮按 vsync id 去重，故每轮读到的重复帧不会重复计数。
    """

    def __init__(
        self,
        *,
        adb_path: str,
        serial: str,
        target: str,
        interval_s: float = DEFAULT_INTERVAL_S,
        call_device: DeviceCaller | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if interval_s <= 0:
            raise ValueError("interval_s 必须为正数")
        self.adb_path = adb_path
        self.serial = serial
        self.target = target
        self.interval_s = interval_s
        self._call = call_device or _default_call_device(adb_path)
        self._sleep = sleep
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._result = FrameResult()
        self._seen: set[str] = set()

    @property
    def rounds(self) -> int:
        return self._result.rounds

    def start(self) -> None:
        """清零窗口并开始轮询（reset 失败只记录，不阻断采集）。"""
        if self._thread is not None:
            raise RuntimeError("poller already started")
        self._result.reset_ok = self._reset()
        self._thread = threading.Thread(
            target=self._run, name="mobile-perf-frames", daemon=True
        )
        self._thread.start()

    def stop(self, join_timeout_s: float = 30.0) -> FrameResult:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout_s)
            self._thread = None
        return FrameResult(
            samples=list(self._result.samples),
            rounds=self._result.rounds,
            errors=self._result.errors,
            reset_ok=self._result.reset_ok,
        )

    def _reset(self) -> bool:
        try:
            output = self._call(
                ["-s", self.serial, "shell", "dumpsys", "gfxinfo", self.target, "reset"],
                SHELL_TIMEOUT_S,
            )
        except Exception:  # noqa: BLE001 - reset 失败只影响窗口起点，不阻断
            return False
        return int(getattr(output, "returncode", 1)) == 0

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self._round()
            self._stop_event.wait(self.interval_s)

    def _round(self) -> None:
        self._result.rounds += 1
        try:
            output = self._call(self._round_args(), SHELL_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - 单轮失败只计数，不中断旁路采集
            self._result.errors += 1
            return
        if int(getattr(output, "returncode", 1)) != 0:
            self._result.errors += 1
            return
        for sample in parse_framestats(getattr(output, "stdout", "") or ""):
            if sample.vsync_id in self._seen:
                continue
            self._seen.add(sample.vsync_id)
            self._result.samples.append(sample)

    def _round_args(self) -> list[str]:
        return [
            "-s",
            self.serial,
            "shell",
            "dumpsys",
            "gfxinfo",
            self.target,
            "framestats",
        ]


def _default_call_device(adb_path: str) -> DeviceCaller:
    from .adb import default_call_device

    return default_call_device(adb_path)


def parse_uptime(text: str) -> float | None:
    """解析 ``/proc/uptime`` 的首列（设备已开机秒数，含小数）；不可读返回 None。"""
    for token in (text or "").split():
        try:
            value = float(token)
        except ValueError:
            continue
        # uptime 非负且远小于一年；其他形态（错误信息里的数字）一律不认
        if 0.0 <= value < 31_536_000.0:
            return value
    return None


def measure_clock_offset(
    call_device: DeviceCaller,
    serial: str,
    *,
    elapsed: Callable[[], float | None],
    timeout_s: float = SHELL_TIMEOUT_S,
) -> dict[str, Any] | None:
    """括号式测「设备时钟 → 会话时间轴」的偏移（帧时刻换算用，工单 19）。

    做法：读 ``/proc/uptime`` 前后各取一次会话相对秒，取中点与 uptime 相减。
    adb 往返延迟有几十毫秒量级，取中点能把它的**一半**消掉；不取中点就等于
    把整趟往返都算进偏移里。只测一次即可——同一会话内两条时钟的漂移远小于
    本指标的判据粒度（300 ms 静默窗口）。

    返回 ``{"offsetS": 偏移, "uptimeS": 设备 uptime, "rttMs": 往返耗时}``；
    命令失败或 uptime 不可读返回 None（调用方据此如实降级，不猜）。
    """
    before = elapsed()
    try:
        output = call_device(["-s", serial, "shell", "cat", UPTIME_PATH], timeout_s)
    except Exception:  # noqa: BLE001 - 测不到偏移就不换算
        return None
    after = elapsed()
    if int(getattr(output, "returncode", 1)) != 0:
        return None
    uptime_s = parse_uptime(getattr(output, "stdout", "") or "")
    if uptime_s is None or before is None or after is None:
        return None
    return {
        "offsetS": (before + after) / 2.0 - uptime_s,
        "uptimeS": uptime_s,
        "rttMs": round((after - before) * 1000.0, 3),
    }
