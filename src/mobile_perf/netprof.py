"""网络剖面编排（工单 17）：把被测 App 的网络条件变成可配置、可复现的采集前置。

与 ``gate`` 同构的**环境编排**模块，不参与采集链路：起停 mitmproxy 子进程、建/拆
``adb reverse``、写/清设备系统代理。采集本身仍由 SoloPi 负责，本模块只在会话前后
改变网络条件。

三条真机实测约束（详见 docs/设计方案-M6-GPU与弱网.md §2.4）：

1. **mitmproxy 需 Python ≥3.12**（本项目运行时 ≥3.10）→ 只能作可选依赖，跑在独立
   解释器上，通过 subprocess 调用 ``netproxy_addon.py``；未安装时 ``not_tested`` 并
   给出安装提示，**不阻断**非弱网采集。
2. **不能用"页面能否打开"判断代理是否生效**——被测 App 有本地缓存与多域名兜底
   （实测：代理指向死端口 9999 时首屏仍完整加载）。生效证据**只看代理侧日志**，
   即 ``netproxy_addon`` 写出的 JSON Lines；日志里没有连接就是没生效，不假设成功。
3. **异常退出会把设备留在坏网络** → ``stop()`` 在异常路径上仍恢复；``doctor()`` 供
   启动时检查并清理残留代理/端口转发。

本模块只做**直通整形档**（不解密 TLS）：可得连接级耗时与字节数；接口级明细归
工单 18 的解密档。整形口径见 ``netproxy_addon`` 模块文档。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import metrics as metrics_module

# 内置档位预设：延迟(ms) / 下行(kbps) / 上行(kbps) / 丢包(%)
# 数值见 M6 方案 §6；宿主可用 overrides 覆盖，或走 custom 全量指定
PROFILE_PRESETS: dict[str, dict[str, float]] = {
    "none": {"latency_ms": 0.0, "down_kbps": 0.0, "up_kbps": 0.0, "loss_pct": 0.0},
    "4g": {"latency_ms": 50.0, "down_kbps": 4000.0, "up_kbps": 1000.0, "loss_pct": 0.0},
    "3g": {"latency_ms": 200.0, "down_kbps": 780.0, "up_kbps": 330.0, "loss_pct": 0.0},
    "weak": {"latency_ms": 400.0, "down_kbps": 400.0, "up_kbps": 200.0, "loss_pct": 2.0},
}
CUSTOM_PROFILE = "custom"
PROFILE_NAMES = (*PROFILE_PRESETS, CUSTOM_PROFILE)

_PARAM_KEYS = ("latency_ms", "down_kbps", "up_kbps", "loss_pct")

METRIC_CONN_DURATION = "net_conn_duration_ms"

DEFAULT_PORT = 8080
DEFAULT_START_TIMEOUT_S = 20.0
DEFAULT_STOP_TIMEOUT_S = 10.0
ADB_TIMEOUT_S = 15.0

# 代理侧日志文件名与 addon 脚本（随包分发，由独立解释器加载）
ADDON_FILENAME = "netproxy_addon.py"
LOG_FILENAME = "net_proxy.jsonl"

# 证据三态：observed=代理侧看到连接；none=代理起来了但没流量；unavailable=代理未起
EVIDENCE_OBSERVED = "observed"
EVIDENCE_NONE = "none"
EVIDENCE_UNAVAILABLE = "unavailable"


class NetProfileError(RuntimeError):
    """网络剖面起停失败（代理不可用、端口占用、重复 start 等）。"""


@dataclass(frozen=True)
class NetProfile:
    """一个网络剖面：档位名 + 生效参数（已规范化，可直接落库与比对）。"""

    name: str
    latency_ms: float
    down_kbps: float
    up_kbps: float
    loss_pct: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "latencyMs": self.latency_ms,
            "downKbps": self.down_kbps,
            "upKbps": self.up_kbps,
            "lossPct": self.loss_pct,
        }

    @property
    def is_passthrough(self) -> bool:
        """``none`` 档不施加任何整形。

        注意：**不等于不开代理**。直通档仍走代理，只是整形参数全 0——这样
        ``net_conn_duration_ms`` 的对照组与弱网组走同一条测量路径，两档才可比
        （工单 17 的验收正是"weak 明显高于 none"）。代理不可用时 passthrough
        可以不要代理，整形档则必须如实 ``not_tested``。
        """
        return (
            self.latency_ms == 0
            and self.down_kbps == 0
            and self.up_kbps == 0
            and self.loss_pct == 0
        )


def build_profile(name: str, overrides: dict[str, float] | None = None) -> NetProfile:
    """按档位名构造网络剖面；``custom`` 必须给全四个参数，预设档可部分覆盖。

    未知档位、未知参数、缺参数、负数、丢包 >100% 一律抛 ValueError——宁可早失败
    也不静默用一个错误的网络条件跑采集。
    """
    if name not in PROFILE_NAMES:
        raise ValueError(f"未知网络档位 {name!r}，可选: {', '.join(PROFILE_NAMES)}")
    overrides = {key: float(value) for key, value in (overrides or {}).items()}
    unknown = set(overrides) - set(_PARAM_KEYS)
    if unknown:
        raise ValueError(f"未知网络参数: {', '.join(sorted(unknown))}")

    if name == CUSTOM_PROFILE:
        missing = [key for key in _PARAM_KEYS if key not in overrides]
        if missing:
            raise ValueError(f"custom 档位缺少参数: {', '.join(missing)}")
        params = overrides
    else:
        params = {**PROFILE_PRESETS[name], **overrides}

    for key in _PARAM_KEYS:
        if params[key] < 0:
            raise ValueError(f"网络参数 {key} 不能为负，收到 {params[key]}")
    if params["loss_pct"] > 100:
        raise ValueError(f"丢包率不能超过 100%，收到 {params['loss_pct']}")
    return NetProfile(name=name, **params)


def profiles_are_comparable(
    left: dict[str, Any] | None, right: dict[str, Any] | None
) -> bool:
    """两个网络剖面是否可比（档位名与参数全等）。

    ``None``（未记录档位的历史 run）与任何档位都不可比——与设备档案的严格匹配
    语义一致，避免把不同网络条件的 run 混在一起比趋势。
    """
    if left is None or right is None:
        return left is None and right is None
    keys = ("name", "latencyMs", "downKbps", "upKbps", "lossPct")
    return all(left.get(key) == right.get(key) for key in keys)


# ---------- mitmproxy 探测（可选依赖，不在本进程 import） ----------


@dataclass
class ProxyAvailability:
    """mitmproxy 可用性探测结论；不可用时给出可直接执行的下一步。"""

    available: bool
    executable: str | None = None
    version: str | None = None
    reason: str | None = None
    hint: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "executable": self.executable,
            "version": self.version,
            "reason": self.reason,
            "hint": self.hint,
        }


def find_mitmproxy(
    *,
    candidates: list[str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    probe: Callable[[str], str | None] | None = None,
    env: dict[str, str] | None = None,
) -> ProxyAvailability:
    """寻找可用的 mitmdump（找不到返回可用性结论，不抛错）。

    探测顺序：显式候选 → 环境变量 ``MOBILE_PERF_MITMDUMP`` → PATH 上的 ``mitmdump``。
    ``probe`` 可注入（测试用），默认执行 ``--version``。
    """
    environ = os.environ if env is None else env
    probe = probe or _probe_version
    pool = list(candidates or [])
    env_value = environ.get("MOBILE_PERF_MITMDUMP")
    if env_value:
        pool.append(env_value)
    found = which("mitmdump")
    if found:
        pool.append(found)

    for path in pool:
        if not path:
            continue
        version = probe(path)
        if version:
            return ProxyAvailability(available=True, executable=path, version=version)
    return ProxyAvailability(
        available=False,
        reason="mitmproxy 不可用（未安装，或解释器版本低于其要求的 Python 3.12）",
        hint=(
            "弱网档位需要 mitmproxy：用 Python ≥3.12 建独立 venv 安装 mitmproxy 后，"
            "把 MOBILE_PERF_MITMDUMP 指向该 venv 的 mitmdump。"
            "未安装时弱网指标如实 not_tested，其余采集照常完成"
        ),
    )


def _probe_version(path: str) -> str | None:
    try:
        result = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in (result.stdout or "").splitlines():
        if line.lower().startswith("mitmproxy:"):
            return line.split(":", 1)[1].strip()
    return None


def port_in_use(port: int, host: str = "127.0.0.1", timeout_s: float = 0.5) -> bool:
    """端口上是否已有监听者。

    用 connect 探测而非 bind 探测：Windows 的 SO_REUSEADDR 允许抢占式 bind，
    绑定成功并不代表端口空闲，会误判。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout_s)
        try:
            sock.connect((host, port))
        except OSError:
            return False
    return True


# ---------- 代理侧日志：生效的唯一证据 ----------


@dataclass
class ProxyLogSummary:
    """从代理侧 JSON Lines 日志解析出的证据（纯函数产物，可直接落库）。"""

    connections: int = 0
    conn_durations_ms: list[float] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)
    connect_requests: int = 0
    http_requests: int = 0
    errors: int = 0
    bytes_down: int = 0
    bytes_up: int = 0
    params: dict[str, Any] | None = None

    @property
    def evidence(self) -> str:
        """代理侧是否真的看到了流量（隧道连接或明文请求任一即可）。"""
        return (
            EVIDENCE_OBSERVED
            if self.connections or self.http_requests
            else EVIDENCE_NONE
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "connections": self.connections,
            "connectRequests": self.connect_requests,
            "httpRequests": self.http_requests,
            "hosts": list(self.hosts),
            "errors": self.errors,
            "bytesDown": self.bytes_down,
            "bytesUp": self.bytes_up,
            "params": self.params,
        }


def analyze_proxy_log(lines: list[str]) -> ProxyLogSummary:
    """把 addon 的 JSON Lines 解析成证据结论；坏行跳过（不影响其余行）。

    连接耗时 = 该 TCP 隧道流 ``tcp_start`` → **首个客户端字节**（连接建立→首字节，
    对应 M6 方案 §5 的口径）。没有首字节的流（客户端连上就断）不计入耗时，
    但仍计入连接数——它确实是代理观察到的连接。

    明文 HTTP 请求只计入 ``http_requests``（作为生效证据），不参与连接耗时：
    它没有 CONNECT 隧道，口径不同，混在一起会污染分布。
    """
    summary = ProxyLogSummary()
    started: dict[str, float] = {}
    first_byte: set[str] = set()
    hosts: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        event = record.get("ev")
        if event == "params":
            summary.params = {k: v for k, v in record.items() if k not in ("t", "ev")}
        elif event == "tcp_start":
            flow_id = str(record.get("fid"))
            summary.connections += 1
            started[flow_id] = float(record.get("t") or 0.0)
            host = record.get("host")
            if host and host not in hosts:
                hosts.append(host)
        elif event == "tcp_message":
            flow_id = str(record.get("fid"))
            size = int(record.get("size") or 0)
            if record.get("from_client"):
                summary.bytes_up += size
            else:
                summary.bytes_down += size
            if record.get("from_client") and flow_id in started and flow_id not in first_byte:
                first_byte.add(flow_id)
                elapsed = float(record.get("t") or 0.0) - started[flow_id]
                if elapsed >= 0:
                    summary.conn_durations_ms.append(elapsed * 1000.0)
        elif event == "http_connect":
            summary.connect_requests += 1
            host = record.get("host")
            if host and host not in hosts:
                hosts.append(host)
        elif event == "http_request":
            summary.http_requests += 1
            host = record.get("host")
            if host and host not in hosts:
                hosts.append(host)
        elif event in ("tcp_error", "server_connect_error", "error"):
            summary.errors += 1
    summary.hosts = hosts
    return summary


def read_proxy_log(path: str | os.PathLike[str] | None) -> ProxyLogSummary:
    """读取日志文件并解析；文件不存在时返回空结论（= 没有观察到连接）。"""
    if not path:
        return ProxyLogSummary()
    try:
        with Path(path).open(encoding="utf-8") as handle:
            return analyze_proxy_log(handle.readlines())
    except OSError:
        return ProxyLogSummary()


def conn_duration_rows(summary: ProxyLogSummary) -> list[dict[str, Any]]:
    """连接耗时聚合行（会话级单样本）；没有连接返回空列表，不硬造 0 值。"""
    if not summary.conn_durations_ms:
        return []
    return [metrics_module.aggregate_row(METRIC_CONN_DURATION, summary.conn_durations_ms)]


# ---------- 生命周期编排 ----------


@dataclass
class NetSession:
    """一次弱网会话的结论：环境是否施加、证据、恢复情况与错误。

    ``applied`` 只表示**环境设置成功**（代理在跑、reverse 与系统代理已写）。
    是否**真的生效**看 ``evidence``：只有代理侧日志观察到连接才算 observed。
    """

    profile: dict[str, Any] | None = None
    applied: bool = False
    evidence: str = EVIDENCE_UNAVAILABLE
    proxy_started: bool = False
    reverse_set: bool = False
    proxy_set: bool = False
    setup_errors: list[str] = field(default_factory=list)
    restored: dict[str, Any] = field(default_factory=dict)
    log_summary: ProxyLogSummary = field(default_factory=ProxyLogSummary)
    log_path: str | None = None
    hint: str | None = None
    note: str | None = None
    started_at: float | None = None
    stopped_at: float | None = None

    @property
    def effective(self) -> bool:
        """环境施加成功且代理侧观察到连接——这才是"弱网确实生效"。"""
        return self.applied and self.evidence == EVIDENCE_OBSERVED

    @property
    def not_tested_reason(self) -> str | None:
        """未生效时的如实原因（含可执行下一步）；生效返回 None。"""
        if not self.applied:
            return self.setup_errors[0] if self.setup_errors else "网络剖面未施加"
        if self.evidence == EVIDENCE_OBSERVED:
            return None
        if self.note:
            return self.note
        return (
            "代理侧日志未观察到任何连接：被测 App 可能走缓存/未发起网络请求，"
            "或代理未被系统代理设置覆盖（不能以页面能否打开判断，需检查设备代理设置）"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "applied": self.applied,
            "effective": self.effective,
            "evidence": self.evidence,
            "notTestedReason": self.not_tested_reason,
            "hint": self.hint,
            "note": self.note,
            "durationS": (
                round(self.stopped_at - self.started_at, 3)
                if self.started_at is not None and self.stopped_at is not None
                else None
            ),
            "proxy": self.log_summary.as_dict(),
            "setupErrors": list(self.setup_errors),
            "restored": dict(self.restored),
            "logPath": self.log_path,
        }


DeviceCaller = Callable[..., Any]  # call_device(args, timeout_s) -> .stdout/.returncode


class NetProfiler:
    """网络剖面编排器：起代理 → 建 reverse → 设系统代理；退出必恢复。

    恢复顺序与设置相反（先清系统代理，再拆 reverse，最后停代理），每步独立记录
    成败——某一步失败不能让后续恢复被跳过，否则设备会被留在坏网络里。
    """

    def __init__(
        self,
        *,
        adb_path: str,
        serial: str,
        mitmdump: str | None = None,
        port: int = DEFAULT_PORT,
        call_device: DeviceCaller | None = None,
        popen: Callable[..., Any] = subprocess.Popen,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
        probe: Callable[[str], str | None] | None = None,
        which: Callable[[str], str | None] = shutil.which,
        in_use: Callable[[int], bool] = port_in_use,
        env: dict[str, str] | None = None,
    ):
        self.adb_path = adb_path
        self.serial = serial
        self.port = port
        self._explicit_mitmdump = mitmdump
        self._call = call_device or _default_call_device(adb_path)
        self._popen = popen
        self._sleep = sleep
        self._now = now
        self._probe = probe
        self._which = which
        self._in_use = in_use
        self._env = env
        self._process: Any | None = None
        self._availability: ProxyAvailability | None = None
        self._profile: NetProfile | None = None
        self._session = NetSession()
        self._log_path: str | None = None

    # ---- 可用性 ----

    def availability(self) -> ProxyAvailability:
        """探测 mitmproxy（结果缓存；探测失败不抛错，只返回不可用结论）。"""
        if self._availability is None:
            self._availability = find_mitmproxy(
                candidates=[self._explicit_mitmdump] if self._explicit_mitmdump else None,
                which=self._which,
                probe=self._probe,
                env=self._env,
            )
        return self._availability

    @property
    def session(self) -> NetSession:
        return self._session

    # ---- 生命周期 ----

    def start(self, profile: NetProfile, *, work_dir: str | os.PathLike[str]) -> NetSession:
        """施加网络剖面：起代理 → reverse → 系统代理（任一失败即回滚已做步骤）。

        ``none`` 档同样走代理（整形参数全 0），这样对照组与弱网组共用一条测量
        路径，``net_conn_duration_ms`` 两档才可比。区别只在 mitmproxy 缺失时：
        passthrough 可以不带代理继续（如实记 note），整形档必须 ``not_tested``。
        """
        if self._process is not None:
            raise NetProfileError("网络剖面已在运行，不能重复 start")
        self._profile = profile
        self._session = NetSession(
            profile=profile.as_dict(), started_at=self._now(), evidence=EVIDENCE_UNAVAILABLE
        )

        availability = self.availability()
        if not availability.available:
            if profile.is_passthrough:
                # 不整形就不需要代理证明：采集照常，只是没有连接级耗时
                self._session.applied = True
                self._session.evidence = EVIDENCE_OBSERVED
                self._session.note = (
                    "none 档无整形，未启动代理（mitmproxy 不可用）；"
                    "本 run 无 net_conn_duration_ms，但采集不受影响"
                )
                self._session.hint = availability.hint
                return self._session
            self._session.setup_errors.append(availability.reason or "mitmproxy 不可用")
            self._session.hint = availability.hint
            return self._session
        if self._in_use(self.port):
            self._session.setup_errors.append(f"端口 {self.port} 已被占用")
            return self._session

        work_path = Path(work_dir)
        work_path.mkdir(parents=True, exist_ok=True)
        self._log_path = str(work_path / LOG_FILENAME)
        try:
            self._spawn_proxy(availability.executable or "mitmdump", work_path)
        except Exception as exc:  # noqa: BLE001 - 起代理失败要如实回滚
            self._session.setup_errors.append(f"代理启动失败: {exc}")
            self._stop_proxy()
            return self._session
        self._session.proxy_started = True

        if not self._set_reverse(True):
            self._session.setup_errors.append("adb reverse 建立失败")
            self._rollback()
            return self._session
        self._session.reverse_set = True

        if not self._set_device_proxy(True):
            self._session.setup_errors.append("设备系统代理设置失败")
            self._rollback()
            return self._session
        self._session.proxy_set = True
        self._session.applied = True
        self._session.log_path = self._log_path
        return self._session

    def stop(self) -> NetSession:
        """恢复网络环境、回收代理，并以代理侧日志判定是否真的生效；幂等。"""
        if self._profile is None:
            return self._session
        self._session.restored = {
            "deviceProxy": self._set_device_proxy(False) if self._session.proxy_set else None,
            "reverse": self._set_reverse(False) if self._session.reverse_set else None,
            "proxyProcess": self._stop_proxy() if self._session.proxy_started else None,
        }
        # 只有真起过代理才用日志判定证据：none 档在无 mitmproxy 时按"无整形即
        # 无需证明"记 observed，不能被一份不存在的日志覆盖成 none
        if self._log_path and self._session.proxy_started:
            self._session.log_summary = read_proxy_log(self._log_path)
            if self._session.applied:
                self._session.evidence = self._session.log_summary.evidence
        self._session.stopped_at = self._now()
        self._session.proxy_set = False
        self._session.reverse_set = False
        self._session.proxy_started = False
        self._process = None
        return self._session

    def __enter__(self) -> NetProfiler:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.stop()
        return False  # 块内异常原样传播，但环境已恢复

    # ---- 代理进程 ----

    def _spawn_proxy(self, executable: str, work_path: Path) -> None:
        addon = Path(__file__).resolve().parent / ADDON_FILENAME
        if not addon.is_file():
            raise NetProfileError(f"整形 addon 缺失: {addon}")
        environment = dict(os.environ if self._env is None else self._env)
        environment["MOBILE_PERF_NETPARAMS"] = json.dumps(
            {key: getattr(self._profile, key) for key in _PARAM_KEYS}
        )
        environment["MOBILE_PERF_NETLOG"] = str(self._log_path)
        args = [
            executable,
            "-q",
            "--listen-port",
            str(self.port),
            "--set",
            "connection_strategy=lazy",
            "-s",
            str(addon),
        ]
        log = (work_path / "net_proxy.stdout.log").open("w", encoding="utf-8")
        self._process = self._popen(
            args, stdout=log, stderr=subprocess.STDOUT, text=True, env=environment
        )
        deadline = self._now() + DEFAULT_START_TIMEOUT_S
        while self._now() < deadline:
            if self._process.poll() is not None:
                raise NetProfileError("代理进程提前退出（详见 net_proxy.stdout.log）")
            if self._in_use(self.port):
                return  # 端口可连 = 已在监听
            self._sleep(0.2)
        raise NetProfileError("代理未在超时内开始监听")

    def _stop_proxy(self) -> bool:
        process = self._process
        if process is None:
            return True
        try:
            process.terminate()
            try:
                process.wait(timeout=DEFAULT_STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=DEFAULT_STOP_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - 进程可能已自己退出
            return False
        return True

    def _rollback(self) -> None:
        """start 中途失败：把已做的步骤倒序撤销。"""
        if self._session.proxy_set:
            self._set_device_proxy(False)
            self._session.proxy_set = False
        if self._session.reverse_set:
            self._set_reverse(False)
            self._session.reverse_set = False
        if self._session.proxy_started:
            self._stop_proxy()
            self._session.proxy_started = False

    # ---- 设备侧 ----

    def _set_reverse(self, enabled: bool) -> bool:
        target = f"tcp:{self.port}"
        args = ["-s", self.serial, "reverse"]
        args += [target, target] if enabled else ["--remove", target]
        return _ok(self._call(args, ADB_TIMEOUT_S))

    def _set_device_proxy(self, enabled: bool) -> bool:
        value = f"127.0.0.1:{self.port}" if enabled else ":0"
        args = ["-s", self.serial, "shell", "settings", "put", "global", "http_proxy", value]
        return _ok(self._call(args, ADB_TIMEOUT_S))


def _ok(result: Any) -> bool:
    return int(getattr(result, "returncode", 1)) == 0


def _default_call_device(adb_path: str) -> DeviceCaller:
    from .adb import default_call_device

    return default_call_device(adb_path)


# ---------- 残留清理（doctor） ----------


def doctor(
    call_device: DeviceCaller, serial: str, port: int = DEFAULT_PORT
) -> dict[str, Any]:
    """检查并清理上次异常退出遗留的代理设置/端口转发，并**复验**清理结果。

    返回 ``{"proxy", "reverse", "staleProxy", "staleReverse", "cleaned",
    "proxyClean", "reverseClean"}``。正常状态下调用无副作用；清理后复读一次，
    只有读回干净才置 ``*Clean=True``——清理命令返回 0 不等于设备真的干净。
    """
    before_proxy = _read_proxy(call_device, serial)
    before_reverse = _read_reverse(call_device, serial)
    stale_proxy = before_proxy not in ("", ":0", "null")
    stale_reverse = any(f"tcp:{port}" in item for item in before_reverse)

    if stale_proxy:
        call_device(
            ["-s", serial, "shell", "settings", "put", "global", "http_proxy", ":0"],
            ADB_TIMEOUT_S,
        )
    if stale_reverse:
        call_device(["-s", serial, "reverse", "--remove", f"tcp:{port}"], ADB_TIMEOUT_S)

    after_proxy = _read_proxy(call_device, serial) if stale_proxy else before_proxy
    after_reverse = _read_reverse(call_device, serial) if stale_reverse else before_reverse
    return {
        "proxy": before_proxy,
        "reverse": before_reverse,
        "staleProxy": stale_proxy,
        "staleReverse": stale_reverse,
        "cleaned": stale_proxy or stale_reverse,
        "proxyClean": after_proxy in ("", ":0", "null"),
        "reverseClean": not any(f"tcp:{port}" in item for item in after_reverse),
    }


def _read_proxy(call_device: DeviceCaller, serial: str) -> str:
    result = call_device(
        ["-s", serial, "shell", "settings", "get", "global", "http_proxy"], ADB_TIMEOUT_S
    )
    return (getattr(result, "stdout", "") or "").strip()


def _read_reverse(call_device: DeviceCaller, serial: str) -> list[str]:
    result = call_device(["-s", serial, "reverse", "--list"], ADB_TIMEOUT_S)
    return [
        line.strip()
        for line in (getattr(result, "stdout", "") or "").splitlines()
        if line.strip()
    ]


def dump_json(payload: dict[str, Any]) -> str:
    """稳定序列化（CLI 输出与测试断言共用）。"""
    return json.dumps(payload, ensure_ascii=False, indent=2)
