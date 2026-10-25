"""collector 归档原语的单元测试：runId 生成、run 目录、产物校验、元数据落盘。"""

import json

import pytest

from mobile_perf import collector


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
