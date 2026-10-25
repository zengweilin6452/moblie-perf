"""collector 归档原语的单元测试：runId 生成、run 目录、产物校验、元数据落盘。"""

import json
from datetime import datetime
from pathlib import Path

import pytest

from mobile_perf import collector

FIXTURE_CSV_DIR = Path(__file__).parent.parent / "fixtures" / "solopi_csv_sample"


def test_new_run_id_format_and_uniqueness():
    run_a = collector.new_run_id()
    run_b = collector.new_run_id()

    assert run_a.startswith("run-")
    parts = run_a.split("-")
    assert len(parts) == 4  # run-<日期>-<时间>-<随机>
    assert len(parts[3]) == 4
    assert run_a != run_b


def test_create_run_dir_makes_fresh_directory(tmp_path):
    run_dir = collector.create_run_dir(tmp_path, "run-x")

    assert run_dir == tmp_path / "run-x"
    assert run_dir.is_dir()


def test_create_run_dir_refuses_existing(tmp_path):
    (tmp_path / "run-x").mkdir()

    with pytest.raises(collector.RunDirError, match="run-x"):
        collector.create_run_dir(tmp_path, "run-x")


def test_validate_artifacts_finds_nonempty_csv(tmp_path):
    artifacts = tmp_path / "artifacts" / "performance"
    artifacts.mkdir(parents=True)
    (artifacts / "performance.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (artifacts / "empty.csv").write_text("", encoding="utf-8")

    files = collector.validate_artifacts(tmp_path / "artifacts")

    assert files == [artifacts / "performance.csv"]


def test_validate_artifacts_fails_when_no_csv(tmp_path):
    (tmp_path / "artifacts").mkdir()

    with pytest.raises(collector.ArtifactError, match="CSV"):
        collector.validate_artifacts(tmp_path / "artifacts")


def test_write_meta_round_trip(tmp_path):
    meta = {"runId": "run-x", "target": "tv.danmaku.bili", "items": ["CPU"]}

    path = collector.write_meta(tmp_path, meta)

    assert path == tmp_path / "meta.json"
    assert json.loads(path.read_text(encoding="utf-8")) == meta


def _write_solopi_csv(path, value_column, values, *, encoding="gb18030"):
    rows = ["RecordTime," + value_column + ",extra,SimpleTime"]
    rows += [f"{1000000 + i},{value},,{i}" for i, value in enumerate(values)]
    path.write_text("\n".join(rows) + "\n", encoding=encoding)


def test_aggregate_computes_min_median_mean_max_p90(tmp_path):
    # 独立手算基线：值 1..5 → min=1 median=3 mean=3 max=5 p90=4.6
    values = [1, 2, 3, 4, 5]
    _write_solopi_csv(tmp_path / "PSS-main-18390_Memory_x.csv", "PSS-main-18390(MB)", values)

    aggregates = collector.aggregate_artifacts(tmp_path)

    assert len(aggregates) == 1
    agg = aggregates[0]
    assert agg["metric_key"] == "mem_pss_mb"
    assert agg["unit"] == "MB"
    assert agg["source"] == "solo_pi"
    assert agg["min"] == 1.0
    assert agg["median"] == 3.0
    assert agg["mean"] == 3.0
    assert agg["max"] == 5.0
    assert agg["p90"] == pytest.approx(4.6)
    assert agg["sample_count"] == 5


def test_aggregate_skips_unmapped_columns_and_non_numeric_values(tmp_path):
    _write_solopi_csv(
        tmp_path / "PSS-main-18390_Memory_x.csv",
        "PSS-main-18390(MB)",
        ["10", "", "abc", "20"],
    )

    aggregates = collector.aggregate_artifacts(tmp_path)

    assert len(aggregates) == 1
    assert aggregates[0]["metric_key"] == "mem_pss_mb"
    assert aggregates[0]["min"] == 10.0
    assert aggregates[0]["max"] == 20.0
    assert aggregates[0]["sample_count"] == 2


def test_aggregate_merges_files_mapping_to_same_metric(tmp_path):
    _write_solopi_csv(tmp_path / "PSS-main-111_Memory_a.csv", "PSS-main-111(MB)", [10.0])
    _write_solopi_csv(tmp_path / "PSS-main-222_Memory_b.csv", "PSS-main-222(MB)", [30.0])

    aggregates = collector.aggregate_artifacts(tmp_path)

    assert len(aggregates) == 1
    assert aggregates[0]["min"] == 10.0
    assert aggregates[0]["max"] == 30.0
    assert aggregates[0]["sample_count"] == 2


def test_aggregate_handles_real_fixture_files():
    # 真实 CSV（GBK）：映射出的 canonical key 与样本数应与文件行数一致
    aggregates = {
        agg["metric_key"]: agg for agg in collector.aggregate_artifacts(FIXTURE_CSV_DIR)
    }

    assert {
        "mem_pss_mb",
        "cpu_global_pct",
        "frame_delay_max_ms",
        "battery_current_ma",
    } <= set(aggregates)
    pss_file = next(FIXTURE_CSV_DIR.glob("PSS-main-*.csv"))
    expected_rows = len(pss_file.read_text(encoding="gb18030").splitlines()) - 1
    assert aggregates["mem_pss_mb"]["sample_count"] == expected_rows
    pss = aggregates["mem_pss_mb"]
    assert pss["min"] <= pss["median"] <= pss["max"]
    assert aggregates["frame_delay_max_ms"]["unit"] == "ms"


def test_aggregate_raises_when_no_csv(tmp_path):
    with pytest.raises(collector.ArtifactError):
        collector.aggregate_artifacts(tmp_path)


def test_aggregate_raises_when_nothing_maps(tmp_path):
    unmapped = ("进程上行速率-main-1(KB/S)", [1.0, 2.0])
    _write_solopi_csv(tmp_path / "进程上行速率-main-1_Network_x.csv", *unmapped)

    with pytest.raises(collector.ArtifactError, match="canonical"):
        collector.aggregate_artifacts(tmp_path)


class FakeAdbRunner:
    """模拟 adb：设备端 performance-<uuid> 目录里放若干 CSV，记录执行过的命令。"""

    def __init__(self, remote_dir, files):
        self.remote_dir = remote_dir
        self.files = files  # {文件名: 行数}
        self.commands = []
        self.removed = []
        self.fail_ls = False

    def __call__(self, cmd, timeout_s):
        self.commands.append(cmd)
        if "ls" in cmd:
            if self.fail_ls:
                return _AdbResult("", 1)
            return _AdbResult("\n".join(self.files) + "\n", 0)
        if "wc" in cmd:
            # 设备端 glob 展开一次探完：每行 "<行数> <远端路径>"
            lines = [
                f"{count} {self.remote_dir}/{name}" for name, count in self.files.items()
            ]
            return _AdbResult("\n".join(lines) + "\n", 0)
        if "rm" in cmd:
            self.removed.append(cmd[-1])
            return _AdbResult("", 0)
        return _AdbResult("", 1)


class _AdbResult:
    def __init__(self, stdout, returncode):
        self.stdout = stdout
        self.returncode = returncode


def _make_local_artifacts(tmp_path, files):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(parents=True)
    for name, values in files.items():
        _write_solopi_csv(artifacts / name, "PSS-main-18390(MB)", values)
    return artifacts


REMOTE = "/sdcard/Android/data/com.example/files/records/performance-abc123"


def test_verify_pull_integrity_passes_then_device_dir_removed(tmp_path):
    artifacts = _make_local_artifacts(tmp_path, {"PSS-main-1_Memory_a.csv": [1, 2, 3]})
    runner = FakeAdbRunner(REMOTE, {"PSS-main-1_Memory_a.csv": 4})  # 1 表头 + 3 数据

    report, removed = collector.cleanup_after_pull(
        "adb", "749622c9", REMOTE, artifacts, runner=runner
    )

    assert report.ok and not report.mismatches and report.probe_error is None
    assert report.checked_files == ["PSS-main-1_Memory_a.csv"]
    assert removed is True
    assert runner.removed == [REMOTE]


def test_verify_pull_integrity_flags_row_count_mismatch(tmp_path):
    artifacts = _make_local_artifacts(tmp_path, {"PSS-main-1_Memory_a.csv": [1, 2, 3]})
    runner = FakeAdbRunner(REMOTE, {"PSS-main-1_Memory_a.csv": 9})  # 设备端行数更多

    report, removed = collector.cleanup_after_pull(
        "adb", "749622c9", REMOTE, artifacts, runner=runner
    )

    assert not report.ok
    assert report.mismatches == [
        {
            "file": "PSS-main-1_Memory_a.csv",
            "reason": "row_count",
            "deviceLines": 9,
            "localLines": 4,
        }
    ]
    assert removed is False
    assert runner.removed == []  # 不一致绝不删设备端


def test_verify_pull_integrity_flags_missing_local_file(tmp_path):
    artifacts = _make_local_artifacts(tmp_path, {"PSS-main-1_Memory_a.csv": [1]})
    runner = FakeAdbRunner(
        REMOTE, {"PSS-main-1_Memory_a.csv": 2, "全局下行速率_Network_b.csv": 5}
    )

    report = collector.verify_pull_integrity(
        "adb", "749622c9", REMOTE, artifacts, runner=runner
    )

    assert not report.ok
    assert report.mismatches == [
        {"file": "全局下行速率_Network_b.csv", "reason": "missing_local"}
    ]


def test_verify_pull_integrity_reports_probe_failure(tmp_path):
    artifacts = _make_local_artifacts(tmp_path, {"PSS-main-1_Memory_a.csv": [1]})
    runner = FakeAdbRunner(REMOTE, {})
    runner.fail_ls = True

    report = collector.verify_pull_integrity(
        "adb", "749622c9", REMOTE, artifacts, runner=runner
    )

    assert not report.ok
    assert report.probe_error
    assert report.checked_files == []


def test_remove_device_session_dir_refuses_outside_records(tmp_path):
    runner = FakeAdbRunner("/data/local/tmp/performance-abc", {})

    removed = collector.remove_device_session_dir(
        "adb", "749622c9", "/data/local/tmp/performance-abc", runner=runner
    )

    assert removed is False
    assert runner.removed == []


def _make_run(tmp_path, store, stamp):
    """造一个本组件 run：目录 run-<stamp>-abcd + sessions 行。"""
    run_id = f"run-{stamp}-abcd"
    (tmp_path / run_id).mkdir()
    store.record_session({"run_id": run_id, "target": "tv.danmaku.bili"})
    return run_id


def test_retention_removes_runs_older_than_keep_days(tmp_path):
    from mobile_perf.store import PerfStore

    store = PerfStore(tmp_path / "db" / "mobile_perf.db")
    old = _make_run(tmp_path, store, "20260901-000000")
    fresh = _make_run(tmp_path, store, "20261025-110000")
    now = datetime(2026, 10, 25, 12, 0, 0)

    removed = collector.enforce_retention(
        tmp_path, store, keep_days=30, now=now
    )

    assert removed == [old]
    assert not (tmp_path / old).exists()
    assert (tmp_path / fresh).exists()
    assert [r["run_id"] for r in store.fetch_sessions()] == [fresh]
    store.close()


def test_retention_keeps_only_latest_keep_runs(tmp_path):
    from mobile_perf.store import PerfStore

    store = PerfStore(tmp_path / "db" / "mobile_perf.db")
    first = _make_run(tmp_path, store, "20261001-000000")
    second = _make_run(tmp_path, store, "20261002-000000")
    third = _make_run(tmp_path, store, "20261003-000000")
    now = datetime(2026, 10, 25, 12, 0, 0)

    removed = collector.enforce_retention(
        tmp_path, store, keep_runs=2, keep_days=365, now=now
    )

    assert removed == [first]  # 最旧的超出 keep_runs
    assert (tmp_path / second).exists() and (tmp_path / third).exists()
    store.close()


def test_retention_never_touches_non_component_paths(tmp_path):
    from mobile_perf.store import PerfStore

    store = PerfStore(tmp_path / "db" / "mobile_perf.db")
    _make_run(tmp_path, store, "20260901-000000")
    (tmp_path / "my-own-data").mkdir()
    (tmp_path / "run-not-a-real-run").mkdir()
    (tmp_path / "run-20260901-000000-toolong").mkdir()
    now = datetime(2026, 10, 25, 12, 0, 0)

    removed = collector.enforce_retention(tmp_path, store, keep_days=30, now=now)

    assert removed == ["run-20260901-000000-abcd"]
    assert (tmp_path / "my-own-data").exists()
    assert (tmp_path / "run-not-a-real-run").exists()
    assert (tmp_path / "run-20260901-000000-toolong").exists()
    store.close()


def test_retention_requires_positive_thresholds(tmp_path):
    from mobile_perf.store import PerfStore

    store = PerfStore(tmp_path / "db" / "mobile_perf.db")
    with pytest.raises(ValueError, match="正数"):
        collector.enforce_retention(tmp_path, store, keep_runs=0, now=datetime.now())
    with pytest.raises(ValueError, match="正数"):
        collector.enforce_retention(tmp_path, store, keep_days=0, now=datetime.now())
    store.close()
