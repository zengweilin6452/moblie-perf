"""巡检（Keeper）：设备可用性内聚（Q11/Q12）——doctor 巡检、tcpip 自愈、
无障碍/充电/前台检查、时钟校准。

设计约束（设计方案 §4 #3/#4、§6.1/6.2）：
- 所有巡检动作都是短事务（由调用方注入的 call_device/run_doctor 走 gate 借用），
  巡检不持有设备权，不阻塞宿主长任务；
- ensure_ready 幂等可重入：设备健康时绝不触发 tcpip/connect 等状态改变动作，
  自愈仅在 doctor 失败或设备离线时执行一次并重试；
- 无障碍"假开启"（MIUI：设置里已开但服务未 Bound）无法代开——如实标
  needs_attention 并产生事件，由人工处理；
- 充电时只降级不失败：Battery/温度项由编排层按 degraded_items 标 not_tested；
- 弹窗开关确认走票 06 的自编译 App 控制面（adb forward + 本机 HTTP 读回），
  kit 默认接线 default_dialog_probe；直接构造 Keeper 未注入时跳过。
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import adb, solo

# SoloPi 无障碍服务的类路径片段。真机（749622c9）dumpsys 的 Enabled services
# 行显示运行时组件为 com.alipay.hulu/com.alipay.hulu.shared.event.accessibility.
# AccessibilityServiceImpl；片段不含 .shared. 前缀以同时命中 portal/shared 变体。
SOLOPI_ACCESSIBILITY_SERVICE = "event.accessibility.AccessibilityServiceImpl"

# 充电时降级为 not_tested 的采集项（温度项对应热补充工单的口径）。
BATTERY_DEGRADED_ITEMS = ("Battery", "Temperature")

CLOCK_OFFSET_WARN_S = 2.0
DIALOG_PROBE_SKIPPED_REASON = "未注入弹窗开关探针，跳过确认"

# 自编译 App 控制面（票 06）：设备端 HTTP 服务端口与开关读回端点。
# 读走本机 HTTP（list/get 只读）；set 只允许 ADB Scheme Activity，探针不写。
DIALOG_PROBE_CONFIG_KEY = "KEY_SUPPRESS_INFO_DIALOG"
DEFAULT_CONTROL_PORT = 23342
PROBE_HTTP_TIMEOUT_S = 3.0

TCP_IP_PORT = 5555
SHELL_TIMEOUT_S = 15.0
CONNECT_TIMEOUT_S = 10.0

DeviceCaller = Callable[..., Any]  # call_device(args, timeout_s) -> returncode/stdout/stderr
DoctorFn = Callable[[], Any]  # run_doctor() -> success/data（HarnessResult 兼容）
DialogProbe = Callable[[], "CheckResult"]

TCP_SERIAL_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}:\d+$")


class CheckResult:
    """单项巡检结论：status ∈ ok / warn / fail / skipped。"""

    def __init__(self, name: str, status: str, detail: dict[str, Any] | None = None):
        self.name = name
        self.status = status
        self.detail = dict(detail or {})

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass(frozen=True)
class ReadinessReport:
    """一次巡检（含自愈）的完整结论，供编排层降级决策与入库。"""

    serial: str
    ok: bool
    needs_attention: bool
    healed: bool
    checks: tuple[CheckResult, ...]
    events: tuple[dict[str, Any], ...]
    degraded_items: tuple[str, ...]
    foreground_package: str | None
    clock_offset_s: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "serial": self.serial,
            "ok": self.ok,
            "needsAttention": self.needs_attention,
            "healed": self.healed,
            "degradedItems": list(self.degraded_items),
            "foregroundPackage": self.foreground_package,
            "clockOffsetS": self.clock_offset_s,
            "checks": [c.as_dict() for c in self.checks],
            "events": list(self.events),
        }


# ---------- 决策纯函数：吃 stdout 样本，不碰设备 ----------


def parse_devices_stdout(stdout: str) -> list[str]:
    """解析 `adb devices` 输出，返回 device 状态的 serial 列表。"""
    devices: list[str] = []
    for line in (stdout or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    return devices


def parse_battery_charging(stdout: str) -> bool | None:
    """从 `dumpsys battery` 判充电中；输出不可解析返回 None（不猜）。"""
    powered = False
    status: int | None = None
    seen = False
    for line in (stdout or "").splitlines():
        flag = re.match(r"\s*(AC|USB|Wireless|Dock) powered:\s*(true|false)", line, re.I)
        if flag:
            seen = True
            powered = powered or flag.group(2).lower() == "true"
        state = re.match(r"\s*status:\s*(-?\d+)", line)
        if state:
            seen = True
            status = int(state.group(1))
    if not seen:
        return None
    # 2=charging, 5=full（仍插电）：插着电就不测 Battery/温度
    return powered or status in (2, 5)


BOUND_SERVICE_ENTRY_RE = re.compile(r"Service\s*[\[{]")
SECTION_HEADER_RE = re.compile(r"\w[\w ]*services:")


def _bound_section_text(lines: list[str]) -> str:
    """截取 `Bound services:` 段：同行花括号内容 + 后续更深缩进的行。"""
    for index, line in enumerate(lines):
        if "bound services" not in line.lower():
            continue
        indent = len(line) - len(line.lstrip())
        parts = [line.split(":", 1)[1] if ":" in line else ""]
        for inner in lines[index + 1:]:
            if not inner.strip():
                continue
            if SECTION_HEADER_RE.search(inner) or (len(inner) - len(inner.lstrip())) <= indent:
                break  # 进入下一个段（Enabled/Binding 等）
            parts.append(inner)
        return "\n".join(parts)
    return ""


def parse_accessibility_bound(stdout: str, service_fragment: str) -> bool | None:
    """无障碍服务是否 Bound。

    True=Bound 段有服务条目且输出可见 SoloPi 服务；False=未绑定（含
    Bound 段为空的"假开启"，或绑定了别的服务）；None=输出不是
    accessibility dump（如空输出/设备异常），无法判定。

    真机格式差异：部分 Android 版本 Bound 条目只写 label（无组件名），
    组件名仅出现在 Enabled services——因此"是否我们的服务"以整个 dump
    中出现服务组件为准，"是否绑定"以 Bound 段非空为准。
    """
    lines = (stdout or "").splitlines()
    lowered = [line.lower() for line in lines]
    if not any("bound services" in ln or "enabled services" in ln for ln in lowered):
        return None
    mentions_service = any(service_fragment in line for line in lines)
    bound_entry = bool(BOUND_SERVICE_ENTRY_RE.search(_bound_section_text(lines)))
    return bound_entry and mentions_service


FOREGROUND_RE = re.compile(
    r"(?:topResumedActivity|mResumedActivity|ResumedActivity).*?u0\s+([\w.]+)/"
)

# 同源行的组件名（pkg/cls，cls 允许相对形式 .Foo）——前台还原用它恢复
# 采集前的具体页面，而不是把应用重置到启动页
FOREGROUND_COMPONENT_RE = re.compile(
    r"(?:topResumedActivity|mResumedActivity|ResumedActivity).*?u0\s+([\w.]+/[\w.$]+)"
)


def parse_foreground_package(stdout: str) -> str | None:
    """从 `dumpsys activity activities` 提取当前前台包名；读不到返回 None。"""
    match = FOREGROUND_RE.search(stdout or "")
    return match.group(1) if match else None


def parse_foreground_component(stdout: str) -> str | None:
    """从同一 dumpsys 输出提取前台组件（pkg/cls）；读不到返回 None。"""
    match = FOREGROUND_COMPONENT_RE.search(stdout or "")
    return match.group(1) if match else None


WLAN_SRC_RE = re.compile(r"dev wlan0.*?src (\d{1,3}(?:\.\d{1,3}){3})")


def parse_wlan_ip(stdout: str) -> str | None:
    """从 `ip route` 输出取 wlan0 的 src 地址，供 `adb connect` 补建。"""
    match = WLAN_SRC_RE.search(stdout or "")
    return match.group(1) if match else None


def evaluate_clock_offset(
    *, device_epoch_s: float, host_epoch_s: float, threshold_s: float = CLOCK_OFFSET_WARN_S
) -> dict[str, Any]:
    """device-host 时钟偏移结论：>threshold 秒记 warning（Q12）。"""
    offset_s = round(host_epoch_s - device_epoch_s, 3)
    return {"offsetS": offset_s, "warn": abs(offset_s) > threshold_s}


def is_tcp_serial(serial: str) -> bool:
    """serial 形如 ip:port 时，断线后只需 `adb connect` 补建。"""
    return bool(TCP_SERIAL_RE.match(serial))


# ---------- 默认设备通道（经 gate 借用后由 kit/CLI 包装调用） ----------


def default_call_device(adb_path: str) -> DeviceCaller:
    """默认 adb 调用：args 为 adb 可执行文件之后的完整参数。"""

    def call(args: list[str], timeout_s: float = SHELL_TIMEOUT_S) -> Any:
        return adb._adb_runner([adb_path, *args], timeout_s)

    return call


def default_run_doctor(
    adb_path: str, serial: str, repo_root: str | None = None
) -> DoctorFn:
    """默认 doctor：借用 Harness 的 doctor 契约（快照版本，升级只回归 solo 模块测试）。"""
    return lambda: solo.doctor(adb_path, serial, repo_root=repo_root)


def parse_config_get_response(body: str) -> bool | None:
    """解析控制面 `GET /scheme/config?action=get` 响应，返回开关当前值。

    响应形如 {"success": true, "config": {..., "value": true}}；非敏感 key 才
    带 value。任何不可解析/失败响应返回 None（不猜）。
    """
    try:
        data = json.loads(body or "")
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("success") is not True:
        return None
    config = data.get("config")
    if not isinstance(config, dict) or "value" not in config:
        return None
    value = config["value"]
    return value if isinstance(value, bool) else None


def _urllib_get(url: str, timeout_s: float) -> str:
    with urllib.request.urlopen(url, timeout=timeout_s) as response:
        return response.read().decode("utf-8", "replace")


def default_dialog_probe(
    call_device: DeviceCaller,
    serial: str,
    *,
    port: int = DEFAULT_CONTROL_PORT,
    fetch: Callable[[str, float], str] | None = None,
) -> DialogProbe:
    """默认弹窗开关探针：adb forward 控制端口 → 本机 HTTP 读回开关值。

    call_device 应为（可能经 gate 借用的）设备命令通道，forward 是探针唯一
    的设备动作；fetch 可注入替身做无网络单测。
    """

    def probe() -> CheckResult:
        forward = call_device(
            ["-s", serial, "forward", f"tcp:{port}", f"tcp:{port}"]
        )
        if forward.returncode != 0:
            return CheckResult(
                "dialog_probe",
                "fail",
                {"error": "adb forward 失败", "stderr": (forward.stderr or "").strip()},
            )
        url = (
            f"http://127.0.0.1:{port}/scheme/config"
            f"?action=get&key={DIALOG_PROBE_CONFIG_KEY}"
        )
        try:
            body = (fetch if fetch is not None else _urllib_get)(
                url, PROBE_HTTP_TIMEOUT_S
            )
        except Exception as exc:  # noqa: BLE001 - 控制面不可达如实 fail
            return CheckResult(
                "dialog_probe",
                "fail",
                {"error": f"控制面 HTTP 不可达: {exc}", "port": port},
            )
        value = parse_config_get_response(body)
        if value is None:
            return CheckResult(
                "dialog_probe",
                "fail",
                {"error": "控制面响应不可解析", "port": port},
            )
        return CheckResult(
            "dialog_probe",
            "ok" if value else "warn",
            {"suppressed": value, "port": port},
        )

    return probe


class Keeper:
    """一次设备巡检（含连接自愈）。幂等可重入：健康设备零状态改变动作。

    call_device / run_doctor 由调用方注入并负责 gate 借用；now 注入返回
    host epoch 秒（时钟校准的宿主侧基准）。
    """

    def __init__(
        self,
        *,
        adb_path: str,
        serial: str,
        call_device: DeviceCaller | None = None,
        run_doctor: DoctorFn | None = None,
        dialog_probe: DialogProbe | None = None,
        now: Callable[[], float] = time.time,
        service_fragment: str = SOLOPI_ACCESSIBILITY_SERVICE,
    ):
        self.adb_path = adb_path
        self.serial = serial
        self._call = call_device or default_call_device(adb_path)
        self._doctor_fn = run_doctor or default_run_doctor(adb_path, serial)
        self._dialog_probe = dialog_probe
        self._now = now
        self._service = service_fragment

    # -- 设备命令快捷方式 --

    def _shell(self, *args: str, timeout_s: float = SHELL_TIMEOUT_S) -> Any:
        return self._call(["-s", self.serial, "shell", *args], timeout_s)

    def _devices(self) -> list[str]:
        result = self._call(["devices"], CONNECT_TIMEOUT_S)
        return parse_devices_stdout(result.stdout or "")

    def _online(self) -> bool:
        return self.serial in self._devices()

    def _event(self, kind: str, detail: dict[str, Any]) -> dict[str, Any]:
        return {
            "kind": kind,
            "ts": datetime.fromtimestamp(self._now())
            .astimezone()
            .isoformat(timespec="seconds"),
            "detail": detail,
        }

    # -- 巡检流程 --

    def run(self) -> ReadinessReport:
        checks: list[CheckResult] = []
        events: list[dict[str, Any]] = []
        healed = False

        online = self._online()
        doctor_ok, doctor_detail = self._doctor_attempt()
        if not online or not doctor_ok:
            heal_event = self._heal()
            healed = True
            online = self._online()
            doctor_ok, doctor_detail = self._doctor_attempt()  # 自愈后重试一次
            heal_event["detail"]["restored"] = online and doctor_ok
            events.append(heal_event)

        ok = online and doctor_ok
        checks.append(
            CheckResult("connection", "ok" if online else "fail", {"serial": self.serial})
        )
        checks.append(
            CheckResult("harness_doctor", "ok" if doctor_ok else "fail", doctor_detail)
        )

        acc_fail, needs_attention = self._accessibility_check(checks, events)
        ok = ok and not acc_fail

        degraded_items: tuple[str, ...] = ()
        charging = self._battery_check(checks)
        if charging:
            degraded_items = BATTERY_DEGRADED_ITEMS

        foreground = self._foreground_check(checks)
        clock_offset = self._clock_check(checks, events)
        self._dialog_probe_check(checks)

        return ReadinessReport(
            serial=self.serial,
            ok=ok,
            needs_attention=needs_attention,
            healed=healed,
            checks=tuple(checks),
            events=tuple(events),
            degraded_items=degraded_items,
            foreground_package=foreground,
            clock_offset_s=clock_offset,
        )

    def _doctor_attempt(self) -> tuple[bool, dict[str, Any]]:
        try:
            result = self._doctor_fn()
        except Exception as exc:  # Harness 无 JSON 契约/子进程异常都视为 doctor 未通过
            return False, {"error": str(exc)}
        data = getattr(result, "data", {}) or {}
        detail = {
            key: data[key]
            for key in ("failedChecks", "missingPermissions", "error")
            if key in data
        }
        return bool(getattr(result, "success", False)), detail

    def _heal(self) -> dict[str, Any]:
        """连接自愈：doctor 失败或离线 → tcpip 补建 → connect（重试判定由 run 负责，§4 #3）。"""
        actions: list[str] = []
        if is_tcp_serial(self.serial):
            self._call(["connect", self.serial], CONNECT_TIMEOUT_S)
            actions.append(f"connect {self.serial}")
        else:
            tcpip = self._call(
                ["-s", self.serial, "tcpip", str(TCP_IP_PORT)], CONNECT_TIMEOUT_S
            )
            actions.append(f"tcpip {TCP_IP_PORT} (exit={tcpip.returncode})")
            if tcpip.returncode == 0:
                route = self._shell("ip", "route")
                ip = parse_wlan_ip(route.stdout or "")
                if ip:
                    self._call(["connect", f"{ip}:{TCP_IP_PORT}"], CONNECT_TIMEOUT_S)
                    actions.append(f"connect {ip}:{TCP_IP_PORT}")
        return self._event("tcpip_heal", {"actions": actions, "restored": None})

    def _accessibility_check(
        self, checks: list[CheckResult], events: list[dict[str, Any]]
    ) -> tuple[bool, bool]:
        """无障碍"假开启"检测：Bound 空如实暴露，不代开（§4 #4）。

        返回 (是否阻断就绪, 是否 needs_attention)；dumpsys 无法解析时阻断但不标
        needs_attention（分不清是假开启还是设备异常，不猜）。
        """
        result = self._shell("dumpsys", "accessibility")
        bound = parse_accessibility_bound(result.stdout or "", self._service)
        if bound is True:
            checks.append(CheckResult("accessibility", "ok", {"bound": True}))
            return False, False
        if bound is None:
            checks.append(
                CheckResult(
                    "accessibility",
                    "fail",
                    {"bound": None, "error": "dumpsys accessibility 输出无法解析"},
                )
            )
            return True, False
        checks.append(CheckResult("accessibility", "fail", {"bound": False}))
        events.append(
            self._event(
                "accessibility_needs_attention",
                {"service": self._service, "boundServicesEmpty": True},
            )
        )
        return True, True

    def _battery_check(self, checks: list[CheckResult]) -> bool | None:
        result = self._shell("dumpsys", "battery")
        charging = parse_battery_charging(result.stdout or "")
        if charging is None:
            checks.append(
                CheckResult("battery", "warn", {"charging": None, "error": "输出无法解析"})
            )
            return None
        checks.append(CheckResult("battery", "warn" if charging else "ok", {"charging": charging}))
        return charging

    def _foreground_check(self, checks: list[CheckResult]) -> str | None:
        result = self._shell("dumpsys", "activity", "activities")
        package = parse_foreground_package(result.stdout or "")
        checks.append(CheckResult("foreground", "ok" if package else "warn", {"package": package}))
        return package

    def _clock_check(
        self, checks: list[CheckResult], events: list[dict[str, Any]]
    ) -> float | None:
        result = self._shell("date", "+%s")
        try:
            device_epoch = float((result.stdout or "").strip())
        except ValueError:
            checks.append(
                CheckResult("clock_offset", "warn", {"error": "device epoch 不可读"})
            )
            return None
        verdict = evaluate_clock_offset(device_epoch_s=device_epoch, host_epoch_s=self._now())
        checks.append(CheckResult("clock_offset", "warn" if verdict["warn"] else "ok", verdict))
        if verdict["warn"]:
            events.append(self._event("clock_offset", dict(verdict)))
        return verdict["offsetS"]

    def _dialog_probe_check(self, checks: list[CheckResult]) -> None:
        if self._dialog_probe is None:
            checks.append(
                CheckResult("dialog_probe", "skipped", {"reason": DIALOG_PROBE_SKIPPED_REASON})
            )
            return
        checks.append(self._dialog_probe())


__all__ = [
    "BATTERY_DEGRADED_ITEMS",
    "CheckResult",
    "Keeper",
    "ReadinessReport",
    "default_call_device",
    "default_dialog_probe",
    "default_run_doctor",
    "evaluate_clock_offset",
    "is_tcp_serial",
    "parse_accessibility_bound",
    "parse_battery_charging",
    "parse_config_get_response",
    "parse_devices_stdout",
    "parse_foreground_package",
    "parse_foreground_component",
    "parse_wlan_ip",
]
