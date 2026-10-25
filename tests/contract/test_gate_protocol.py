"""DeviceGate 与真实 moblie-auto DeviceLease 的协议契约测试（防漂移）。

仅测试把 moblie-auto 源码插入 sys.path 导入真实 DeviceLease；产品代码零 import
（ADR-0001）。锁文件竞争不需要真机，但区域锁仅 Windows 可用。
"""

import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

MOBILE_AUTO_SRC = Path(
    os.environ.get(
        "MOBILE_AUTO_SRC", r"D:\workspace\claudecode-test\moblie-auto\src"
    )
)

pytestmark = [
    pytest.mark.skipif(os.name != "nt", reason="区域锁协议仅 Windows"),
    pytest.mark.skipif(
        not MOBILE_AUTO_SRC.exists(), reason="找不到 moblie-auto 源码目录"
    ),
]


def load_mobile_auto_lease():
    """把 moblie-auto 源码插入 sys.path 并导入真实 DeviceLease（仅测试如此）。"""
    if str(MOBILE_AUTO_SRC) not in sys.path:
        sys.path.insert(0, str(MOBILE_AUTO_SRC))
    from mobile_auto.core.lease import DeviceLease
    from mobile_auto.errors import DeviceBusy

    return DeviceLease, DeviceBusy


def expected_lock_path(lease_dir: Path, serial: str) -> Path:
    # 独立真值：按协议文档重算摘要，而非复用任一方实现的代码
    digest = hashlib.sha256(serial.encode("utf-8")).hexdigest()[:24]
    return lease_dir / f"{digest}.lock"


def test_gate_and_device_lease_derive_the_same_lock_path(tmp_path):
    from mobile_perf.gate import FileLockGate

    DeviceLease, _ = load_mobile_auto_lease()
    serial = "contract-path-1"

    gate = FileLockGate(tmp_path, serial=serial)
    lease = DeviceLease(serial, tmp_path)

    assert gate.path == expected_lock_path(tmp_path, serial)
    assert lease.path == gate.path
    assert lease.holder_path == gate.holder_path


HOLD_LEASE_SCRIPT = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from mobile_auto.core.lease import DeviceLease

serial = sys.argv[2]
ready_path, release_path, lease_dir = Path(sys.argv[3]), Path(sys.argv[4]), Path(sys.argv[5])
lease = DeviceLease(serial, lease_dir)
lease.acquire()
try:
    ready_path.write_text("1", encoding="utf-8")
    while release_path.exists():
        time.sleep(0.05)
finally:
    lease.release()
"""


def _wait_for(path: Path, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    pytest.fail(f"等待超时: {path}")


def test_device_lease_held_by_other_process_blocks_gate_borrow_immediately(tmp_path):
    """方向 1（真跨进程）：moblie-auto DeviceSession 持锁期间 collect 借用 → 立即 GateBusyError。

    同进程持有会被 pid 重入判定放行（嵌入模式语义），所以必须用子进程持有租约。
    """
    from mobile_perf.gate import FileLockGate, GateBusyError

    serial = "contract-busy-1"
    ready, release = tmp_path / "ready", tmp_path / "release"
    release.write_text("1", encoding="utf-8")
    proc = subprocess.Popen(  # noqa: S603 - 固定脚本与参数，无外部输入
        [
            sys.executable,
            "-c",
            HOLD_LEASE_SCRIPT,
            str(MOBILE_AUTO_SRC),
            serial,
            str(ready),
            str(release),
            str(tmp_path),
        ]
    )
    try:
        _wait_for(ready)
        with pytest.raises(GateBusyError, match=serial):
            FileLockGate(tmp_path).borrow(serial, purpose="mobile_perf.start")
    finally:
        release.unlink(missing_ok=True)
        proc.wait(timeout=15)

    # DeviceSession 释放后立即可借：短事务语义
    with FileLockGate(tmp_path).borrow(serial, purpose="mobile_perf.start"):
        pass


def test_gate_held_blocks_device_lease_acquire(tmp_path):
    from mobile_perf.gate import FileLockGate

    DeviceLease, DeviceBusy = load_mobile_auto_lease()
    serial = "contract-busy-2"
    gate = FileLockGate(tmp_path)
    token = gate.borrow(serial, purpose="mobile_perf.stop_pull")
    token.__enter__()
    try:
        lease = DeviceLease(serial, tmp_path)
        with pytest.raises(DeviceBusy):
            lease.acquire()
    finally:
        token.__exit__(None, None, None)

    # 我方释放后 moblie-auto 立即可获取：collect 录制中不持锁的对偶验证
    DeviceLease(serial, tmp_path).acquire()


def test_device_lease_read_holder_sees_gate_purpose_and_pid(tmp_path):
    from mobile_perf.gate import FileLockGate

    load_mobile_auto_lease()
    from mobile_auto.core.lease import read_holder

    serial = "contract-holder-1"
    gate = FileLockGate(tmp_path)
    with gate.borrow(serial, purpose="mobile_perf.doctor"):
        holder = read_holder(serial, tmp_path)

    assert holder is not None
    assert holder["purpose"] == "mobile_perf.doctor"
    assert holder["pid"] == os.getpid()
