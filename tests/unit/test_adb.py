"""resolve_adb 三级解析（显式参数 > 环境变量 > 随包 platform-tools）的单元测试。"""

import pytest

from mobile_perf.adb import AdbResolutionError, bundled_adb_path, resolve_adb


def test_explicit_param_wins_over_env(tmp_path, monkeypatch):
    fake = tmp_path / "adb.exe"
    fake.write_bytes(b"x")
    monkeypatch.setenv("MOBILE_PERF_ADB", str(tmp_path / "missing-a.exe"))
    monkeypatch.setenv("ADB", str(tmp_path / "missing-b.exe"))

    assert resolve_adb(str(fake)) == fake


def test_env_var_used_when_no_explicit(tmp_path, monkeypatch):
    fake = tmp_path / "adb.exe"
    fake.write_bytes(b"x")
    monkeypatch.setenv("MOBILE_PERF_ADB", str(fake))
    monkeypatch.setenv("ADB", str(tmp_path / "missing.exe"))

    assert resolve_adb(None) == fake


def test_legacy_adb_env_used_when_mobile_perf_env_missing(tmp_path, monkeypatch):
    fake = tmp_path / "adb.exe"
    fake.write_bytes(b"x")
    monkeypatch.delenv("MOBILE_PERF_ADB", raising=False)
    monkeypatch.setenv("ADB", str(fake))

    assert resolve_adb(None) == fake


def test_bundled_fallback_when_no_explicit_and_no_env(monkeypatch):
    monkeypatch.delenv("MOBILE_PERF_ADB", raising=False)
    monkeypatch.delenv("ADB", raising=False)

    resolved = resolve_adb(None)

    assert resolved == bundled_adb_path()
    assert resolved.is_file()


def test_env_var_pointing_to_missing_file_fails_fast(tmp_path, monkeypatch):
    monkeypatch.setenv("MOBILE_PERF_ADB", str(tmp_path / "nope.exe"))
    monkeypatch.delenv("ADB", raising=False)

    with pytest.raises(AdbResolutionError, match="nope.exe"):
        resolve_adb(None)
