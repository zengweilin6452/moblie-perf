"""Harness 子进程边界契约（ADR-0002）。

证明宿主进程不因调用 Harness 被 sys.path 污染、顶级模块名不被劫持，
且真实 Harness 入口能以 JSON stdout 契约被子进程调用。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

from mobile_perf import solo

REAL_ENTRY = solo.harness_entry()


def test_real_harness_entry_is_importable_only_in_subprocess():
    """真实 Harness 的 import 会 sys.path.insert 自身目录并产生顶级 timing 模块；
    这些副作用必须只发生在子进程内。"""
    assert (REAL_ENTRY.parent / "timing.py").is_file()
    host_path_before = list(sys.path)

    with pytest.raises(solo.HarnessError) as exc_info:
        # --help 只构建 parser 即退出，不触碰 adb；stdout 是用法文本而非 JSON。
        solo.run_harness(["--help"], entry=REAL_ENTRY)

    usage = exc_info.value.stdout
    assert "perf-start" in usage
    assert "perf-stop" in usage

    assert sys.path == host_path_before
    assert importlib.util.find_spec("timing") is None


def test_harness_syspath_injection_does_not_leak_to_host(tmp_path):
    """子进程内的 sys.path 注入与顶级导入照常生效，但宿主进程不受影响。"""
    stub = tmp_path / "stub_harness.py"
    stub.write_text(
        "import json, sys\n"
        "sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))\n"
        "import timing\n"
        "print(json.dumps({'success': True, 'timingFile': timing.__file__,"
        " 'sysPathLen': len(sys.path)}))\n",
        encoding="utf-8",
    )
    (tmp_path / "timing.py").write_text("MARKER = 'stub-harness-timing'\n", encoding="utf-8")
    host_path_before = list(sys.path)

    result = solo.run_harness([], entry=stub)

    assert Path(result.data["timingFile"]) == tmp_path / "timing.py"
    assert result.data["timingFile"] == str(tmp_path / "timing.py")
    assert sys.path == host_path_before
    assert importlib.util.find_spec("timing") is None
