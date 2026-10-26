"""补采（supplement）单元测试：dumpsys 输出解析、对时换算、轮询生命周期与降级。"""

from mobile_perf import supplement


class TestParseGpubusy:
    """kgsl `gpubusy` 的 `busy total` 二元组 → 窗口平均占用率（%）。"""

    def test_ratio_is_percentage(self):
        assert supplement.parse_gpubusy("  280749 1008311\n") == 280749 * 100.0 / 1008311

    def test_zero_zero_is_idle_not_unknown(self):
        # total==0 且 busy==0 表示该窗口 GPU 空闲，是有效读数 0%，不是"读不到"
        assert supplement.parse_gpubusy("0 0\n") == 0.0

    def test_busy_with_zero_total_is_rejected(self):
        # 驱动窗口刚重置的瞬时态：busy 非 0 但 total 为 0，比值无意义 → 不硬算
        assert supplement.parse_gpubusy("123 0\n") is None

    def test_malformed_outputs_return_none(self):
        assert supplement.parse_gpubusy("") is None
        missing = "cat: /sys/class/kgsl/kgsl-3d0/gpubusy: No such file\n"
        assert supplement.parse_gpubusy(missing) is None
        assert supplement.parse_gpubusy("280749\n") is None  # 只有一个数
        assert supplement.parse_gpubusy("280749 1008311 42\n") is None  # 多一列

    def test_over_hundred_percent_rejected(self):
        # busy 大于 total 说明取到的不是同一窗口的两个计数，宁可降级也不报 100%+
        assert supplement.parse_gpubusy("200 100\n") is None


class TestParseGpuProcMem:
    """`dumpsys gpu` 的 `Proc <pid> total:` 字节数 → MB（1024² 换算）。"""

    def test_extracts_target_pid_in_mb(self):
        stdout = (
            "Global total: 400000000\n"
            "Proc 23286 total: 131457024\n"
            "Proc 30000 total: 1000000\n"
        )
        assert supplement.parse_gpu_proc_mem_mb(stdout, 23286) == 131457024 / (1024.0 * 1024.0)

    def test_global_total_is_not_taken_as_app_memory(self):
        # Global total 是全机之和，取错会把整机显存算成应用显存
        assert supplement.parse_gpu_proc_mem_mb("Global total: 400000000\n", 23286) is None

    def test_missing_pid_returns_none(self):
        assert supplement.parse_gpu_proc_mem_mb("Proc 1 total: 100\n", 23286) is None
        assert supplement.parse_gpu_proc_mem_mb("", 23286) is None


class TestParsePidof:
    """`pidof <pkg>` 恰好一个 pid 才采用；零个或多个都不猜。"""

    def test_single_pid_is_accepted(self):
        assert supplement.parse_pidof("23286\n") == 23286

    def test_no_pid_returns_none(self):
        assert supplement.parse_pidof("") is None
        assert supplement.parse_pidof("\n") is None

    def test_multiple_pids_are_ambiguous(self):
        # 多进程应用：取错进程比不取更糟
        assert supplement.parse_pidof("23286 23300\n") is None


class TestSplitSections:
    """轮询输出按段标记切分，避免 cat/dumpsys 的输出互相串味。"""

    def test_splits_named_sections(self):
        stdout = (
            "1792895592\n"
            "__mp_section__:gpubusy\n"
            "  280749 1008311\n"
            "__mp_section__:battery\n"
            "  temperature: 285\n"
            "__mp_section__:gpu\n"
            "Proc 23286 total: 131457024\n"
        )
        sections = supplement.split_sections(stdout)
        assert sections[supplement.GPUBUSY_SECTION].strip() == "280749 1008311"
        assert "temperature: 285" in sections[supplement.BATTERY_SECTION]
        assert "Proc 23286" in sections[supplement.GPU_SECTION]

    def test_empty_section_stays_empty(self):
        sections = supplement.split_sections(
            "__mp_section__:gpubusy\n__mp_section__:gpu\nProc 1 total: 2\n"
        )
        assert sections[supplement.GPUBUSY_SECTION].strip() == ""
        assert "Proc 1" in sections[supplement.GPU_SECTION]

    def test_text_before_first_marker_is_dropped(self):
        sections = supplement.split_sections("noise\n__mp_section__:battery\n  temperature: 285\n")
        assert sections == {supplement.BATTERY_SECTION: "  temperature: 285"}

    def test_no_markers_gives_no_sections(self):
        assert supplement.split_sections("temperature: 285\n") == {}


class TestParseBatteryTemperature:
    """dumpsys battery 的 temperature 字段：值×0.1 = °C；解析不了返回 None。"""

    def test_extracts_temperature_tenths_as_celsius(self):
        stdout = (
            "Current Battery Service state:\n"
            "  AC powered: false\n"
            "  status: 3\n"
            "  temperature: 285\n"
        )
        assert supplement.parse_battery_temperature(stdout) == 28.5

    def test_negative_temperature_supported(self):
        assert supplement.parse_battery_temperature("  temperature: -50\n") == -5.0

    def test_missing_field_returns_none(self):
        assert supplement.parse_battery_temperature("Current Battery Service state:\n") is None

    def test_empty_output_returns_none(self):
        assert supplement.parse_battery_temperature("") is None


class TestParseGpuUtilization:
    """`dumpsys gpu` 的利用率类指标；无该类输出（机型不支持）返回 None。"""

    def test_parses_gpu_utilization_line_with_percent(self):
        stdout = "GPU Utilization (last 1s): 34%\nFrequency: 500000\n"
        assert supplement.parse_gpu_utilization(stdout) == 34.0

    def test_parses_vendor_style_without_percent_sign(self):
        stdout = "gpu utilisation: 57\nvoltage: 0\n"
        assert supplement.parse_gpu_utilization(stdout) == 57.0

    def test_value_over_100_is_rejected(self):
        assert supplement.parse_gpu_utilization("GPU Utilization: 150%\n") is None

    def test_gles_only_output_returns_none(self):
        # 多数机型 dumpsys gpu 只有 GLES/驱动信息，无利用率行 → 不支持
        stdout = "GLES: OpenGL ES 3.2 V@0615.53\nDriver Version: 512502\n"
        assert supplement.parse_gpu_utilization(stdout) is None

    def test_empty_output_returns_none(self):
        assert supplement.parse_gpu_utilization("") is None


class TestParseDeviceEpoch:
    """轮询命令输出（date +%s 在首行）取设备 epoch；首行不是纯数字返回 None。"""

    def test_first_digit_line_is_epoch(self):
        stdout = "1792895592\nCurrent Battery Service state:\n  temperature: 285\n"
        assert supplement.parse_device_epoch(stdout) == 1792895592.0

    def test_missing_date_output_returns_none(self):
        assert supplement.parse_device_epoch("Current Battery Service state:\n") is None
        assert supplement.parse_device_epoch("") is None


class TestUnifiedTimeline:
    """对时：设备采样时间 + 时钟偏移(host-device) − 会话起点 = 统一时间轴秒。"""

    def test_converts_device_epoch_with_offset(self):
        elapsed = supplement.to_unified_elapsed_s(
            device_epoch_s=999.0, clock_offset_s=1.0, session_start_host_epoch_s=998.0
        )
        assert elapsed == 2.0

    def test_negative_offset_supported(self):
        elapsed = supplement.to_unified_elapsed_s(
            device_epoch_s=1000.0, clock_offset_s=-2.0, session_start_host_epoch_s=1000.0
        )
        assert elapsed == -2.0


class FakeCallDevice:
    """按轮次返回预制 stdout；耗尽后重复最后一份；可注入异常。"""

    def __init__(self, outputs, error_after=None):
        self.outputs = list(outputs)
        self.error_after = error_after
        self.calls = []

    def __call__(self, args, timeout_s):
        self.calls.append(list(args))
        if self.error_after is not None and len(self.calls) > self.error_after:
            raise OSError("adb not executable")
        index = min(len(self.calls) - 1, len(self.outputs) - 1)
        result = type("R", (), {})()
        result.stdout = self.outputs[index]
        result.returncode = 0
        return result


def combined_output(
    epoch,
    temperature=None,
    gpu=None,
    gpubusy=None,
    gpu_proc=None,
    pids=None,
    *,
    with_markers=True,
):
    """拼一次轮询的完整 stdout（段标记 + 各段内容）。

    gpubusy 传 "busy total" 字符串或 "raw:<原文>"（模拟文件不存在等异常输出）；
    gpu_proc 传 (pid, bytes) 生成 `Proc <pid> total:` 行；pids 传 pidof 的原文。
    """
    if not with_markers:
        # 兼容旧用例：无段标记的拼接输出（解析器此时取不到任何段）
        lines = [str(int(epoch))]
        if temperature is not None:
            lines += ["Current Battery Service state:", f"  temperature: {temperature}"]
        if gpu is not None:
            lines += [f"GPU Utilization (last 1s): {gpu}%"]
        return "\n".join(lines) + "\n"

    lines = [str(int(epoch))]
    if gpubusy is not None:
        lines.append(f"{supplement.SECTION_MARKER}:{supplement.GPUBUSY_SECTION}")
        if gpubusy.startswith("raw:"):
            lines.append(gpubusy[len("raw:"):])
        else:
            lines.append(f"  {gpubusy}")
    if temperature is not None:
        lines += [
            f"{supplement.SECTION_MARKER}:{supplement.BATTERY_SECTION}",
            "Current Battery Service state:",
            f"  temperature: {temperature}",
        ]
    gpu_lines = []
    if gpu is not None:
        gpu_lines.append(f"GPU Utilization (last 1s): {gpu}%")
    if gpu_proc is not None:
        gpu_lines.append(f"Proc {gpu_proc[0]} total: {gpu_proc[1]}")
    if gpu_lines:
        lines += [f"{supplement.SECTION_MARKER}:{supplement.GPU_SECTION}", *gpu_lines]
    if pids is not None:
        lines += [f"{supplement.SECTION_MARKER}:{supplement.PID_SECTION}", pids]
    return "\n".join(lines) + "\n"


def make_poller(fake_call, **kwargs):
    kwargs.setdefault("adb_path", "adb.exe")
    kwargs.setdefault("serial", "749622c9")
    kwargs.setdefault("interval_s", 0.2)
    kwargs.setdefault("clock_offset_s", 1.0)
    kwargs.setdefault("call_device", fake_call)
    kwargs.setdefault("sleep", lambda seconds: None)
    kwargs.setdefault("now", lambda: 1000.0)
    # 默认不采显存：多数用例只关心温度/利用率，显存单列一组用例
    kwargs.setdefault("collect_gpu_mem", False)
    return supplement.SupplementPoller(**kwargs)


def wait_until(predicate, timeout_s=5.0):
    import time as time_module

    deadline = time_module.monotonic() + timeout_s
    while time_module.monotonic() < deadline:
        if predicate():
            return True
        time_module.sleep(0.005)
    return False



class TestSupplementPoller:
    """轮询生命周期：统一时间轴采样、线程清理与不支持项如实降级。"""

    def test_samples_land_on_unified_timeline(self):
        call = FakeCallDevice(
            [
                combined_output(999, temperature=280, gpu=30),
                combined_output(1001, temperature=281, gpu=40),
            ]
        )
        poller = make_poller(call)
        poller.start()
        assert wait_until(lambda: poller.rounds == 2)
        result = poller.stop()

        assert [s["elapsed_s"] for s in result.samples] == [0.0, 2.0]
        assert [s["temp_battery_c"] for s in result.samples] == [28.0, 28.1]
        assert [s["gpu_util_pct"] for s in result.samples] == [30.0, 40.0]
        assert [s["device_epoch_s"] for s in result.samples] == [999.0, 1001.0]
        assert result.rounds == 2 and result.errors == 0
        assert result.degraded == []

    def test_stop_joins_thread_cleanly(self):
        import threading

        call = FakeCallDevice([combined_output(999, temperature=280)])
        poller = make_poller(call)
        before = threading.active_count()
        poller.start()
        assert wait_until(lambda: poller.rounds == 1)
        result = poller.stop()
        assert threading.active_count() == before
        assert result.rounds == 1
        # stop 后不再产生新样本
        assert len(result.samples) == 1
        assert len(call.calls) == 1

    def test_gpu_unsupported_degrades_honestly(self):
        call = FakeCallDevice(
            [combined_output(999, temperature=280), combined_output(1001, temperature=281)]
        )
        poller = make_poller(call)
        poller.start()
        assert wait_until(lambda: poller.rounds == 2)
        result = poller.stop()

        assert [s["gpu_util_pct"] for s in result.samples] == [None, None]
        assert len(result.degraded) == 1
        assert result.degraded[0]["key"] == "gpu_util_pct"
        assert result.degraded[0]["reason"] != ""

    def test_temperature_disabled_not_polled_not_degraded(self):
        call = FakeCallDevice([combined_output(999, gpu=30)])
        poller = make_poller(call, collect_temperature=False)
        poller.start()
        assert wait_until(lambda: poller.rounds == 1)
        result = poller.stop()

        assert result.samples[0]["temp_battery_c"] is None
        assert result.samples[0]["gpu_util_pct"] == 30.0
        assert result.degraded == []
        assert "dumpsys" in call.calls[0] and "battery" not in call.calls[0]
        assert "gpu" in call.calls[0]

    def test_adb_failure_degrades_all_without_raising(self):
        call = FakeCallDevice([], error_after=0)
        poller = make_poller(call)
        poller.start()
        assert wait_until(lambda: poller.rounds == 2)
        result = poller.stop()

        assert result.samples == []
        assert result.errors == 2
        keys = {entry["key"] for entry in result.degraded}
        assert keys == {"temp_battery_c", "gpu_util_pct"}

    def test_kgsl_source_wins_over_dumpsys(self):
        """三级降级链第一级：kgsl 可读时优先用它，dumpsys 的值不参与。"""
        call = FakeCallDevice(
            [combined_output(999, temperature=280, gpubusy="250000 1000000", gpu=99)]
        )
        poller = make_poller(call)
        poller.start()
        assert wait_until(lambda: poller.rounds == 1)
        result = poller.stop()

        assert result.samples[0]["gpu_util_pct"] == 25.0
        assert result.samples[0]["gpu_util_source"] == supplement.SOURCE_KGSL
        assert result.gpu_source == supplement.SOURCE_KGSL
        assert result.degraded == []

    def test_kgsl_missing_falls_back_to_dumpsys(self):
        """第二级：kgsl 路径不存在（非 Adreno）时落回 dumpsys gpu 文本。"""
        call = FakeCallDevice(
            [
                combined_output(
                    999, temperature=280,
                    gpubusy="raw:cat: ...: No such file or directory", gpu=42,
                )
            ]
        )
        poller = make_poller(call)
        poller.start()
        assert wait_until(lambda: poller.rounds == 1)
        result = poller.stop()

        assert result.samples[0]["gpu_util_pct"] == 42.0
        assert result.samples[0]["gpu_util_source"] == supplement.SOURCE_DUMPSYS
        assert result.gpu_source == supplement.SOURCE_DUMPSYS

    def test_both_sources_unavailable_records_each_stage(self):
        """第三级：两级都拿不到 → not_tested，且降级事件带逐级原因。"""
        # 非 Adreno 机型：kgsl 路径不存在且 stderr 被丢弃 → 该段为空
        call = FakeCallDevice([combined_output(999, temperature=280, gpubusy="")])
        poller = make_poller(call)
        poller.start()
        assert wait_until(lambda: poller.rounds == 1)
        result = poller.stop()

        assert result.samples[0]["gpu_util_pct"] is None
        assert result.gpu_source is None
        entry = next(item for item in result.degraded if item["key"] == "gpu_util_pct")
        assert entry["reason"] == "output_unparseable_or_unsupported"
        assert entry["stages"] == ["kgsl:kgsl_not_readable", "dumpsys:unparseable"]

    def test_kgsl_garbage_output_is_unparseable_not_unreadable(self):
        """有输出但不是 `busy total`：记为 unparseable，与"路径不存在"区分开。"""
        call = FakeCallDevice(
            [combined_output(999, temperature=280, gpubusy="raw:Permission denied")]
        )
        poller = make_poller(call)
        poller.start()
        assert wait_until(lambda: poller.rounds == 1)
        result = poller.stop()

        entry = next(item for item in result.degraded if item["key"] == "gpu_util_pct")
        assert entry["stages"][0] == "kgsl:kgsl_unparseable"

    def test_kgsl_idle_window_is_zero_not_degraded(self):
        """`0 0` 是有效读数（窗口内 GPU 空闲），不能当成"读不到"降级。"""
        call = FakeCallDevice([combined_output(999, temperature=280, gpubusy="0 0")])
        poller = make_poller(call)
        poller.start()
        assert wait_until(lambda: poller.rounds == 1)
        result = poller.stop()

        assert result.samples[0]["gpu_util_pct"] == 0.0
        assert result.degraded == []


class TestSupplementPollerGpuMem:
    """应用级显存（工单 16）：按目标包名的唯一 pid 取 `Proc <pid> total`。"""

    def _run(self, outputs, **kwargs):
        call = FakeCallDevice(outputs)
        kwargs.setdefault("collect_gpu_mem", True)
        kwargs.setdefault("target_pkg", "com.sinosoft.trainapp")
        poller = make_poller(call, **kwargs)
        poller.start()
        assert wait_until(lambda: poller.rounds >= 1)
        return poller.stop()

    def test_reads_target_process_memory(self):
        result = self._run(
            [
                combined_output(
                    999, temperature=280, gpubusy="0 0",
                    gpu_proc=(23286, 131457024), pids="23286\n",
                )
            ]
        )

        assert result.samples[0]["gpu_mem_mb"] == 131457024 / (1024.0 * 1024.0)
        assert result.degraded == []

    def test_ambiguous_pid_degrades_instead_of_guessing(self):
        """多进程应用（`:push` 等）有多个 pid：取错进程比不取更糟。"""
        result = self._run(
            [
                combined_output(
                    999, gpubusy="0 0", gpu_proc=(23286, 131457024), pids="23286 23300\n"
                )
            ]
        )

        assert result.samples[0]["gpu_mem_mb"] is None
        entry = next(item for item in result.degraded if item["key"] == "gpu_mem_mb")
        assert entry["stages"] == [supplement.REASON_PID_AMBIGUOUS]

    def test_missing_pid_degrades(self):
        result = self._run([combined_output(999, gpubusy="0 0", pids="")])

        assert result.samples[0]["gpu_mem_mb"] is None
        entry = next(item for item in result.degraded if item["key"] == "gpu_mem_mb")
        assert entry["stages"] == [supplement.REASON_PID_UNAVAILABLE]

    def test_pid_without_proc_row_degrades(self):
        result = self._run(
            [combined_output(999, gpubusy="0 0", gpu_proc=(1, 100), pids="23286\n")]
        )

        assert result.samples[0]["gpu_mem_mb"] is None
        entry = next(item for item in result.degraded if item["key"] == "gpu_mem_mb")
        assert entry["stages"] == [supplement.REASON_PROC_NOT_FOUND]

    def test_round_args_include_pidof_for_target_package(self):
        poller = make_poller(FakeCallDevice([]), collect_gpu_mem=True, target_pkg="com.demo")
        args = poller._round_args()
        assert "pidof" in args and "com.demo" in args
        assert supplement.GPUBUSY_PATH in args


class TestSupplementPollerCommands:
    """轮询命令形态：一次 adb 事务、段标记分隔，且不越权（不 root、不写设备）。"""

    def test_round_args_is_single_short_command(self):
        poller = make_poller(FakeCallDevice([]))
        args = poller._round_args()
        assert args[:3] == ["-s", "749622c9", "shell"]
        assert args[3:5] == ["date", "+%s"]
        assert "battery" in args and "gpu" in args


class TestWriteSupplementCsv:
    """补采样本落盘：canonical key 做列名，elapsed_s 做统一时间轴列。"""

    def test_writes_header_and_rows_with_empty_values(self, tmp_path):
        samples = [
            {"elapsed_s": 0.0, "temp_battery_c": 28.0, "gpu_util_pct": None, "gpu_mem_mb": None},
            {"elapsed_s": 2.0, "temp_battery_c": 28.1, "gpu_util_pct": 40.0, "gpu_mem_mb": 125.4},
        ]
        path = supplement.write_supplement_csv(tmp_path / "supplement_thermal_gpu.csv", samples)
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert lines == [
            "elapsed_s,temp_battery_c,gpu_util_pct,gpu_mem_mb",
            "0.0,28.0,,",
            "2.0,28.1,40.0,125.4",
        ]


class TestAggregateSamples:
    """补采样本聚合：与 collector 聚合行同形状；None-only 指标不产出行。"""

    def test_gpu_mem_aggregates_to_its_own_row(self):
        samples = [
            {"elapsed_s": 0.0, "gpu_mem_mb": 100.0},
            {"elapsed_s": 2.0, "gpu_mem_mb": 120.0},
        ]
        rows = supplement.aggregate_samples(samples)
        assert [row["metric_key"] for row in rows] == ["gpu_mem_mb"]
        assert rows[0]["unit"] == "MB"
        assert rows[0]["median"] == 110.0

    def test_aggregates_only_keys_with_samples(self):
        samples = [
            {"elapsed_s": 0.0, "temp_battery_c": 10.0, "gpu_util_pct": None, "gpu_mem_mb": None},
            {"elapsed_s": 2.0, "temp_battery_c": 20.0, "gpu_util_pct": None, "gpu_mem_mb": None},
            {"elapsed_s": 4.0, "temp_battery_c": 30.0, "gpu_util_pct": None, "gpu_mem_mb": None},
        ]
        rows = supplement.aggregate_samples(samples)
        assert len(rows) == 1
        row = rows[0]
        assert row["metric_key"] == "temp_battery_c"
        assert row["unit"] == "°C"
        assert row["source"] == "pc_supplement"
        assert (row["min"], row["median"], row["mean"], row["max"], row["p90"]) == (
            10.0, 20.0, 20.0, 30.0, 28.0
        )
        assert row["sample_count"] == 3

    def test_empty_samples_give_no_rows(self):
        assert supplement.aggregate_samples([]) == []
