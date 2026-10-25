"""logwatch：采集期间旁路监听 logcat 的 crash/ANR，事件入统一时间轴（设计方案 §4 #7）。

设计约束：
- 监听是旁路进程：`adb logcat` 子进程常驻读取，不持有 gate 设备权（不占用短事务
  借用窗口），采集 stop 时干净终止（terminate → wait → kill 兜底，不悬挂）；
- 行解析是纯函数（LogcatParser）：main buffer 的 FATAL EXCEPTION/AndroidRuntime
  堆栈与 events buffer 的 am_crash/am_anr 多路匹配；目标包过滤可配（packages），
  识别不出包名的事件保留（不猜包归属，如实入库供人工判断）；
- 时间轴：logcat 用 `-v threadtime,epoch` 取设备 epoch 秒，convert_ts 用 keeper
  校准的 device-host 偏移换算到宿主时间轴；偏移未校准时按偏移 0 换算并在
  detail.tsSource 如实标记（不冒充已校准）。
"""

from __future__ import annotations

import re
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

STACK_MAX_LINES = 60
STACK_DETAIL_MAX = 30
CRASH_DEDUPE_WINDOW_S = 10.0

CRASH_SOURCE_MAIN = "logcat_main"
CRASH_SOURCE_EVENTS = "logcat_events"

_EPOCH_LINE_RE = re.compile(
    r"^\s*(\d{9,13}\.\d{1,3})\s+(\d+)\s+(\d+)\s+([VDIWEF])\s+(\S+?):\s(.*)$"
)
_DATE_LINE_RE = re.compile(
    r"^\s*(\d{2})-(\d{2})\s(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s+(\d+)\s+(\d+)\s+([VDIWEF])\s+(\S+?):\s(.*)$"
)
_PACKAGE_RE = re.compile(r"\b([a-zA-Z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)\b")
_ANR_PACKAGE_RE = re.compile(r"ANR in ([a-zA-Z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)")
_BRACKET_RE = re.compile(r"\[(.*)\]\s*$")
_PROCESS_RE = re.compile(r"Process:\s*([a-zA-Z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)")

PackageFilter = list[str] | None


@dataclass(frozen=True)
class LogcatLine:
    """一行 logcat（threadtime 格式）的解析结果；epoch 解析不出时为 None。"""

    pid: int
    tid: int
    level: str
    tag: str
    message: str
    epoch_s: float | None = None
    device_local: tuple[int, ...] | None = None  # date 形式的 (月,日,时,分,秒,毫秒)


def parse_logcat_line(line: str) -> LogcatLine | None:
    """解析一行 logcat threadtime 输出；非日志行（分隔线、空行）返回 None。"""
    match = _EPOCH_LINE_RE.match(line)
    if match:
        epoch_s, pid, tid, level, tag, message = match.groups()
        return LogcatLine(int(pid), int(tid), level, tag, message, epoch_s=float(epoch_s))
    match = _DATE_LINE_RE.match(line)
    if match:
        month, day, hh, mm, ss, mmm, pid, tid, level, tag, message = match.groups()
        local = (int(month), int(day), int(hh), int(mm), int(ss), int(mmm))
        return LogcatLine(int(pid), int(tid), level, tag, message, device_local=local)
    return None


@dataclass
class _PendingCrash:
    """FATAL EXCEPTION 之后累积 AndroidRuntime 堆栈的暂存候选。"""

    pid: int
    first_line: str
    epoch_s: float | None
    stack: list[str] = field(default_factory=list)

    def stack_lines(self) -> list[str]:
        return [self.first_line, *self.stack]


@dataclass(frozen=True)
class RawEvent:
    """解析器产出的 crash/anr 候选事件（入库前再换算 ts、过滤与去重）。"""

    kind: str  # "crash" | "anr"
    source: str  # CRASH_SOURCE_MAIN | CRASH_SOURCE_EVENTS
    message: str
    pid: int | None = None
    package: str | None = None
    device_epoch_s: float | None = None
    stack: tuple[str, ...] = ()


def _event_dict(event: RawEvent) -> dict[str, Any]:
    return {
        "kind": event.kind,
        "source": event.source,
        "message": event.message,
        "pid": event.pid,
        "package": event.package,
        "device_epoch_s": event.device_epoch_s,
        "stack": list(event.stack),
    }


def _package_from_bracket(message: str) -> str | None:
    """events buffer 的 am_crash/am_anr 消息：`[user,pid,package,...]` 防御式取包名。"""
    bracket = _BRACKET_RE.search(message)
    if not bracket:
        return None
    match = _PACKAGE_RE.search(bracket.group(1))
    return match.group(1) if match else None


def _package_from_pid_field(message: str) -> int | None:
    """events buffer 括号字段里取崩溃/ANR 进程 pid。

    字段顺序版本相关（老格式 [user,pid,...]，MIUI 实测 [pid,user,...]）：
    Linux pid 恒 ≥1000，取前两个字段中第一个 ≥1000 的数字段；都不满足返回 None。
    """
    bracket = _BRACKET_RE.search(message)
    if not bracket:
        return None
    fields = [f.strip() for f in bracket.group(1).split(",")]
    for value in fields[:2]:
        if value.isdigit() and int(value) >= 1000:
            return int(value)
    return None


def filter_by_packages(
    events: list[dict[str, Any]], packages: PackageFilter
) -> list[dict[str, Any]]:
    """目标包过滤：packages 为 None 不过滤；识别不出包名的事件保留（不猜归属）。"""
    if packages is None:
        return list(events)
    return [
        event
        for event in events
        if event.get("package") is None or event["package"] in packages
    ]


def merge_duplicates(
    events: list[dict[str, Any]], window_s: float = CRASH_DEDUPE_WINDOW_S
) -> list[dict[str, Any]]:
    """同 (kind, pid) 且设备时间差在窗口内的事件合并为一条。

    同一次 crash 会同时命中 main buffer（FATAL EXCEPTION，带堆栈）与 events
    buffer（am_crash，带包名）；合并时以带包名的一方为基准，堆栈取并集保序。
    """
    merged: list[dict[str, Any]] = []
    for event in events:
        duplicate = None
        for prior in merged:
            if prior["kind"] != event["kind"] or prior.get("pid") != event.get("pid"):
                continue
            base_epoch = event.get("device_epoch_s")
            prior_epoch = prior.get("device_epoch_s")
            if base_epoch is not None and prior_epoch is not None:
                if abs(base_epoch - prior_epoch) <= window_s:
                    duplicate = prior
                    break
        if duplicate is None:
            merged.append(dict(event))
            continue
        if event.get("package") and not duplicate.get("package"):
            keep, other = event, duplicate
            merged[merged.index(duplicate)] = keep
        else:
            keep, other = duplicate, event
        if not keep.get("package") and other.get("package"):
            keep["package"] = other["package"]
        stack = list(keep.get("stack") or [])
        stack.extend(line for line in (other.get("stack") or []) if line not in stack)
        keep["stack"] = stack
    return merged


def _host_epoch_to_iso(epoch_s: float) -> str:
    return datetime.fromtimestamp(epoch_s).astimezone().isoformat(timespec="milliseconds")


def convert_ts(
    device_epoch_s: float, clock_offset_s: float | None
) -> tuple[str, str]:
    """设备 epoch 秒 → 宿主时间轴 ISO 串与来源标记。

    keeper 的偏移定义是 host - device（epoch 秒），直接相加即宿主时间轴；
    偏移未校准时按 0 换算（设备与宿主通常同区），并如实标 uncalibrated。
    """
    offset = clock_offset_s if clock_offset_s is not None else 0.0
    if clock_offset_s is not None:
        source = "device_epoch_calibrated"
    else:
        source = "device_epoch_uncalibrated"
    return _host_epoch_to_iso(device_epoch_s + offset), source


class LogcatParser:
    """logcat 行流 → crash/anr 事件候选的纯状态机（不碰子进程与设备）。

    feed() 逐行喂入（main 与 events buffer 混流即可），返回由此定稿的候选；
    FATAL EXCEPTION 后同 pid 同 tag 的行累积为堆栈，遇其他行或堆栈行数到上限即定稿。
    """

    def __init__(self, stack_max: int = STACK_MAX_LINES):
        self._stack_max = stack_max
        self._pending: _PendingCrash | None = None

    def feed(self, line: str) -> list[RawEvent]:
        emitted: list[RawEvent] = []
        if self._pending is not None:
            parsed = parse_logcat_line(line)
            is_stack_line = (
                parsed is not None
                and parsed.pid == self._pending.pid
                and parsed.tag == "AndroidRuntime"
            )
            if is_stack_line and len(self._pending.stack) < self._stack_max:
                self._pending.stack.append(line.rstrip("\n"))
                return emitted
            emitted.append(_event_dict(self._finalize_pending()))
        emitted.extend(self._classify(line))
        return emitted

    def flush(self) -> list[RawEvent]:
        if self._pending is None:
            return []
        return [_event_dict(self._finalize_pending())]

    def _finalize_pending(self) -> RawEvent:
        pending = self._pending
        self._pending = None
        assert pending is not None
        package = None
        for raw in reversed(pending.stack_lines()):
            match = _PROCESS_RE.search(raw)
            if match:
                package = match.group(1)
                break
        return RawEvent(
            kind="crash",
            source=CRASH_SOURCE_MAIN,
            message=pending.first_line.split(": ", 1)[-1],
            pid=pending.pid,
            package=package,
            device_epoch_s=pending.epoch_s,
            stack=tuple(pending.stack_lines()),
        )

    def _classify(self, line: str) -> list[RawEvent]:
        parsed = parse_logcat_line(line)
        if parsed is None:
            return []
        message = parsed.message
        if "FATAL EXCEPTION" in message:
            self._pending = _PendingCrash(
                pid=parsed.pid, first_line=line.rstrip("\n"), epoch_s=parsed.epoch_s
            )
            return []
        if parsed.tag == "am_crash":
            return [
                _event_dict(RawEvent(
                    kind="crash",
                    source=CRASH_SOURCE_EVENTS,
                    message=message,
                    pid=_package_from_pid_field(message),
                    package=_package_from_bracket(message),
                    device_epoch_s=parsed.epoch_s,
                ))
            ]
        if parsed.tag == "am_anr":
            return [
                _event_dict(RawEvent(
                    kind="anr",
                    source=CRASH_SOURCE_EVENTS,
                    message=message,
                    pid=_package_from_pid_field(message),
                    package=_package_from_bracket(message),
                    device_epoch_s=parsed.epoch_s,
                ))
            ]
        if "ANR in" in message:
            match = _ANR_PACKAGE_RE.search(message)
            return [
                _event_dict(RawEvent(
                    kind="anr",
                    source=CRASH_SOURCE_MAIN,
                    message=message,
                    pid=parsed.pid,
                    package=match.group(1) if match else None,
                    device_epoch_s=parsed.epoch_s,
                ))
            ]
        return []


# ---------- 旁路监听进程（不持有 gate 设备权） ----------

LOGCAT_ARGS = ["logcat", "-v", "threadtime,epoch", "-b", "main", "-b", "events", "-T", "1"]

Spawner = Callable[[list[str]], Any]


def default_spawn(adb_path: str) -> Spawner:
    """默认 spawner：`adb -s <serial> logcat ...` 常驻子进程，stdout 逐行读。"""

    def spawn(args: list[str]) -> Any:
        return subprocess.Popen(  # noqa: S603 - 命令由组件自身组装
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    return spawn


class LogWatch:
    """crash/ANR 旁路监听：logcat 子进程 + 读线程，stop 时干净终止。

    生命周期与采集会话绑定（由 kit 在 run_collect 内启动/停止）；不占用
    gate 借用窗口——宿主自动化可随时借设备。事件在 stop 时统一换算时间轴、
    去重、按包过滤，返回 store 就绪的 {kind, ts, detail} 列表。
    """

    def __init__(
        self,
        *,
        adb_path: str,
        serial: str,
        packages: PackageFilter = None,
        clock_offset_s: float | None = None,
        spawn: Spawner | None = None,
        stack_max: int = STACK_MAX_LINES,
    ):
        self._command = [adb_path, "-s", serial, *LOGCAT_ARGS]
        self._spawn = spawn or default_spawn(adb_path)
        self._packages = packages
        self._clock_offset_s = clock_offset_s
        self._parser = LogcatParser(stack_max)
        self._process: Any | None = None
        self._thread: threading.Thread | None = None
        self._events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("logwatch already started")
        self._process = self._spawn(self._command)
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_loop(self) -> None:
        try:
            stream = self._process.stdout if self._process is not None else []
            for line in stream:
                self._absorb(self._parser.feed(line))
        except Exception:  # noqa: BLE001 - 流随进程终止而关闭属正常结束
            pass
        self._absorb(self._parser.flush())

    def _absorb(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        with self._lock:
            self._events.extend(events)

    def stop(self, timeout_s: float = 5.0) -> list[dict[str, Any]]:
        """终止 logcat 子进程并回收读线程，返回最终事件（供入库）。"""
        if self._thread is None:
            raise RuntimeError("logwatch not started")
        process = self._process
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=timeout_s)
            except Exception:  # noqa: BLE001 - terminate 未生效时 kill 兜底
                try:
                    process.kill()
                    process.wait(timeout=timeout_s)
                except Exception:  # noqa: BLE001
                    pass
        self._thread.join(timeout=timeout_s)
        self._thread = None
        with self._lock:
            raw = list(self._events)
        return self._finalize(raw)

    def _finalize(self, raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kept = filter_by_packages(merge_duplicates(raw), self._packages)
        store_events = []
        for event in kept:
            epoch = event.get("device_epoch_s")
            if epoch is None:
                ts, ts_source = None, "unknown"
            else:
                ts, ts_source = convert_ts(epoch, self._clock_offset_s)
            store_events.append(
                {
                    "kind": event["kind"],
                    "ts": ts,
                    "detail": {
                        "package": event.get("package"),
                        "pid": event.get("pid"),
                        "source": event.get("source"),
                        "message": event.get("message"),
                        "stack": list(event.get("stack") or [])[:STACK_DETAIL_MAX],
                        "tsSource": ts_source,
                        "deviceEpochS": epoch,
                    },
                }
            )
        return store_events
