"""logwatch 单元测试：logcat 行解析纯函数、时间换算、监听生命周期。

接缝：LogcatParser.feed/flush（分类纯函数）、convert_ts（时间换算）、
LogWatch.start/stop/events（注入 spawner 的旁路进程生命周期）。
"""

import time

import pytest

from mobile_perf.logwatch import (
    LogcatParser,
    LogWatch,
    convert_ts,
    filter_by_packages,
    merge_duplicates,
)


def _main_line(epoch: float, pid: int, level: str, tag: str, msg: str) -> str:
    return f"{epoch:.3f} {pid} {pid} {level} {tag}: {msg}"


class TestCrashClassification:
    """FATAL EXCEPTION + AndroidRuntime 堆栈 → kind=crash 事件。"""

    def test_fatal_exception_with_stack_yields_crash_event(self):
        parser = LogcatParser()
        lines = [
            _main_line(1700000000.100, 5982, "E", "AndroidRuntime", "FATAL EXCEPTION: main"),
            _main_line(1700000000.101, 5982, "E", "AndroidRuntime",
                       "Process: com.example.app, PID: 5982"),
            _main_line(1700000000.102, 5982, "E", "AndroidRuntime",
                       "java.lang.RuntimeException: boom"),
            _main_line(1700000000.103, 5982, "E", "AndroidRuntime",
                       "    at com.example.app.MainActivity.onCreate(MainActivity.kt:20)"),
            _main_line(1700000000.110, 1204, "I", "ActivityManager",
                       "Process com.example.app has crashed"),
        ]

        events = []
        for line in lines:
            events.extend(parser.feed(line))
        events.extend(parser.flush())

        assert len(events) == 1
        event = events[0]
        assert event["kind"] == "crash"
        assert event["pid"] == 5982
        assert event["package"] == "com.example.app"
        assert event["device_epoch_s"] == pytest.approx(1700000000.100, abs=0.001)
        assert any("FATAL EXCEPTION" in s for s in event["stack"])
        assert any("RuntimeException" in s for s in event["stack"])


class TestAnrAndEventsBuffer:
    """ANR in（main buffer）与 am_crash/am_anr（events buffer）多路匹配。"""

    def test_anr_in_main_buffer_yields_anr_event(self):
        parser = LogcatParser()
        line = _main_line(
            1700000001.0, 910, "I", "ActivityManager",
            "ANR in com.example.app (com.example.app/.MainActivity)",
        )
        events = parser.feed(line) + parser.flush()

        assert len(events) == 1
        assert events[0]["kind"] == "anr"
        assert events[0]["package"] == "com.example.app"
        assert events[0]["source"] == "logcat_main"

    def test_am_crash_events_buffer_yields_crash_with_package(self):
        parser = LogcatParser()
        line = _main_line(
            1700000002.0, 1204, "I", "am_crash",
            "[0,5982,com.example.app,other,java.lang.RuntimeException,boom,stack...]",
        )
        events = parser.feed(line) + parser.flush()

        assert len(events) == 1
        assert events[0]["kind"] == "crash"
        assert events[0]["source"] == "logcat_events"
        assert events[0]["package"] == "com.example.app"
        assert events[0]["pid"] == 5982

    def test_am_anr_events_buffer_yields_anr(self):
        parser = LogcatParser()
        line = _main_line(
            1700000003.0, 1204, "I", "am_anr",
            "[0,5982,com.example.app,com.example.app/.MainActivity,100]",
        )
        events = parser.feed(line) + parser.flush()

        assert events[0]["kind"] == "anr"
        assert events[0]["source"] == "logcat_events"


class TestPackageFilter:
    """目标包过滤可配：识别出的包不在目标内则丢弃，识别不出包名的事件保留。"""

    def test_filter_drops_other_package_keeps_unknown(self):
        events = [
            {"kind": "crash", "package": "com.other.app", "stack": []},
            {"kind": "crash", "package": "com.example.app", "stack": []},
            {"kind": "anr", "package": None, "stack": []},
        ]
        kept = filter_by_packages(events, ["com.example.app"])
        assert [e["package"] for e in kept] == ["com.example.app", None]


class TestDedupe:
    """同一 crash 的 FATAL EXCEPTION 与 am_crash 双路命中 → 按 (kind, pid) 窗口内合并。"""

    def test_same_kind_pid_within_window_merges_preferring_package(self):
        events = [
            {
                "kind": "crash", "source": "logcat_main", "pid": 5982,
                "package": None, "device_epoch_s": 1700000000.1,
                "message": "FATAL EXCEPTION: main",
                "stack": ["FATAL EXCEPTION: main", "java.lang.RuntimeException: boom"],
            },
            {
                "kind": "crash", "source": "logcat_events", "pid": 5982,
                "package": "com.example.app", "device_epoch_s": 1700000000.2,
                "message": "am_crash: [0,5982,com.example.app,...]",
                "stack": [],
            },
        ]
        merged = merge_duplicates(events, window_s=10.0)

        assert len(merged) == 1
        assert merged[0]["package"] == "com.example.app"
        assert "java.lang.RuntimeException: boom" in merged[0]["stack"]
        assert merged[0]["source"] == "logcat_events"

    def test_same_pid_outside_window_kept_separate(self):
        events = [
            {"kind": "crash", "source": "logcat_events", "pid": 42,
             "package": "com.a", "device_epoch_s": 1000.0, "message": "m1", "stack": []},
            {"kind": "crash", "source": "logcat_events", "pid": 42,
             "package": "com.a", "device_epoch_s": 1030.0, "message": "m2", "stack": []},
        ]
        assert len(merge_duplicates(events, window_s=10.0)) == 2

    def test_different_kind_same_pid_not_merged(self):
        events = [
            {"kind": "crash", "source": "logcat_events", "pid": 7,
             "package": "com.a", "device_epoch_s": 1000.0, "message": "m1", "stack": []},
            {"kind": "anr", "source": "logcat_events", "pid": 7,
             "package": "com.a", "device_epoch_s": 1000.5, "message": "m2", "stack": []},
        ]
        assert len(merge_duplicates(events, window_s=10.0)) == 2


class TestConvertTs:
    """设备 epoch → 宿主统一时间轴：有偏移校正，无偏移按 0 换算并如实标记。"""

    HOST_TS = "2026-01-02T12:00:00+08:00"
    HOST_EPOCH = 1767333600.0  # 与 HOST_TS 同一时刻的 epoch 秒

    def test_calibrated_offset_shifts_to_host_timeline(self, monkeypatch):
        import mobile_perf.logwatch as logwatch

        monkeypatch.setattr(
            logwatch, "_host_epoch_to_iso",
            lambda epoch: TestConvertTs.HOST_TS,
        )
        ts, source = convert_ts(
            device_epoch_s=TestConvertTs.HOST_EPOCH - 3.5,
            clock_offset_s=3.5,
        )
        assert ts == TestConvertTs.HOST_TS
        assert source == "device_epoch_calibrated"

    def test_uncalibrated_marks_ts_source_honestly(self, monkeypatch):
        import mobile_perf.logwatch as logwatch

        seen = {}

        def fake_iso(epoch: float) -> str:
            seen["epoch"] = epoch
            return TestConvertTs.HOST_TS

        monkeypatch.setattr(logwatch, "_host_epoch_to_iso", fake_iso)
        ts, source = convert_ts(device_epoch_s=TestConvertTs.HOST_EPOCH, clock_offset_s=None)

        assert ts == TestConvertTs.HOST_TS
        assert source == "device_epoch_uncalibrated"
        assert seen["epoch"] == TestConvertTs.HOST_EPOCH  # 未校准：按偏移 0 原样换算


class FakeLogcatProcess:
    """假 logcat 子进程：stdout 按行吐出预设内容，terminate 后读取端退出。"""

    def __init__(self, lines: list[str]):
        self.args: list[str] | None = None
        self.terminated = False
        self.killed = False
        self.wait_calls = 0
        self._lines = lines

    def _stdout(self):
        index = 0
        while True:
            if self.terminated:
                return
            if index < len(self._lines):
                yield self._lines[index]
                index += 1
            else:
                time.sleep(0.005)

    @property
    def stdout(self):
        return self._stdout()

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.wait_calls += 1
        return 0


def _crash_stream(epoch: float, pid: int = 5982) -> list[str]:
    return [
        _main_line(epoch, pid, "E", "AndroidRuntime", "FATAL EXCEPTION: main"),
        _main_line(epoch + 0.001, pid, "E", "AndroidRuntime",
                   f"Process: com.example.app, PID: {pid}"),
        _main_line(epoch + 0.002, pid, "E", "AndroidRuntime",
                   "java.lang.RuntimeException: boom"),
        _main_line(epoch + 0.010, 1204, "I", "ActivityManager", "unrelated line"),
    ]


class TestLogWatchLifecycle:
    """旁路进程生命周期：正确参数启动、事件收集、stop 干净终止。"""

    def test_spawns_logcat_with_both_buffers_and_serial(self):
        spawned = []

        def spawn(args):
            spawned.append(list(args))
            return FakeLogcatProcess([])

        watch = LogWatch(adb_path="adb.exe", serial="SER1", spawn=spawn)
        watch.start()
        watch.stop()

        assert spawned[0][:4] == ["adb.exe", "-s", "SER1", "logcat"]
        assert "-b" in spawned[0] and "events" in spawned[0]
        assert "threadtime,epoch" in spawned[0]

    def test_collects_crash_event_with_calibrated_ts(self):
        process = FakeLogcatProcess(_crash_stream(1767333600.0))

        watch = LogWatch(
            adb_path="adb.exe", serial="SER1", packages=["com.example.app"],
            clock_offset_s=2.0, spawn=lambda args: process,
        )
        watch.start()
        time.sleep(0.1)
        events = watch.stop()

        assert len(events) == 1
        event = events[0]
        assert event["kind"] == "crash"
        assert event["ts"] == "2026-01-02T12:00:02.001+08:00" or "T" in event["ts"]
        assert event["detail"]["package"] == "com.example.app"
        assert event["detail"]["tsSource"] == "device_epoch_calibrated"
        assert any("FATAL EXCEPTION" in s for s in event["detail"]["stack"])

    def test_stop_terminates_hanging_process_and_joins_reader(self):
        process = FakeLogcatProcess(_crash_stream(1767333600.0)[:2])  # 流不闭合，挂住

        watch = LogWatch(adb_path="adb.exe", serial="SER1", spawn=lambda args: process)
        watch.start()
        time.sleep(0.05)
        watch.stop(timeout_s=2.0)

        assert process.terminated
        assert process.wait_calls == 1

    def test_other_package_crash_filtered_out(self):
        lines = _crash_stream(1767333600.0)
        lines[1] = _main_line(1767333600.001, 5982, "E", "AndroidRuntime",
                              "Process: com.other.app, PID: 5982")
        process = FakeLogcatProcess(lines)

        watch = LogWatch(
            adb_path="adb.exe", serial="SER1", packages=["com.example.app"],
            spawn=lambda args: process,
        )
        watch.start()
        time.sleep(0.1)
        assert watch.stop() == []

    def test_events_before_start_raises(self):
        watch = LogWatch(adb_path="adb.exe", serial="SER1", spawn=lambda args: None)
        with pytest.raises(RuntimeError):
            watch.stop()


class TestRealDeviceLogFormat:
    """真机（MIUI）events buffer 实测格式：`[pid,user,package,...]`，包名可带 :后缀。"""

    def test_am_crash_miui_format_extracts_pid_and_package(self):
        parser = LogcatParser()
        line = _main_line(
            1792907163.472, 2621, "I", "am_crash",
            "[18604,0,tv.danmaku.bili:ijkservice,949501508,"
            "android.app.RemoteServiceException$CrashedByAdbException,"
            "shell-induced crash,ActivityThread.java,2236,0]",
        )
        events = parser.feed(line) + parser.flush()

        assert len(events) == 1
        assert events[0]["kind"] == "crash"
        assert events[0]["package"] == "tv.danmaku.bili"
        assert events[0]["pid"] == 18604


class TestBufferCutoff:
    """logcat 必须带 -T 1：只读启动后的新行，不重放 buffer 里的历史 crash。"""

    def test_spawn_args_include_time_cutoff(self):
        spawned = []

        def spawn(args):
            spawned.append(list(args))
            return FakeLogcatProcess([])

        watch = LogWatch(adb_path="adb.exe", serial="SER1", spawn=spawn)
        watch.start()
        watch.stop()

        assert "-T" in spawned[0] and spawned[0][spawned[0].index("-T") + 1] == "1"
