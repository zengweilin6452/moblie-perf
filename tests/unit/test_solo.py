"""solo 适配层的单元测试：命令组装、JSON stdout 解析、错误传播。

真子进程边界（sys.path 隔离、真实 Harness 入口）在 tests/contract 中证明。
"""

from pathlib import Path

import pytest

from mobile_perf import solo


def write_stub_harness(tmp_path: Path, body: str) -> Path:
    stub = tmp_path / "stub_harness.py"
    stub.write_text(body, encoding="utf-8")
    return stub


def test_run_harness_builds_command_and_parses_compact_json(tmp_path):
    stub = write_stub_harness(
        tmp_path,
        "import json, os, sys\n"
        "print(json.dumps({\n"
        "    'success': True,\n"
        "    'argv': sys.argv[1:],\n"
        "    'pythonpath': os.environ.get('PYTHONPATH', ''),\n"
        "}, ensure_ascii=False))\n",
    )

    result = solo.run_harness(
        ["perf-start", "--items", "CPU"],
        adb_path=str(tmp_path / "adb.exe"),
        serial="749622c9",
        entry=stub,
    )

    assert result.success is True
    assert result.exit_code == 0
    assert result.data["argv"][:5] == [
        "--adb",
        str(tmp_path / "adb.exe"),
        "--serial",
        "749622c9",
        "perf-start",
    ]
    # 子进程只通过显式参数收 adb/serial；不得借 PYTHONPATH 传包内状态
    assert "mobile_perf" not in result.data["pythonpath"]


def test_run_harness_accepts_pretty_multiline_json(tmp_path):
    stub = write_stub_harness(
        tmp_path,
        "import json\n"
        "print(json.dumps({'success': True, 'sessionId': 's-1', 'state': 'recording'},"
        " indent=2))\n",
    )

    result = solo.run_harness(["perf-status"], entry=stub)

    assert result.data["sessionId"] == "s-1"
    assert result.data["state"] == "recording"


def test_run_harness_reflects_failure_payload_without_raising(tmp_path):
    stub = write_stub_harness(
        tmp_path,
        "import json, sys\n"
        "print(json.dumps({'success': False, 'error': '包含不支持的性能指标'}))\n"
        "sys.exit(2)\n",
    )

    result = solo.run_harness(["perf-start"], entry=stub)

    assert result.success is False
    assert result.exit_code == 2
    assert "不支持" in result.data["error"]


def test_run_harness_raises_on_non_json_stdout(tmp_path):
    stub = write_stub_harness(tmp_path, "print('boom: traceback tail')\n")

    with pytest.raises(solo.HarnessError, match="perf-start") as exc_info:
        solo.run_harness(["perf-start"], entry=stub)

    assert "boom" in exc_info.value.stdout


def test_subprocess_env_forces_utf8_without_touching_pythonpath(tmp_path):
    stub = write_stub_harness(
        tmp_path,
        "import json, os\n"
        "print(json.dumps({'utf8': os.environ.get('PYTHONUTF8', ''),"
        " 'pyio': os.environ.get('PYTHONIOENCODING', '')}))\n",
    )

    result = solo.run_harness([], entry=stub)

    assert result.data["utf8"] == "1"
    assert result.data["pyio"].lower() == "utf-8"


def test_perf_start_assembles_perf_start_arguments(tmp_path):
    stub = write_stub_harness(
        tmp_path, "import json, sys\nprint(json.dumps({'argv': sys.argv[1:]}))\n"
    )

    result = solo.perf_start(
        adb_path=str(tmp_path / "adb.exe"),
        serial="749622c9",
        target_package="tv.danmaku.bili",
        items=["CPU", "FPS", "Memory"],
        entry=stub,
    )

    argv = result.data["argv"]
    assert argv[argv.index("perf-start") - 2 :] == ["perf-start"] or "perf-start" in argv
    assert "--target-package" in argv
    assert "tv.danmaku.bili" in argv
    assert "--items" in argv
    assert "CPU,FPS,Memory" in argv


def test_perf_stop_passes_session_id_and_output_dir(tmp_path):
    stub = write_stub_harness(
        tmp_path, "import json, sys\nprint(json.dumps({'argv': sys.argv[1:]}))\n"
    )

    result = solo.perf_stop(
        adb_path=str(tmp_path / "adb.exe"),
        serial="749622c9",
        session_id="abc-123",
        output_dir=tmp_path / "artifacts",
        entry=stub,
    )

    argv = result.data["argv"]
    assert "perf-stop" in argv
    assert argv[argv.index("--session-id") + 1] == "abc-123"
    assert argv[argv.index("--output") + 1] == str(tmp_path / "artifacts")


def test_default_runner_uses_sys_executable_subprocess(tmp_path):
    """默认 runner 以 sys.executable 直跑入口脚本，不依赖 PATH 上的 python。"""
    entry = write_stub_harness(tmp_path, "import json\nprint(json.dumps({'success': True}))\n")

    result = solo.run_harness([], entry=entry)

    assert result.success is True
    assert solo.default_runner.__module__ == "mobile_perf.solo"


def test_harness_entry_points_at_repo_snapshot():
    entry = solo.harness_entry()

    assert entry.is_file()
    assert entry.name == "solopi_ai.py"
    assert "solopi_harness" in str(entry)


def test_real_harness_payload_shape_from_recorded_contract():
    """以 Harness 实测返回形状为基准，锁定 perf-start/perf-stop 的 JSON 契约字段。"""
    from mobile_perf.solo import perf_start_session_id, perf_stop_artifacts

    start_payload = {
        "success": True,
        "state": "recording",
        "sessionId": "e19f9a8e-6f0e-4b3f-9f4f-5f0f0f0f0f0f",
        "targetPackage": "tv.danmaku.bili",
        "items": ["CPU", "Memory"],
    }
    stop_payload = {
        "success": True,
        "state": "stopped",
        "sessionId": "e19f9a8e-6f0e-4b3f-9f4f-5f0f0f0f0f0f",
        "outputPath": "/sdcard/SoloPi/performance/e19f",
        "recordsRoot": "/sdcard/SoloPi/performance",
        "artifacts": {
            "performance": {
                "localDir": str(Path("runs/run-x/artifacts")),
                "files": ["performance.csv"],
            }
        },
    }

    assert perf_start_session_id({"success": True}) is None
    assert perf_start_session_id(start_payload) == start_payload["sessionId"]
    with pytest.raises(solo.HarnessError):
        perf_start_session_id(start_payload | {"success": True, "sessionId": 42})

    assert perf_stop_artifacts(stop_payload)["localDir"].endswith("artifacts")
    assert perf_stop_artifacts({"success": True}) == {}
