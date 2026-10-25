"""FileLockGate 的单元测试：锁文件协议、fast-fail、重入与 passthrough。

真实区域锁只在 Windows 上跑（与 moblie-auto 同为 Windows 限定）；"其他进程
持锁"用测试自身对同一字节区域加锁来模拟，不需要真机。
"""

import hashlib
import json
import os
import socket

import pytest

from mobile_perf.gate import FileLockGate, GateBusyError

pytestmark_file_lock = pytest.mark.skipif(os.name != "nt", reason="区域锁仅 Windows")


def make_gate(tmp_path, **kwargs):
    kwargs.setdefault("lease_dir", tmp_path / "leases")
    return FileLockGate(**kwargs)


def lock_paths(tmp_path, serial):
    digest = hashlib.sha256(serial.encode("utf-8")).hexdigest()[:24]
    return (
        tmp_path / "leases" / f"{digest}.lock",
        tmp_path / "leases" / f"{digest}.holder.json",
    )


class TestRegionLockProtocol:
    @pytestmark_file_lock
    def test_lock_and_holder_files_follow_mobile_auto_naming(self, tmp_path):
        gate = make_gate(tmp_path)
        serial = "749622c9"

        with gate.borrow(serial, purpose="mobile_perf.test"):
            lock_path, holder_path = lock_paths(tmp_path, serial)
            assert gate.path == lock_path
            assert gate.holder_path == holder_path
            assert lock_path.exists()
            assert holder_path.exists()
        assert gate.held is False

    @pytestmark_file_lock
    def test_borrow_writes_holder_json_with_pid_hostname_purpose(self, tmp_path):
        gate = make_gate(tmp_path)

        with gate.borrow("749622c9", purpose="mobile_perf.test"):
            _, holder_path = lock_paths(tmp_path, "749622c9")
            holder = json.loads(holder_path.read_text(encoding="utf-8"))
        assert holder["pid"] == os.getpid()
        assert holder["hostname"] == socket.gethostname()
        assert holder["purpose"] == "mobile_perf.test"
        assert holder["acquired_at"]  # ISO 时间戳存在

    @pytestmark_file_lock
    def test_release_unlocks_region_so_others_can_acquire(self, tmp_path):
        import msvcrt

        gate = make_gate(tmp_path)
        with gate.borrow("749622c9", purpose="mobile_perf.test"):
            pass

        # 释放后测试进程能立即锁上同一字节区域，证明句柄级锁已归还
        lock_path, _ = lock_paths(tmp_path, "749622c9")
        handle = open(lock_path, "a+b")
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            handle.close()

    @pytestmark_file_lock
    def test_borrow_is_reentrant_within_hold(self, tmp_path):
        gate = make_gate(tmp_path)

        with gate.borrow("749622c9", purpose="mobile_perf.outer"):
            with gate.borrow("749622c9", purpose="mobile_perf.inner"):
                assert gate.held is True


class TestBusyFastFail:
    @pytestmark_file_lock
    def test_borrow_raises_gate_busy_immediately_when_region_held_elsewhere(
        self, tmp_path
    ):
        import msvcrt

        gate = make_gate(tmp_path)
        lock_path, _ = lock_paths(tmp_path, "749622c9")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_bytes(b"")

        # 模拟"其他进程"：测试自身先锁住 1 字节区域
        other = open(lock_path, "a+b")
        try:
            other.seek(0)
            msvcrt.locking(other.fileno(), msvcrt.LK_NBLCK, 1)
            with pytest.raises(GateBusyError, match="749622c9"):
                gate.borrow("749622c9", purpose="mobile_perf.test")
        finally:
            other.seek(0)
            msvcrt.locking(other.fileno(), msvcrt.LK_UNLCK, 1)
            other.close()
        assert gate.held is False

    @pytestmark_file_lock
    def test_reentrant_pass_when_holder_pid_is_own_process(self, tmp_path):
        first = make_gate(tmp_path)
        token = first.borrow("749622c9", purpose="mobile_perf.embedded")
        token.__enter__()

        second = make_gate(tmp_path)
        with second.borrow("749622c9", purpose="mobile_perf.nested"):
            # 本进程已持锁：重入放行且 second 不另持句柄
            assert second.held is False

        token.__exit__(None, None, None)
        assert first.held is False


class TestNonWindowsPassthrough:
    def test_non_windows_borrow_passes_through_and_emits_warning(self, tmp_path):
        warnings = []
        gate = make_gate(
            tmp_path, platform="posix", on_warning=warnings.append
        )

        with gate.borrow("749622c9", purpose="mobile_perf.test"):
            pass

        assert (tmp_path / "leases").exists() is False  # 不触碰锁文件
        assert len(warnings) == 1
        assert warnings[0]["event"] == "gate_passthrough"
        assert warnings[0]["serial"] == "749622c9"

    def test_default_warning_handler_writes_stderr(self, tmp_path, capsys):
        gate = make_gate(tmp_path, platform="posix")

        with gate.borrow("749622c9", purpose="mobile_perf.test"):
            pass

        assert "gate_passthrough" in capsys.readouterr().err


class TestBusyMessage:
    @pytestmark_file_lock
    def test_busy_error_mentions_serial_and_purpose(self, tmp_path):
        import msvcrt

        gate = make_gate(tmp_path)
        lock_path, _ = lock_paths(tmp_path, "emulator-5554")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_bytes(b"")
        other = open(lock_path, "a+b")
        try:
            other.seek(0)
            msvcrt.locking(other.fileno(), msvcrt.LK_NBLCK, 1)
            with pytest.raises(GateBusyError, match="mobile_perf.doctor"):
                gate.borrow("emulator-5554", purpose="mobile_perf.doctor")
        finally:
            other.seek(0)
            msvcrt.locking(other.fileno(), msvcrt.LK_UNLCK, 1)
            other.close()
