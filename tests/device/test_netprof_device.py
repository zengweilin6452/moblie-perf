"""弱网编排真机门控（ticket 17）：两档对比、设备可恢复、doctor 复验。

无设备或无 mitmproxy 自动跳过（`-m device` 不计为通过）。三件事：
① 代理链路真通——被测 App 的流量出现在**代理侧日志**里（不用页面表现判断）；
② `weak` 档的连接耗时明显高于 `none` 档（对照组也走代理，两档同一测量路径）；
③ 会话结束后设备代理与 reverse 已恢复，`netprof.doctor` 复验干净。

目标包默认 com.sinosoft.trainapp，可用 MOBILE_PERF_DEVICE_TARGET 覆盖；
mitmdump 路径走 MOBILE_PERF_MITMDUMP > PATH。
"""

import os
import statistics
import time

import pytest

from mobile_perf import netprof
from mobile_perf.adb import bundled_adb_path, default_call_device, list_devices

pytestmark = pytest.mark.device

DEVICE_SERIAL = os.environ.get("MOBILE_PERF_DEVICE_SERIAL", "749622c9")
TARGET_PACKAGE = os.environ.get("MOBILE_PERF_DEVICE_TARGET", "com.sinosoft.trainapp")
PORT = int(os.environ.get("MOBILE_PERF_NET_PORT", "8080"))

# weak 档连接耗时至少是对照组的这个倍数才算"明显高于"（400ms 固定延迟 + 整形，
# 实测差距远大于此；取保守阈值避免机器抖动导致假失败）
MIN_RATIO = 1.5
# 每档驱动流量后的观察窗口（秒）：够 App 发首屏请求
DRIVE_SECONDS = 12.0


def _device_online() -> bool:
    adb_path = bundled_adb_path()
    if not adb_path.is_file():
        return False
    return DEVICE_SERIAL in list_devices(str(adb_path))


MITMDUMP = netprof.find_mitmproxy().executable

pytestmark = [
    pytest.mark.device,
    pytest.mark.skipif(not _device_online(), reason="设备不在线"),
    pytest.mark.skipif(MITMDUMP is None, reason="本机无 mitmdump（Python ≥3.12 独立 venv）"),
]


@pytest.fixture(scope="module")
def caller():
    return default_call_device(str(bundled_adb_path()))


def _shell(caller, *args, timeout_s=20.0):
    return caller(["-s", DEVICE_SERIAL, "shell", *args], timeout_s)


def _measure(caller, tmp_path, profile_name, run_id):
    """起一档弱网 → 驱动目标 App 网络流量 → 恢复 → 返回会话结论。"""
    profile = netprof.build_profile(profile_name)
    profiler = netprof.NetProfiler(
        adb_path=str(bundled_adb_path()),
        serial=DEVICE_SERIAL,
        mitmdump=MITMDUMP,
        port=PORT,
        call_device=caller,
    )
    work_dir = tmp_path / run_id
    try:
        session = profiler.start(profile, work_dir=work_dir)
        assert session.applied, f"{profile_name} 档未施加: {session.setup_errors}"
        # 冷启目标 App 触发首屏网络请求（同一次操作两档复用，保证可比）
        _shell(caller, "am", "force-stop", TARGET_PACKAGE)
        _shell(caller, "monkey", "-p", TARGET_PACKAGE, "1")
        time.sleep(DRIVE_SECONDS)
        # 滚动一下，触发列表页的接口请求
        _shell(caller, "input", "swipe", "540", "1600", "540", "600", "300")
        time.sleep(3.0)
    finally:
        session = profiler.stop()
    return session


def test_weak_profile_is_clearly_slower_than_passthrough(caller, tmp_path):
    """核心验收：weak 档连接耗时明显高于 none 档，且两档都真的抓到了流量。"""
    baseline = _measure(caller, tmp_path, "none", "net-none")
    weak = _measure(caller, tmp_path, "weak", "net-weak")

    assert baseline.effective, f"none 档未抓到流量: {baseline.not_tested_reason}"
    assert weak.effective, f"weak 档未抓到流量: {weak.not_tested_reason}"
    assert weak.log_summary.params["latencyMs"] == 400.0

    baseline_median = statistics.median(baseline.log_summary.conn_durations_ms)
    weak_median = statistics.median(weak.log_summary.conn_durations_ms)
    assert weak_median > baseline_median * MIN_RATIO, (
        f"weak 中位 {weak_median:.1f}ms 未明显高于 none 中位 {baseline_median:.1f}ms"
        f"（要求 ≥{MIN_RATIO}×）"
    )


def test_device_restored_after_profile(caller, tmp_path):
    """退出必恢复：系统代理回到 :0、reverse 已拆，doctor 复验干净。"""
    session = _measure(caller, tmp_path, "weak", "net-restore")

    assert session.restored["deviceProxy"] is True
    assert session.restored["reverse"] is True
    assert session.restored["proxyProcess"] is True

    report = netprof.doctor(caller, DEVICE_SERIAL, PORT)
    assert report["proxyClean"] is True
    assert report["reverseClean"] is True


def test_doctor_cleans_residual_proxy_on_real_device(caller):
    """doctor 真机：手工写坏代理后能被发现并清理，复验干净。"""
    try:
        _shell(caller, "settings", "put", "global", "http_proxy", f"127.0.0.1:{PORT}")
        stale = netprof.doctor(caller, DEVICE_SERIAL, PORT)
        assert stale["staleProxy"] is True
        assert stale["cleaned"] is True
        assert stale["proxyClean"] is True

        clean = netprof.doctor(caller, DEVICE_SERIAL, PORT)
        assert clean["cleaned"] is False  # 正常状态调用无副作用
    finally:
        _shell(caller, "settings", "put", "global", "http_proxy", ":0")


def test_proxy_log_records_real_hosts_and_params(caller, tmp_path):
    """代理侧日志给出域名与整形参数——这是"代理真的在中间"的直接证据。"""
    session = _measure(caller, tmp_path, "3g", "net-3g")

    assert session.effective, session.not_tested_reason
    assert session.log_summary.hosts, "代理侧未记录任何域名"
    assert session.log_summary.params["latencyMs"] == 200.0
    assert session.log_summary.conn_durations_ms


def test_decrypt_tier_degrades_honestly_on_untrusted_app(caller, tmp_path):
    """解密档真机（ticket 18）：targetSdk ≥24 的正式包不信任用户 CA。

    期望**走降级分支**——接口级指标如实 not_tested、原因写明证书问题，
    且连接级指标仍然有值（判定后自动降为直通）。这是正确行为而非缺陷。

    若换到满足前置条件的组合（可写 system 分区的模拟器 + 系统 CA / debug 包
    信任用户 CA / root 设备），本用例会走"有接口级数据"分支——两个分支都断言。
    """
    profile = netprof.build_profile("weak")
    profiler = netprof.NetProfiler(
        adb_path=str(bundled_adb_path()),
        serial=DEVICE_SERIAL,
        mitmdump=MITMDUMP,
        port=PORT,
        tier=netprof.TIER_DECRYPT,
        call_device=caller,
    )
    work_dir = tmp_path / "net-decrypt"
    try:
        session = profiler.start(profile, work_dir=work_dir)
        assert session.applied, session.setup_errors
        _shell(caller, "am", "force-stop", TARGET_PACKAGE)
        _shell(caller, "monkey", "-p", TARGET_PACKAGE, "1")
        time.sleep(DRIVE_SECONDS)
        _shell(caller, "input", "swipe", "540", "1600", "540", "600", "300")
        time.sleep(3.0)
    finally:
        session = profiler.stop()

    assert session.tier == netprof.TIER_DECRYPT
    assert session.effective, session.not_tested_reason  # 代理确实在中间
    # 连接级指标与信任结论无关：降级后隧道仍被观测到
    assert session.log_summary.conn_durations_ms

    if session.trust == netprof.TRUST_REJECTED:
        assert session.api_effective is False
        assert "不信任代理 CA" in session.api_not_tested_reason
        assert session.log_summary.api_records == []  # 不产出任何接口数值
        assert profiler.api_breakdown() is None
    else:
        # 满足前置条件的组合：接口级数据必须真的落地
        assert session.trust == netprof.TRUST_OK, session.api_not_tested_reason
        assert session.api_effective is True
        assert session.log_summary.api_records
        breakdown = profiler.api_breakdown()
        assert breakdown["top"] and breakdown["byHost"]

