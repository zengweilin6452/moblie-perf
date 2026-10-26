"""PC 侧补采（supplement）：温度/GPU 等 SoloPi 未覆盖指标的 dumpsys 轮询（§4 #8）。

旁路采集：每轮一条独立 adb shell 短命令，不持有 gate 设备权、不影响 SoloPi
采集；设备采样时间经 keeper 的 device-host 时钟偏移换算到统一时间轴后入库
（source=pc_supplement，canonical key：temp_battery_c / gpu_util_pct /
gpu_mem_mb）。真机不支持某项时如实降级（degraded），不硬造数据。

**GPU 利用率走三级降级链**（工单 16）：

1. `kgsl gpubusy` 比值——Adreno 机型在 shell 用户下可读、无需 root；
2. `dumpsys gpu` 文本里的利用率行（原实现，多数机型没有）；
3. 都拿不到 → `not_tested`，并把每一级的失败原因带进降级事件。

`gpubusy` 是驱动侧约 1 s 滚动窗口的**累计**值（`total` 在 1.00e6–1.04e6 间浮动
且会回落），比值得出的是窗口内平均占用率，**不是瞬时值**。因此只报聚合
（median/p90/max），不承诺逐点序列与真实瞬时负载对齐。

`gpu_mem_mb` 取 `dumpsys gpu` 的 `Proc <pid> total` 字节数（按 1024² 换算）。
pid 由 `pidof <pkg>` 解析：**恰好一个**才采用，零个或多个一律不猜，如实降级。
"""

from __future__ import annotations

import csv
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import metrics as metrics_module
from .adb import default_call_device

TEMPERATURE_RE = re.compile(r"temperature:\s*(-?\d+)", re.IGNORECASE)

# 利用率行形态：行内同时出现 gpu 与 util 词根，行尾是（可选 % 的）数值；
# 厂商注释段（如 "(last 1s)"）不参与匹配。>100 视为无效。
GPU_UTIL_LINE_RE = re.compile(r"gpu.*util", re.IGNORECASE)
GPU_UTIL_VALUE_RE = re.compile(r"(\d{1,3})\s*%?\s*$")

# kgsl 窗口计数器：一行两个十进制整数 `busy total`
GPUBUSY_PATH = "/sys/class/kgsl/kgsl-3d0/gpubusy"
GPUBUSY_RE = re.compile(r"^(\d+)\s+(\d+)$")

# `dumpsys gpu` 的逐进程显存行：`Proc 23286 total: 131457024`
GPU_PROC_TOTAL_RE = re.compile(r"^\s*Proc\s+(\d+)\s+total:\s*(\d+)\s*$", re.MULTILINE)

# 轮询命令的分段标记：`echo <MARKER>:<section>`，避免把 cat/dumpsys 的输出
# 互相串味（例如 dumpsys 里偶然出现的 "temperature:" 被当成电池温度）
SECTION_MARKER = "__mp_section__"
GPUBUSY_SECTION = "gpubusy"
BATTERY_SECTION = "battery"
GPU_SECTION = "gpu"
PID_SECTION = "pid"

# 降级原因（如实记录"哪一级拿不到"）
REASON_KGSL_NOT_READABLE = "kgsl_not_readable"
REASON_KGSL_UNPARSEABLE = "kgsl_unparseable"
REASON_PID_UNAVAILABLE = "pid_unavailable"
REASON_PID_AMBIGUOUS = "pid_ambiguous"
REASON_PROC_NOT_FOUND = "proc_not_found"

SOURCE_KGSL = "kgsl_gpubusy"
SOURCE_DUMPSYS = "dumpsys_gpu"


def parse_battery_temperature(stdout: str) -> float | None:
    """从 `dumpsys battery` 输出提取电池温度（字段值×0.1 = °C）；解析不了返回 None。"""
    match = TEMPERATURE_RE.search(stdout or "")
    return int(match.group(1)) / 10.0 if match else None


def parse_gpu_utilization(stdout: str) -> float | None:
    """从 `dumpsys gpu` 输出提取 GPU 利用率（%）；无利用率行（机型不支持）返回 None。"""
    for line in (stdout or "").splitlines():
        stripped = line.strip()
        if not GPU_UTIL_LINE_RE.search(stripped):
            continue
        match = GPU_UTIL_VALUE_RE.search(stripped)
        if match:
            value = int(match.group(1))
            if value <= 100:
                return float(value)
    return None


def parse_gpubusy(stdout: str) -> float | None:
    """把 kgsl `gpubusy` 的 `busy total` 换算成窗口平均占用率（%）。

    四种分支：正常值 → 比值；`0 0`（该窗口 GPU 空闲）→ 0.0；
    `total` 为 0 但 `busy` 非 0（驱动窗口刚重置）→ None，不硬算；
    格式异常（空输出、非数字、多行噪声）→ None。>100% 同样视为异常。
    """
    for line in (stdout or "").splitlines():
        match = GPUBUSY_RE.match(line.strip())
        if not match:
            continue
        busy, total = int(match.group(1)), int(match.group(2))
        if total == 0:
            return 0.0 if busy == 0 else None
        pct = busy * 100.0 / total
        return pct if 0.0 <= pct <= 100.0 else None
    return None


def parse_gpu_proc_mem_mb(stdout: str, pid: int) -> float | None:
    """从 `dumpsys gpu` 取指定 pid 的显存（MB，1024² 换算）；该 pid 无记录返回 None。

    只认 `Proc <pid> total:` 这一行——`Global total` 是所有进程之和，取错会把
    全机显存算成应用显存。
    """
    for match in GPU_PROC_TOTAL_RE.finditer(stdout or ""):
        if int(match.group(1)) == pid:
            return int(match.group(2)) / (1024.0 * 1024.0)
    return None


def parse_pidof(stdout: str) -> int | None:
    """`pidof <pkg>` 输出**恰好一个** pid 时返回它；零个或多个一律返回 None。

    多进程应用（`:push`、`:remote` 等）会有多个 pid，此时无法确定哪个是主进程
    的显存，取错进程比不取更糟——如实降级。
    """
    pids = [token for token in (stdout or "").split() if token.isdigit()]
    return int(pids[0]) if len(pids) == 1 else None


def split_sections(stdout: str) -> dict[str, str]:
    """按 `__mp_section__:<name>` 标记把一次轮询的输出切成各段。

    标记之前的噪声（如 adb 的提示行）丢弃；同名段重复出现时后者覆盖前者。
    """
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in (stdout or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{SECTION_MARKER}:"):
            current = stripped.split(":", 1)[1].strip()
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return {name: "\n".join(lines) for name, lines in sections.items()}


def parse_device_epoch(stdout: str) -> float | None:
    """轮询命令输出取设备 epoch（`date +%s` 位于首行）；首行不是纯数字返回 None。"""
    for line in (stdout or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        return float(stripped) if stripped.isdigit() else None
    return None


def to_unified_elapsed_s(
    *, device_epoch_s: float, clock_offset_s: float, session_start_host_epoch_s: float
) -> float:
    """设备采样时间换算到统一时间轴：epoch + offset(host-device) − 会话起点（秒）。"""
    return device_epoch_s + clock_offset_s - session_start_host_epoch_s


# ---------- 轮询器：独立线程旁路采集，不持有 gate 设备权 ----------

DEFAULT_INTERVAL_S = 2.0
SHELL_TIMEOUT_S = 10.0

DeviceCaller = Callable[..., Any]  # call_device(args, timeout_s) -> .stdout/.returncode


@dataclass
class SupplementResult:
    """一次补采轮询的完整结论：样本、降级项与轮询/失败计数。

    ``degraded`` 每项含 ``key`` / ``reason``（终态原因码）与 ``stages``
    （降级链逐级原因，如 ``["kgsl:not_readable", "dumpsys:unparseable"]``），
    便于事后区分"这台机器根本没有这个源"与"有这个源但这次读失败"。
    """

    samples: list[dict[str, Any]] = field(default_factory=list)
    degraded: list[dict[str, Any]] = field(default_factory=list)
    rounds: int = 0
    errors: int = 0
    gpu_source: str | None = None


class SupplementPoller:
    """温度/GPU 补采轮询器。

    独立线程执行，每轮一条 adb shell 短命令（单次 adb 事务，不持 gate 设备权，
    与 logcat 旁路监听同理），命令内用段标记把 gpubusy / battery / gpu / pid
    四段输出分开；采样时间用 keeper 的时钟偏移换算到统一时间轴。
    start/stop 幂等配对；rounds 供宿主观测轮询进度。
    """

    def __init__(
        self,
        *,
        adb_path: str,
        serial: str,
        interval_s: float = DEFAULT_INTERVAL_S,
        clock_offset_s: float,
        collect_temperature: bool = True,
        collect_gpu: bool = True,
        collect_gpu_mem: bool = True,
        target_pkg: str | None = None,
        call_device: DeviceCaller | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
    ):
        if interval_s <= 0:
            raise ValueError("interval_s 必须为正数")
        self.adb_path = adb_path
        self.serial = serial
        self.interval_s = interval_s
        self.clock_offset_s = clock_offset_s
        self.collect_temperature = collect_temperature
        self.collect_gpu = collect_gpu
        self.collect_gpu_mem = collect_gpu_mem
        self.target_pkg = target_pkg
        self._call = call_device or default_call_device(adb_path)
        self._sleep = sleep
        self._now = now
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._result = SupplementResult()
        # 逐级降级原因累计（整个会话出现过即记，避免最后一轮偶然成功掩盖前几轮）
        self._stages: dict[str, list[str]] = {
            "gpu_util_pct": [],
            "gpu_mem_mb": [],
        }

    @property
    def rounds(self) -> int:
        """已完成的轮询轮数。"""
        return self._result.rounds

    def _round_args(self) -> list[str]:
        """一次 adb 事务取全部补采项：段标记 + 各自的短命令。"""
        args = ["-s", self.serial, "shell", "date", "+%s"]
        if self.collect_gpu:
            # kgsl 窗口计数器：非 Adreno 机型路径不存在，2>/dev/null 让该段为空
            args += [
                ";", "echo", f"{SECTION_MARKER}:{GPUBUSY_SECTION}",
                ";", "cat", GPUBUSY_PATH, "2>/dev/null",
            ]
        if self.collect_temperature:
            args += [";", "echo", f"{SECTION_MARKER}:{BATTERY_SECTION}", ";", "dumpsys", "battery"]
        if self.collect_gpu or self.collect_gpu_mem:
            args += [";", "echo", f"{SECTION_MARKER}:{GPU_SECTION}", ";", "dumpsys", "gpu"]
        if self.collect_gpu_mem and self.target_pkg:
            args += [";", "echo", f"{SECTION_MARKER}:{PID_SECTION}", ";", "pidof", self.target_pkg]
        return args

    def start(self) -> None:
        """开始轮询；宿主会话起点由本次调用的宿主时钟即取（紧随 session.start 调用）。"""
        if self._thread is not None:
            raise RuntimeError("poller already started")
        self._start_wall = self._now()
        self._thread = threading.Thread(
            target=self._run, name="mobile-perf-supplement", daemon=True
        )
        self._thread.start()

    def stop(self, join_timeout_s: float = 30.0) -> SupplementResult:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout_s)
            self._thread = None
        return self._final_result()

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
        stdout = getattr(output, "stdout", "") or ""
        sections = split_sections(stdout)
        epoch = parse_device_epoch(stdout)
        elapsed = (
            to_unified_elapsed_s(
                device_epoch_s=epoch,
                clock_offset_s=self.clock_offset_s,
                session_start_host_epoch_s=self._start_wall,
            )
            if epoch is not None
            else self._now() - self._start_wall  # date 不可读时退回宿主侧计时
        )
        gpu_util, gpu_source = self._read_gpu_util(sections) if self.collect_gpu else (None, None)
        self._result.samples.append(
            {
                "elapsed_s": round(elapsed, 3),
                "device_epoch_s": epoch,
                "temp_battery_c": (
                    parse_battery_temperature(sections.get(BATTERY_SECTION, ""))
                    if self.collect_temperature
                    else None
                ),
                "gpu_util_pct": gpu_util,
                "gpu_util_source": gpu_source,
                "gpu_mem_mb": self._read_gpu_mem(sections) if self.collect_gpu_mem else None,
            }
        )

    def _read_gpu_util(self, sections: dict[str, str]) -> tuple[float | None, str | None]:
        """三级降级链：kgsl gpubusy 比值 → dumpsys gpu 文本 → None。"""
        kgsl_stdout = sections.get(GPUBUSY_SECTION, "")
        value = parse_gpubusy(kgsl_stdout)
        if value is not None:
            return value, SOURCE_KGSL
        self._note_stage(
            "gpu_util_pct",
            f"kgsl:{REASON_KGSL_UNPARSEABLE if kgsl_stdout.strip() else REASON_KGSL_NOT_READABLE}",
        )
        value = parse_gpu_utilization(sections.get(GPU_SECTION, ""))
        if value is not None:
            return value, SOURCE_DUMPSYS
        self._note_stage("gpu_util_pct", "dumpsys:unparseable")
        return None, None

    def _read_gpu_mem(self, sections: dict[str, str]) -> float | None:
        """按目标包名的唯一 pid 取 `Proc <pid> total`；pid 不唯一或缺失即降级。"""
        if not self.target_pkg:
            self._note_stage("gpu_mem_mb", REASON_PID_UNAVAILABLE)
            return None
        pid_stdout = sections.get(PID_SECTION, "")
        pid = parse_pidof(pid_stdout)
        if pid is None:
            reason = REASON_PID_AMBIGUOUS if pid_stdout.split() else REASON_PID_UNAVAILABLE
            self._note_stage("gpu_mem_mb", reason)
            return None
        value = parse_gpu_proc_mem_mb(sections.get(GPU_SECTION, ""), pid)
        if value is None:
            self._note_stage("gpu_mem_mb", REASON_PROC_NOT_FOUND)
        return value

    def _note_stage(self, key: str, stage: str) -> None:
        stages = self._stages.setdefault(key, [])
        if stage not in stages:
            stages.append(stage)

    def _final_result(self) -> SupplementResult:
        result = SupplementResult(
            samples=list(self._result.samples),
            rounds=self._result.rounds,
            errors=self._result.errors,
        )
        sources = {s.get("gpu_util_source") for s in result.samples} - {None}
        result.gpu_source = sources.pop() if len(sources) == 1 else None
        result.degraded = self._collect_degraded(result)
        return result

    def _collect_degraded(self, final: SupplementResult) -> list[dict[str, Any]]:
        """启用但整轮未取到样本的项如实降级（不支持/命令失败），不硬造数据。"""
        degraded: list[dict[str, Any]] = []
        all_failed = final.rounds > 0 and final.errors == final.rounds
        for enabled, key in (
            (self.collect_temperature, "temp_battery_c"),
            (self.collect_gpu, "gpu_util_pct"),
            (self.collect_gpu_mem, "gpu_mem_mb"),
        ):
            if not enabled:
                continue
            if all_failed:
                reason = "adb_command_failed"
            elif any(sample.get(key) is not None for sample in final.samples):
                continue
            elif final.rounds > 0:
                reason = "output_unparseable_or_unsupported"
            else:
                continue
            entry: dict[str, Any] = {"key": key, "reason": reason}
            stages = self._stages.get(key) or []
            if stages:
                entry["stages"] = list(stages)
            degraded.append(entry)
        return degraded


# ---------- 入库前整理：样本落盘与聚合（与 collector 聚合行同形状） ----------

SAMPLE_KEYS = ("temp_battery_c", "gpu_util_pct", "gpu_mem_mb")


def write_supplement_csv(path: str | Path, samples: list[dict[str, Any]]) -> Path:
    """把补采样本写成 artifacts CSV：elapsed_s 列 + canonical key 列。

    落盘只为可追溯与报告重建（report.py 按 canonical key 吸收）；无样本不写文件。
    """
    path = Path(path)
    if not samples:
        return path
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["elapsed_s", *SAMPLE_KEYS])
        for sample in samples:
            writer.writerow(
                [str(sample["elapsed_s"])]
                + ["" if sample.get(key) is None else sample[key] for key in SAMPLE_KEYS]
            )
    return path


def aggregate_samples(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把补采样本聚合成 store.record_metrics 可吃的行；无样本的指标不产出行。"""
    aggregates: list[dict[str, Any]] = []
    for key in SAMPLE_KEYS:
        values = [sample[key] for sample in samples if sample.get(key) is not None]
        if not values:
            continue
        aggregates.append(metrics_module.aggregate_row(key, values))
    return aggregates
