"""PC 侧补采（supplement）：温度/GPU 等 SoloPi 未覆盖指标的 dumpsys 轮询（§4 #8）。

旁路采集：每轮一条独立 adb shell 短命令，不持有 gate 设备权、不影响 SoloPi
采集；设备采样时间经 keeper 的 device-host 时钟偏移换算到统一时间轴后入库
（source=pc_supplement，canonical key：temp_battery_c / gpu_util_pct）。
真机不支持某项时如实降级（degraded），不硬造数据。
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

from . import adb as adb_module
from . import metrics as metrics_module

TEMPERATURE_RE = re.compile(r"temperature:\s*(-?\d+)", re.IGNORECASE)

# 利用率行形态：行内同时出现 gpu 与 util 词根，行尾是（可选 % 的）数值；
# 厂商注释段（如 "(last 1s)"）不参与匹配。>100 视为无效。
GPU_UTIL_LINE_RE = re.compile(r"gpu.*util", re.IGNORECASE)
GPU_UTIL_VALUE_RE = re.compile(r"(\d{1,3})\s*%?\s*$")


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


def default_call_device(adb_path: str) -> DeviceCaller:
    """默认 adb 调用：每轮一条独立短命令（与 keeper 同构，不经 gate）。"""

    def call(args: list[str], timeout_s: float = SHELL_TIMEOUT_S) -> Any:
        return adb_module._adb_runner([adb_path, *args], timeout_s)

    return call


@dataclass
class SupplementResult:
    """一次补采轮询的完整结论：样本、降级项与轮询/失败计数。"""

    samples: list[dict[str, Any]] = field(default_factory=list)
    degraded: list[dict[str, Any]] = field(default_factory=list)
    rounds: int = 0
    errors: int = 0


class SupplementPoller:
    """温度/GPU 补采轮询器。

    独立线程执行，每轮一条 `date +%s; dumpsys battery; dumpsys gpu` 短命令
    （单次 adb 事务，不持 gate 设备权，与 logcat 旁路监听同理）；采样时间用
    keeper 的时钟偏移换算到统一时间轴。start/stop 幂等配对；rounds 供宿主
    观测轮询进度。
    """

    def __init__(
        self,
        *,
        adb_path: str,
        serial: str,
        interval_s: float = DEFAULT_INTERVAL_S,
        clock_offset_s: float,
        session_start_host_epoch_s: float,
        collect_temperature: bool = True,
        collect_gpu: bool = True,
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
        self.session_start_host_epoch_s = session_start_host_epoch_s
        self.collect_temperature = collect_temperature
        self.collect_gpu = collect_gpu
        self._call = call_device or default_call_device(adb_path)
        self._sleep = sleep
        self._now = now
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._result = SupplementResult()

    @property
    def rounds(self) -> int:
        """已完成的轮询轮数。"""
        return self._result.rounds

    def _round_args(self) -> list[str]:
        args = ["-s", self.serial, "shell", "date", "+%s"]
        if self.collect_temperature:
            args += [";", "dumpsys", "battery"]
        if self.collect_gpu:
            args += [";", "dumpsys", "gpu"]
        return args

    def start(self) -> None:
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
        stdout = getattr(output, "stdout", "") or ""
        epoch = parse_device_epoch(stdout)
        elapsed = (
            to_unified_elapsed_s(
                device_epoch_s=epoch,
                clock_offset_s=self.clock_offset_s,
                session_start_host_epoch_s=self.session_start_host_epoch_s,
            )
            if epoch is not None
            else self._now() - self._start_wall  # date 不可读时退回宿主侧计时
        )
        self._result.samples.append(
            {
                "elapsed_s": round(elapsed, 3),
                "device_epoch_s": epoch,
                "temp_battery_c": (
                    parse_battery_temperature(stdout) if self.collect_temperature else None
                ),
                "gpu_util_pct": parse_gpu_utilization(stdout) if self.collect_gpu else None,
            }
        )

    def _final_result(self) -> SupplementResult:
        result = SupplementResult(
            samples=list(self._result.samples),
            rounds=self._result.rounds,
            errors=self._result.errors,
        )
        result.degraded = self._collect_degraded(result)
        return result

    def _collect_degraded(self, final: SupplementResult) -> list[dict[str, Any]]:
        """启用但整轮未取到样本的项如实降级（不支持/命令失败），不硬造数据。"""
        degraded: list[dict[str, Any]] = []
        all_failed = final.rounds > 0 and final.errors == final.rounds
        for enabled, key in (
            (self.collect_temperature, "temp_battery_c"),
            (self.collect_gpu, "gpu_util_pct"),
        ):
            if not enabled:
                continue
            if all_failed:
                reason = "adb_command_failed"
            elif any(sample[key] is not None for sample in final.samples):
                continue
            elif final.rounds > 0:
                reason = "output_unparseable_or_unsupported"
            else:
                continue
            degraded.append({"key": key, "reason": reason})
        return degraded


# ---------- 入库前整理：样本落盘与聚合（与 collector 聚合行同形状） ----------

SAMPLE_KEYS = ("temp_battery_c", "gpu_util_pct")


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


def _percentile(sorted_values: list[float], ratio: float) -> float:
    """线性插值百分位（与 collector._percentile 同口径；collector 不在本票边界内）。"""
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = ratio * (len(sorted_values) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = rank - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction


def aggregate_samples(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把补采样本聚合成 store.record_metrics 可吃的行；无样本的指标不产出行。"""
    aggregates: list[dict[str, Any]] = []
    for key in SAMPLE_KEYS:
        values = sorted(sample[key] for sample in samples if sample.get(key) is not None)
        if not values:
            continue
        spec = metrics_module.METRICS[key]
        count = len(values)
        aggregates.append(
            {
                "metric_key": key,
                "unit": spec.unit,
                "source": spec.source,
                "min": values[0],
                "median": _percentile(values, 0.5),
                "mean": sum(values) / count,
                "max": values[-1],
                "p90": _percentile(values, 0.9),
                "sample_count": count,
            }
        )
    return aggregates
