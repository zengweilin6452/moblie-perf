"""真机 logwatch 验证（`-m device` 门控；无设备跳过且不计为通过）。

采集期间对目标应用人为制造一次 crash（`adb shell am crash`，只执行一次），
断言：
- events 表出现 kind=crash 记录，包名匹配目标；
- 事件 ts 落在会话窗口内（统一时间轴）；
- 监听为旁路：采集进行中宿主仍能短暂借用设备（短事务不被阻塞）。
"""

import datetime
import subprocess
import threading
import time

import pytest

from mobile_perf.adb import bundled_adb_path, list_devices
from mobile_perf.gate import FileLockGate
from mobile_perf.kit import PerfKit
from mobile_perf.store import PerfStore

pytestmark = pytest.mark.device

DEVICE_SERIAL = "749622c9"
TARGET_PACKAGE = "tv.danmaku.bili"
DURATION_S = 12.0
CRASH_AT_S = 5.0
BORROW_AT_S = 8.0


def _device_online() -> bool:
    adb_path = bundled_adb_path()
    if not adb_path.is_file():
        return False
    return DEVICE_SERIAL in list_devices(str(adb_path))


@pytest.fixture(scope="module")
def device_available():
    if not _device_online():
        pytest.skip(f"真机 {DEVICE_SERIAL} 不在线，device 验证未做")
    return DEVICE_SERIAL


def _shell(adb_path: str, serial: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [adb_path, "-s", serial, "shell", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )


def test_crash_during_collect_lands_on_timeline(device_available, tmp_path):
    adb_path = str(bundled_adb_path())
    store = PerfStore(tmp_path / "data" / "mobile_perf.db")
    kit = PerfKit(
        adb_path=adb_path,
        serial=device_available,
        runs_dir=tmp_path / "runs",
        store=store,
    )

    observations: dict[str, object] = {"borrowed": False, "crash_exit": None}

    def crash_and_probe():
        time.sleep(CRASH_AT_S)
        result = _shell(adb_path, device_available, "am", "crash", TARGET_PACKAGE)
        observations["crash_exit"] = result.returncode
        time.sleep(BORROW_AT_S - CRASH_AT_S)
        # 短事务验证：监听运行中，宿主仍应能即时借到设备
        try:
            with FileLockGate(tmp_path / "device-leases").borrow(
                device_available, purpose="test.short_borrow"
            ):
                observations["borrowed"] = True
        except Exception:  # noqa: BLE001 - 借用失败即验证失败
            pass

    worker = threading.Thread(target=crash_and_probe)
    worker.start()
    try:
        report = kit.run_collect(
            target=TARGET_PACKAGE,
            items=["CPU", "Memory"],
            duration_s=DURATION_S,
            runs_dir=tmp_path / "runs",
        )
    finally:
        worker.join()

    assert observations["borrowed"], "采集期间宿主短事务借用被阻塞"
    assert observations["crash_exit"] == 0, "am crash 执行失败，无法验证"

    run_id = report["runId"]
    crash_events = [
        event
        for event in store.fetch_events(run_id)
        if event["kind"] == "crash"
        and str(event["detail"].get("package", "")).startswith(TARGET_PACKAGE)
    ]
    assert crash_events, f"未捕获目标包 crash 事件: {store.fetch_events(run_id)}"

    started = datetime.datetime.fromisoformat(report["startedAt"])
    stopped = datetime.datetime.fromisoformat(report["stoppedAt"])
    for event in crash_events:
        ts = datetime.datetime.fromisoformat(event["ts"])
        assert started <= ts <= stopped, f"事件 ts 不在会话窗口内: {event['ts']}"
        assert event["detail"]["source"] in ("logcat_main", "logcat_events")
