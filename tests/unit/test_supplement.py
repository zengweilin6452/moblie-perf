"""补采（supplement）单元测试：dumpsys 输出解析、对时换算、轮询生命周期与降级。"""

from mobile_perf import supplement


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


def combined_output(epoch, temperature=None, gpu=None):
    lines = [str(int(epoch))]
    if temperature is not None:
        lines += ["Current Battery Service state:", f"  temperature: {temperature}"]
    if gpu is not None:
        lines += [f"GPU Utilization (last 1s): {gpu}%"]
    return "\n".join(lines) + "\n"


def make_poller(fake_call, **kwargs):
    kwargs.setdefault("adb_path", "adb.exe")
    kwargs.setdefault("serial", "749622c9")
    kwargs.setdefault("interval_s", 0.2)
    kwargs.setdefault("clock_offset_s", 1.0)
    kwargs.setdefault("session_start_host_epoch_s", 998.0)
    kwargs.setdefault("call_device", fake_call)
    kwargs.setdefault("sleep", lambda seconds: None)
    kwargs.setdefault("now", lambda: 1000.0)
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
            [combined_output(999, temperature=280, gpu=30), combined_output(1001, temperature=281, gpu=40)]
        )
        poller = make_poller(call)
        poller.start()
        assert wait_until(lambda: poller.rounds == 2)
        result = poller.stop()

        assert [s["elapsed_s"] for s in result.samples] == [2.0, 4.0]
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
            {"elapsed_s": 0.0, "temp_battery_c": 28.0, "gpu_util_pct": None},
            {"elapsed_s": 2.0, "temp_battery_c": 28.1, "gpu_util_pct": 40.0},
        ]
        path = supplement.write_supplement_csv(tmp_path / "supplement_thermal_gpu.csv", samples)
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert lines == [
            "elapsed_s,temp_battery_c,gpu_util_pct",
            "0.0,28.0,",
            "2.0,28.1,40.0",
        ]


class TestAggregateSamples:
    """补采样本聚合：与 collector 聚合行同形状；None-only 指标不产出行。"""

    def test_aggregates_only_keys_with_samples(self):
        samples = [
            {"elapsed_s": 0.0, "temp_battery_c": 10.0, "gpu_util_pct": None},
            {"elapsed_s": 2.0, "temp_battery_c": 20.0, "gpu_util_pct": None},
            {"elapsed_s": 4.0, "temp_battery_c": 30.0, "gpu_util_pct": None},
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
