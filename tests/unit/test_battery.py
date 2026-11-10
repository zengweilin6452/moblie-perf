"""场景耗电量（perf-metrics-expansion 01）：电流时序积分与 kit 接线测试。

口径锁定：放电为负（真机校准），只计 max(0,-mA)；梯形积分；dt<=0 跳过；
样本不足/零时长不出数；全正电流积出 0 是有效结论。
"""

from pathlib import Path

import pytest
from tests.unit.test_kit import FakeHarnessBackend, charging_keeper_device_call, make_kit

from mobile_perf import battery, collector


def _write_current_csv(path: Path, rows: list[tuple[float, float]]) -> Path:
    """写 SoloPi 形状电流 CSV（GBK 家族编码，与真机产物同口径）。"""
    lines = ["RecordTime,实时电流(mA),extra,SimpleTime"]
    lines += [f"{ts},{value},,{i}" for i, (ts, value) in enumerate(rows)]
    path.write_text("\n".join(lines) + "\n", encoding="gb18030")
    return path


class TestConsumedMah:
    """梯形积分的手算对照与反例。"""

    def test_constant_discharge_for_one_hour_is_exact(self):
        # 100mA 放电 1 小时 = 100mAh（负值=放电，真机口径）
        assert battery.consumed_mah([(0.0, -100.0), (3_600_000.0, -100.0)]) == 100.0

    def test_trapezoid_between_two_values(self):
        # 100→300mA 线性变化 1 小时 = (100+300)/2 = 200mAh
        assert battery.consumed_mah([(0.0, -100.0), (3_600_000.0, -300.0)]) == 200.0

    def test_positive_current_contributes_zero(self):
        # 全正电流（充电/回灌）：0mAh 是有效结论，不是 None
        assert battery.consumed_mah([(0.0, 200.0), (3_600_000.0, 100.0)]) == 0.0

    def test_mixed_sign_only_discharge_segment_counts(self):
        # 前半段 -100→+100：放电侧 100→0，梯形 50mA×0.5h=25；
        # 后半段 +100→-100：放电侧 0→100，同样 25；合计 50mAh
        samples = [(0.0, -100.0), (1_800_000.0, 100.0), (3_600_000.0, -100.0)]
        assert battery.consumed_mah(samples) == 50.0

    def test_single_sample_or_empty_is_none(self):
        assert battery.consumed_mah([(0.0, -100.0)]) is None
        assert battery.consumed_mah([]) is None

    def test_zero_total_duration_is_none(self):
        # 时间戳全相同：没有有效时长，不硬造 0
        assert battery.consumed_mah([(0.0, -100.0), (0.0, -200.0)]) is None

    def test_duplicate_timestamps_do_not_create_negative_integral(self):
        samples = [(0.0, -100.0), (0.0, -100.0), (3_600_000.0, -100.0)]
        assert battery.consumed_mah(samples) == 100.0

    def test_unsorted_input_is_ordered_before_integration(self):
        # 倒序输入不得积出负值
        assert battery.consumed_mah([(3_600_000.0, -100.0), (0.0, -100.0)]) == 100.0


class TestReadSourceSamples:
    """collector 时序读取通道：编码/列映射/坏行跳过。"""

    def test_reads_timestamped_series(self, tmp_path):
        path = _write_current_csv(
            tmp_path / "实时电流_Battery_x.csv", [(1000.0, -100.0), (2000.0, -50.0)]
        )
        assert collector.read_metric_series(path, "battery_current_ma") == [
            (1000.0, -100.0), (2000.0, -50.0)
        ]

    def test_rows_without_parseable_time_are_skipped(self, tmp_path):
        path = tmp_path / "current.csv"
        path.write_text(
            "RecordTime,实时电流(mA),extra,SimpleTime\nbad,-100,,0\n1000,-100,,1\n2000,NaNish,,2\n",
            encoding="gb18030",
        )
        assert collector.read_metric_series(path, "battery_current_ma") == [(1000.0, -100.0)]

    def test_missing_recordtime_column_gives_empty(self, tmp_path):
        path = tmp_path / "current.csv"
        path.write_text("实时电流(mA),SimpleTime\n-100,0\n", encoding="gb18030")
        assert collector.read_metric_series(path, "battery_current_ma") == []

    def test_other_metric_columns_are_not_picked(self, tmp_path):
        path = tmp_path / "pss.csv"
        path.write_text("RecordTime,PSS-main-18390(MB)\n1000,42\n", encoding="gb18030")
        assert collector.read_metric_series(path, "battery_current_ma") == []

    def test_merges_multiple_files(self, tmp_path):
        _write_current_csv(tmp_path / "a.csv", [(2000.0, -50.0)])
        _write_current_csv(tmp_path / "b.csv", [(1000.0, -100.0)])
        samples = battery.read_source_samples(tmp_path)
        assert sorted(samples) == [(1000.0, -100.0), (2000.0, -50.0)]

    def test_missing_dir_gives_empty(self, tmp_path):
        assert battery.read_source_samples(tmp_path / "nope") == []


class TestAggregateFromArtifacts:
    """入库行形状与真机夹具 sanity。"""

    def test_produces_single_sample_row(self, tmp_path):
        _write_current_csv(tmp_path / "current.csv", [(0.0, -100.0), (3_600_000.0, -100.0)])
        rows = battery.aggregate_from_artifacts(tmp_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["metric_key"] == "battery_consumed_mah"
        assert row["unit"] == "mAh"
        assert row["source"] == "solo_pi"
        assert row["median"] == 100.0
        assert row["sample_count"] == 1

    def test_insufficient_samples_produce_no_row(self, tmp_path):
        _write_current_csv(tmp_path / "current.csv", [(0.0, -100.0)])
        assert battery.aggregate_from_artifacts(tmp_path) == []

    def test_no_current_csv_produces_no_row(self, tmp_path):
        (tmp_path / "pss.csv").write_text(
            "RecordTime,PSS-main-18390(MB)\n1000,42\n", encoding="gb18030"
        )
        assert battery.aggregate_from_artifacts(tmp_path) == []

    def test_real_fixture_discharges_positive_energy(self):
        # 真机夹具（343 行真实放电电流）：符号口径若翻反，此测试即红
        fixtures = Path(__file__).parent.parent / "fixtures" / "solopi_csv_sample"
        rows = battery.aggregate_from_artifacts(fixtures)
        assert len(rows) == 1
        assert rows[0]["median"] > 0.0


class TestKitWiring:
    """run_collect 接线：Battery 采集项驱动出数/not_tested，充电降级覆盖。"""

    class _BatteryBackend(FakeHarnessBackend):
        """stop 时额外造一份电流 CSV：100mA 放电 3 秒。"""

        def stop(self, session_id, output_dir):
            payload = super().stop(session_id, output_dir)
            _write_current_csv(
                Path(output_dir) / "实时电流_Battery_fake.csv",
                [(i * 1000.0, -100.0) for i in range(4)],
            )
            return payload

    def test_battery_item_produces_consumed_mah(self, tmp_path):
        kit = make_kit(tmp_path, self._BatteryBackend())
        report = kit.run_collect(
            target="tv.danmaku.bili",
            items=["Battery"],
            duration_s=30.0,
            runs_dir=tmp_path / "runs",
            supplement=False,
        )
        entry = report["metricsSummary"]["battery_consumed_mah"]
        assert entry["median"] == pytest.approx(100.0 * 3 / 3600.0)
        assert entry["sampleCount"] == 1
        assert report["battery"]["method"] == "trapezoid_discharge_only"
        assert report["battery"]["sourceMetric"] == "battery_current_ma"

    def test_battery_item_without_current_column_marks_not_tested(self, tmp_path):
        kit = make_kit(tmp_path, FakeHarnessBackend())
        report = kit.run_collect(
            target="tv.danmaku.bili",
            items=["Battery"],
            duration_s=30.0,
            runs_dir=tmp_path / "runs",
            supplement=False,
        )
        entry = report["metricsSummary"]["battery_consumed_mah"]
        assert entry["notTested"] is True
        assert "实时电流" in entry["notTestedReason"]

    def test_no_battery_item_leaves_key_absent(self, tmp_path):
        kit = make_kit(tmp_path, FakeHarnessBackend())
        report = kit.run_collect(
            target="tv.danmaku.bili",
            items=["CPU"],
            duration_s=30.0,
            runs_dir=tmp_path / "runs",
            supplement=False,
        )
        assert "battery_consumed_mah" not in report["metricsSummary"]
        assert "battery" not in report

    def test_charging_overrides_with_degraded_reason(self, tmp_path):
        kit = make_kit(
            tmp_path, self._BatteryBackend(), device_call=charging_keeper_device_call()
        )
        report = kit.run_collect(
            target="tv.danmaku.bili",
            items=["Battery"],
            duration_s=30.0,
            runs_dir=tmp_path / "runs",
            supplement=False,
        )
        entry = report["metricsSummary"]["battery_consumed_mah"]
        assert entry["notTested"] is True
        assert "充电" in entry["notTestedReason"]
