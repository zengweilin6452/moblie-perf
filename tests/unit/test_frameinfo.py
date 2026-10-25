"""逐帧采集（ticket 15）：framestats 解析、统计口径与轮询器。

真机样本 tests/fixtures/gfxinfo/framestats_real.txt（120 帧）是口径校准基准。
"""

import csv
from pathlib import Path

import pytest

from mobile_perf import frameinfo

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "gfxinfo" / "framestats_real.txt"


def _row(vsync_id: str, intended_ns: int, completed_ns: int, interval_ns: int = 8_333_333) -> str:
    """构造一行 framestats，列序严格照真机数据（AOSP FrameInfoIndex）。

    注意第 10 列（表头声称 FrameInterval）实际是 FrameStartTime 时间戳，
    真正的周期在第 11 列——真机输出就是这个错位，见模块文档。
    """
    cells = ["0"] * 24
    cells[0] = "0"  # Flags
    cells[1] = str(vsync_id)  # FrameTimelineVsyncId
    cells[2] = str(intended_ns)  # IntendedVsync
    cells[3] = str(intended_ns)  # Vsync
    cells[10] = str(intended_ns - 1_000_000)  # FrameStartTime（时间戳）
    cells[11] = str(interval_ns)  # FrameInterval（真实周期）
    cells[16] = str(completed_ns)  # FrameCompleted
    return ",".join(cells) + ","


def _wrap(rows: list[str]) -> str:
    header = (
        "Flags,FrameTimelineVsyncId,IntendedVsync,Vsync,InputEventId,HandleInputStart,"
        "AnimationStart,PerformTraversalsStart,DrawStart,FrameDeadline,FrameInterval,"
        "FrameStartTime,SyncQueued,SyncStart,IssueDrawCommandsStart,SwapBuffers,"
        "FrameCompleted,DequeueBufferDuration,QueueBufferDuration,GpuCompleted,"
        "SwapBuffersCompleted,DisplayPresentTime,CommandSubmissionCompleted,"
    )
    return "\n".join(["Applications Graphics Acceleration Info:", "", "---PROFILEDATA---", header]
                     + rows + ["---PROFILEDATA---", "", "View hierarchy:"])


def test_parse_real_fixture_yields_frames():
    samples = frameinfo.parse_framestats(FIXTURE.read_text(encoding="utf-8"))

    assert len(samples) == 120  # 真机样本：一次 dump 内 120 个唯一帧
    assert all(sample.duration_ms >= 0 for sample in samples)
    assert len({sample.vsync_id for sample in samples}) == len(samples)


def test_parse_real_fixture_is_deterministic():
    text = FIXTURE.read_text(encoding="utf-8")

    assert frameinfo.parse_framestats(text) == frameinfo.parse_framestats(text)


def test_parse_empty_window_returns_no_samples():
    text = _wrap([])

    assert frameinfo.parse_framestats(text) == []


def test_parse_skips_incomplete_frames():
    # FrameCompleted=0 表示该帧未完成，不能算出耗时（否则是巨值/负值）
    rows = [_row("1", 1_000_000_000, 0), _row("2", 1_000_000_000, 1_008_333_333)]
    samples = frameinfo.parse_framestats(_wrap(rows))

    assert [sample.vsync_id for sample in samples] == ["2"]
    assert samples[0].duration_ms == pytest.approx(8.333333, abs=1e-5)


def test_parse_dedupes_across_sections_by_vsync_id():
    # 真机同一帧可能出现在多个 PROFILEDATA 分段：按 vsync id 去重，只留首次
    row = _row("42", 1_000_000_000, 1_016_000_000)
    text = _wrap([row]) + "\n" + _wrap([row])

    samples = frameinfo.parse_framestats(text)

    assert len(samples) == 1
    assert samples[0].duration_ms == pytest.approx(16.0)


def test_parse_single_frame():
    samples = frameinfo.parse_framestats(
        _wrap([_row("7", 2_000_000_000, 2_020_000_000)])
    )

    assert len(samples) == 1
    assert samples[0].interval_ms == pytest.approx(8.333333, abs=1e-5)


def test_parse_ignores_text_without_profiledata():
    assert frameinfo.parse_framestats("Total frames rendered: 0\n") == []


def test_frame_stats_empty_returns_empty_dict():
    assert frameinfo.frame_stats([]) == {}


def test_frame_stats_percentiles_and_jank_ratio():
    # 60Hz 下 16.6ms 为周期：40ms 与 100ms 两帧超时（>8.33*1.5=12.5ms），8ms 不超
    samples = [
        frameinfo.FrameSample("1", 8.0, 16.67),
        frameinfo.FrameSample("2", 40.0, 16.67),
        frameinfo.FrameSample("3", 100.0, 16.67),
    ]

    stats = frameinfo.frame_stats(samples)

    assert stats["count"] == 3
    assert stats["p50"] == pytest.approx(40.0)
    assert stats["p99"] == pytest.approx(98.8)
    assert stats["jankCount"] == 2
    assert stats["jankRatioPct"] == pytest.approx(200 / 3)
    assert stats["refreshHz"] == pytest.approx(60.0, abs=0.05)  # 16.67ms 的倒数


def test_frame_stats_uses_per_frame_interval_for_variable_refresh():
    # 120Hz 帧（周期 8.33ms）：10ms 不算超时；同批 60Hz 帧（16.67ms）10ms 也不算
    samples = [
        frameinfo.FrameSample("1", 10.0, 8.33),
        frameinfo.FrameSample("2", 10.0, 16.67),
    ]

    assert frameinfo.frame_stats(samples)["jankCount"] == 0


def test_frame_stats_falls_back_to_median_interval_when_missing():
    # 部分帧缺 FrameInterval 时，用其余帧的中位数周期兜底（而不是跳过该帧）
    samples = [frameinfo.FrameSample("1", 40.0, 16.67), frameinfo.FrameSample("2", 5.0, None)]

    stats = frameinfo.frame_stats(samples)

    assert stats["jankCount"] == 1  # 40ms > 25.0ms（兜底周期 16.67 × 1.5），5ms 不算


def test_frame_stats_without_any_interval_gives_no_jank_verdict():
    # 全部缺周期信息时无从判定超时，如实报 0 计数与 None 刷新率，不猜
    samples = [frameinfo.FrameSample("1", 40.0, None), frameinfo.FrameSample("2", 5.0, None)]

    stats = frameinfo.frame_stats(samples)

    assert stats["jankCount"] == 0
    assert stats["refreshHz"] is None
    assert stats["count"] == 2


def test_aggregate_from_samples_emits_five_rows():
    samples = [
        frameinfo.FrameSample("1", 8.0, 16.67),
        frameinfo.FrameSample("2", 40.0, 16.67),
    ]

    rows = frameinfo.aggregate_from_samples(samples)

    keys = [row["metric_key"] for row in rows]
    assert keys == list(frameinfo.STAT_METRIC_KEYS)
    by_key = {row["metric_key"]: row for row in rows}
    assert by_key[frameinfo.METRIC_COUNT]["median"] == 2.0
    assert by_key[frameinfo.METRIC_COUNT]["sample_count"] == 1  # 会话级单样本
    assert by_key[frameinfo.METRIC_P50]["unit"] == "ms"
    assert by_key[frameinfo.METRIC_JANK_RATIO]["unit"] == "%"


def test_aggregate_from_empty_samples_returns_no_rows():
    assert frameinfo.aggregate_from_samples([]) == []


def test_write_frames_csv_roundtrip(tmp_path):
    samples = [frameinfo.FrameSample("1", 8.1234, 16.6667), frameinfo.FrameSample("2", 40.0, None)]

    path = frameinfo.write_frames_csv(tmp_path / "frames.csv", samples)

    rows = list(csv.reader(path.read_text(encoding="utf-8").splitlines()))
    assert rows[0] == ["vsync_id", "duration_ms", "interval_ms"]
    assert rows[1] == ["1", "8.123", "16.667"]
    assert rows[2] == ["2", "40.0", ""]


def test_write_frames_csv_skips_empty(tmp_path):
    path = frameinfo.write_frames_csv(tmp_path / "frames.csv", [])

    assert not path.exists()


class _FakeOutput:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode


class _FakeDevice:
    """按调用顺序返回预置输出，并记录每次调用的参数。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[list[str]] = []

    def __call__(self, args, timeout_s=15.0):
        self.calls.append(list(args))
        return self.responses.pop(0) if self.responses else _FakeOutput("")


def test_poller_resets_then_polls_and_dedupes():
    payload = _wrap([_row("1", 1_000_000_000, 1_010_000_000)])
    device = _FakeDevice([_FakeOutput(""), _FakeOutput(payload), _FakeOutput(payload)])
    poller = frameinfo.FramePoller(
        adb_path="adb",
        serial="s1",
        target="com.example",
        interval_s=0.01,
        call_device=device,
        sleep=lambda _s: None,
    )

    poller.start()
    result = poller.stop(join_timeout_s=2.0)

    assert result.reset_ok is True
    assert device.calls[0][-2:] == ["com.example", "reset"]
    assert "framestats" in device.calls[1]
    assert len(result.samples) == 1  # 两轮读到同一帧，去重后只算一次
    assert result.rounds >= 1


def test_poller_reports_reset_failure_without_blocking():
    device = _FakeDevice([_FakeOutput("", returncode=1), _FakeOutput("")])
    poller = frameinfo.FramePoller(
        adb_path="adb",
        serial="s1",
        target="com.example",
        interval_s=0.01,
        call_device=device,
        sleep=lambda _s: None,
    )

    poller.start()
    result = poller.stop(join_timeout_s=2.0)

    assert result.reset_ok is False
    assert result.samples == []


def test_poller_counts_round_errors():
    class Boom:
        def __init__(self):
            self.n = 0

        def __call__(self, args, timeout_s=15.0):
            self.n += 1
            if self.n == 1:
                return _FakeOutput("")
            raise RuntimeError("adb gone")

    poller = frameinfo.FramePoller(
        adb_path="adb",
        serial="s1",
        target="com.example",
        interval_s=0.01,
        call_device=Boom(),
        sleep=lambda _s: None,
    )

    poller.start()
    result = poller.stop(join_timeout_s=2.0)

    assert result.errors >= 1
    assert result.samples == []


def test_poller_rejects_non_positive_interval():
    with pytest.raises(ValueError):
        frameinfo.FramePoller(
            adb_path="adb", serial="s1", target="p", interval_s=0, call_device=_FakeDevice([])
        )


def test_real_fixture_refresh_rate_is_plausible_not_zero():
    """真机表头把 FrameInterval 与 FrameStartTime 写反，按表头取值会得到时间戳。

    回归保护：必须按数据位置取周期，算出约 120Hz 而不是 0 或负数。
    """
    samples = frameinfo.parse_framestats(FIXTURE.read_text(encoding="utf-8"))

    stats = frameinfo.frame_stats(samples)

    assert 100 <= stats["refreshHz"] <= 145
    assert all(0 < sample.interval_ms <= 20 for sample in samples)


def test_parse_falls_back_when_interval_column_holds_a_timestamp():
    """周期列取到时间戳量级（>1000ms）时回退到相邻列，而不是当成周期。"""
    cells = ["0"] * 24
    cells[0], cells[1], cells[2] = "0", "9", "1000000000"
    cells[10] = "1001000000"  # FrameStartTime 时间戳
    cells[11] = "21926046309707"  # 被污染的周期列（时间戳量级）
    cells[16] = "1008000000"
    text = _wrap([",".join(cells) + ","])

    samples = frameinfo.parse_framestats(text)

    assert len(samples) == 1
    # 回退取到第 10 列（1001000000ns ≈ 1001ms 仍超上界）→ 无有效周期
    assert samples[0].interval_ms is None or samples[0].interval_ms <= 1000


def test_parse_row_with_too_few_columns_is_skipped():
    text = _wrap(["0,1,1000000000,1000000000,0,0"])

    assert frameinfo.parse_framestats(text) == []
