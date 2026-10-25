"""门面：组装 adb 解析、Harness 适配层、采集会话与归档，提供 run_collect 编排。"""

from __future__ import annotations

import dataclasses
import logging
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from . import collector, solo
from . import metrics as metrics_module
from . import report as report_module
from . import supplement as supplement_module
from .adb import default_repo_root, resolve_adb
from .gate import DeviceGate, FileLockGate
from .judge import JudgeSpec, Verdict
from .judge import judge as _judge_report
from .keeper import (
    CheckResult,
    Keeper,
    ReadinessReport,
    parse_foreground_component,
    parse_foreground_package,
)
from .session import CollectSession
from .store import PerfStore

logger = logging.getLogger(__name__)

KEEPER_BORROW_PURPOSE = "mobile_perf.keeper"

DEFAULT_KEEP_RUNS = 200
DEFAULT_KEEP_DAYS = 30.0


class CollectError(RuntimeError):
    """采集闭环失败（启动、停止、pull 校验等任一环节）。"""


class TrendError(RuntimeError):
    """趋势查询失败（未知指标、无数据等）。"""


class ProfileMismatchError(TrendError):
    """跨设备档案的趋势查询未显式传参（Q6：口径不一致默认拒绝）。"""


# 趋势查询的单包 run 上限（与 store.fetch_sessions 的 LIMIT 对应）
TREND_MAX_RUNS = 1000


class CollectBackend(Protocol):
    """采集后端最小契约：kit 只依赖这两个方法，便于注入替身。"""

    def start(self, target: str, items: list[str]) -> str: ...

    def stop(self, session_id: str, output_dir: str) -> dict[str, Any]: ...


class HarnessBackend:
    """以 solo 适配层为后端的默认实现（ADR-0002：子进程调用 Harness）。"""

    def __init__(
        self,
        adb_path: str,
        serial: str,
        repo_root: str | Path | None = None,
    ):
        self.adb_path = adb_path
        self.serial = serial
        self._repo_root = repo_root

    def start(self, target: str, items: list[str]) -> str:
        result = solo.perf_start(
            self.adb_path, self.serial, target, items, repo_root=self._repo_root
        )
        if not result.success:
            raise CollectError(
                f"perf-start 失败: {result.data.get('error', result.stderr.strip() or '未知错误')}"
            )
        session_id = solo.perf_start_session_id(result.data)
        if session_id is None:
            raise CollectError("Harness 未返回 sessionId，无法停止采集")
        return session_id

    def stop(self, session_id: str, output_dir: str) -> dict[str, Any]:
        result = solo.perf_stop(
            self.adb_path, self.serial, session_id, output_dir, repo_root=self._repo_root
        )
        if not result.success:
            raise CollectError(
                f"perf-stop 失败: {result.data.get('error', result.stderr.strip() or '未知错误')}"
            )
        return result.data


class CollectScope:
    """交互式采集窗口（设计方案 §6.2）：with 块内宿主驱动 UI 并 mark 打标。

    __exit__ 无条件 finalize：宿主（如 pytest 用例）在块内失败也拿到完整
    报告，端侧录制不会悬挂；finalize 幂等，显式调用与退出清理共用一次收口。
    前台守卫（foreground_restore，默认开启，Q16 专项）：Harness perf-start
    经 SoloPi Scheme 控制面启动录制，可能改变前台——作用域记下 start 前的
    前台组件，start/stop 之后检测到漂移即还原（am start 组件或 monkey 启动），
    事件随 run 入库（kind=foreground）供判读与专项验收。
    """

    def __init__(
        self,
        kit: PerfKit,
        *,
        target: str,
        items: list[str],
        host_run_id: str | None,
        runs_dir: Path | None,
        supplement: bool,
        supplement_interval_s: float,
    ):
        self._kit = kit
        self.target = target
        self.items = list(items)
        self._host_run_id = host_run_id
        self._supplement = supplement
        self._supplement_interval_s = supplement_interval_s
        self._run_id = host_run_id or collector.new_run_id()
        runs_dir_path = Path(runs_dir) if runs_dir is not None else kit.runs_dir
        self._run_dir = collector.create_run_dir(runs_dir_path, self._run_id)
        self._artifacts_dir = self._run_dir / "artifacts"
        self._session: CollectSession | None = None
        self._poller: Any | None = None
        self._watcher: Any | None = None
        self._watch_warnings: list[dict[str, Any]] = []
        self.foreground_before: dict[str, Any] | None = None
        self.foreground_events: list[dict[str, Any]] = []
        self.report: dict[str, Any] | None = None
        self._finalized = False

    @property
    def run_id(self) -> str:
        return self._run_id

    def mark(self, label: str):
        if self._session is None:
            raise RuntimeError("cannot mark before collect scope is entered")
        return self._session.mark(label)

    def __enter__(self) -> CollectScope:
        kit = self._kit
        backend = kit.backend
        if kit.foreground_restore:
            self.foreground_before = kit._read_foreground_state()
        self._session = CollectSession(
            target=self.target,
            items=self.items,
            serial=kit.serial,
            start=lambda: kit._borrowed(
                "mobile_perf.start", lambda: backend.start(self.target, self.items)
            ),
            stop=lambda session_id, output_dir: kit._borrowed(
                "mobile_perf.stop_pull",
                lambda: backend.stop(session_id, output_dir),
            ),
            clock=kit._clock,
        )
        self._session.start()
        kit._supplement_offset = (0.0, "assumed")
        self._poller = kit._start_supplement(
            self._supplement, self._supplement_interval_s
        )
        self._watcher = kit._start_crash_watch(self.target, self._watch_warnings)
        kit._guard_foreground(self, phase="after_start")
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.finalize()
        return False  # 块内异常原样传播；报告已在 finalize 产出

    def finalize(self) -> dict[str, Any]:
        """停止采集并走归档/入库/报告全链路；幂等，返回本次 run 的报告。"""
        if self._finalized:
            return self.report
        self._finalized = True
        kit = self._kit
        supplement_result = None
        # 先停补采轮询再停端侧采集：补采窗口与 SoloPi 录制窗口对齐，
        # 也避免 stop/pull 借用设备期间继续旁路 dumpsys
        if self._poller is not None:
            supplement_result = self._poller.stop()
        try:
            record = self._session.stop(str(self._artifacts_dir))
        finally:
            # 监听是旁路进程：采集停止后立即回收，不悬挂
            watch_events = kit._stop_crash_watch(self._watcher)
        kit._guard_foreground(self, phase="after_stop")

        artifact_files = collector.validate_artifacts(self._artifacts_dir)

        # pull 完整性：设备端逐文件行数校验；通过才删设备端会话目录
        remote_dir = (
            record.stop_payload.get("outputPath")
            if isinstance(record.stop_payload, dict)
            else None
        )
        integrity_report = None
        device_removed = False
        if remote_dir:
            integrity_report, device_removed = collector.cleanup_after_pull(
                str(kit.adb_path),
                kit.serial,
                remote_dir,
                self._artifacts_dir,
                runner=kit._adb_runner,
            )
            device_row_check = "passed" if integrity_report.ok else "failed"
        else:
            device_row_check = "skipped"  # 载荷未带设备端路径（如注入替身后端）
        integrity = {
            "deviceRowCheck": device_row_check,
            "deviceDirRemoved": device_removed,
        }
        if integrity_report is not None and not integrity_report.ok:
            integrity["mismatches"] = integrity_report.mismatches
            if integrity_report.probe_error:
                integrity["probeError"] = integrity_report.probe_error

        # 聚合只吃本地已 pull 的数据；原始 CSV 永不入库
        aggregates = collector.aggregate_artifacts(self._artifacts_dir)

        # 补采合并：样本落 artifacts（供报告重建 elapsed 时间轴），聚合并入本 run
        supplement_info: dict[str, Any] = {"enabled": bool(self._poller)}
        supplement_aggregates: list[dict[str, Any]] = []
        if supplement_result is not None:
            supplement_info.update(
                {
                    "intervalS": self._supplement_interval_s,
                    "rounds": supplement_result.rounds,
                    "errors": supplement_result.errors,
                    "degraded": supplement_result.degraded,
                    "clockOffsetS": kit._supplement_offset[0],
                    "clockOffsetSource": kit._supplement_offset[1],
                }
            )
            if supplement_result.samples:
                supplement_aggregates = supplement_module.aggregate_samples(
                    supplement_result.samples
                )
            if supplement_aggregates:
                csv_path = supplement_module.write_supplement_csv(
                    self._artifacts_dir / "supplement_thermal_gpu.csv",
                    supplement_result.samples,
                )
                supplement_info["csv"] = str(csv_path)

        merged_aggregates = aggregates + supplement_aggregates
        metrics_summary = report_module.summarize_aggregates(merged_aggregates)
        # 充电降级（A1/§6.2）：巡检结论含 Battery/Temperature 降级项时，
        # 对应指标标注 not_tested（附原因），其余指标照常
        readiness = kit.last_readiness
        if readiness is not None:
            metrics_module.mark_not_tested(
                metrics_summary, getattr(readiness, "degraded_items", ())
            )

        meta = {
            "runId": self._run_id,
            "target": self.target,
            "items": self.items,
            "serial": kit.serial,
            "deviceProfile": kit.device_profile,
            "hostRunId": self._host_run_id,
            "startedAt": record.started_at,
            "stoppedAt": record.stopped_at,
            "durationS": round(record.duration_s, 3),
            "soloSessionId": record.session_id,
            "marks": record.marks_as_list(),
            "status": "stopped",
            "integrity": integrity,
            "supplement": supplement_info,
        }
        collector.write_meta(self._run_dir, meta)

        report_data = report_module.build_report(
            meta=meta,
            record={
                "sessionId": record.session_id,
                "marks": record.marks_as_list(),
            },
            artifacts=[str(path) for path in artifact_files],
            metrics_summary=metrics_summary,
            integrity=integrity,
            series=report_module.series_from_artifacts(self._artifacts_dir),
            # events 先置空，事件入库后在落盘前回填（见下方）
            events=[],
        )

        store = kit.store
        store.record_session(
            {
                "run_id": self._run_id,
                "solo_session_id": record.session_id,
                "serial": kit.serial,
                "device_profile": kit.device_profile,
                "target": self.target,
                "items": self.items,
                "started_at": record.started_at,
                "stopped_at": record.stopped_at,
                "duration_s": record.duration_s,
                "host_run_id": self._host_run_id,
                "status": "stopped",
                "run_dir": str(self._run_dir),
            }
        )
        store.record_metrics(self._run_id, merged_aggregates)
        store.record_run_artifacts(self._run_id, [str(self._artifacts_dir), str(self._run_dir)])
        for degraded in supplement_info.get("degraded", []):
            store.record_event(
                self._run_id,
                kind="supplement_degraded",
                detail={
                    "key": degraded["key"],
                    "reason": degraded["reason"],
                    "rounds": supplement_info.get("rounds"),
                    "errors": supplement_info.get("errors"),
                },
            )
        # crash/ANR 事件入统一时间轴（ts 已由 logwatch 用 keeper 偏移换算）
        for event in watch_events:
            store.record_event(
                self._run_id, kind=event["kind"], detail=event["detail"], ts=event["ts"]
            )
        for warning in self._watch_warnings:
            store.record_event(self._run_id, kind="logwatch_warning", detail=dict(warning))
        # keeper 巡检事件入统一时间轴（A2）：clock_offset/accessibility_needs_attention/
        # tcpip_heal/foreground 等以现有 kind 关联本 run 入库，随报告返回
        readiness = kit.last_readiness
        if readiness is not None:
            for event in getattr(readiness, "events", ()) or ():
                store.record_event(
                    self._run_id,
                    kind=event["kind"],
                    detail=event.get("detail", {}),
                    ts=event.get("ts"),
                )
        # 前台守卫事件（Q16 专项证据）：start/stop 后的漂移与还原记录
        for event in self.foreground_events:
            store.record_event(self._run_id, kind="foreground", detail=dict(event))
        if integrity_report is not None and not integrity_report.ok:
            reason = (
                "row_count_probe_failed" if integrity_report.probe_error else "row_count_mismatch"
            )
            store.record_event(
                self._run_id,
                kind="integrity_warning",
                detail={
                    "reason": reason,
                    "deviceDir": remote_dir,
                    "deviceDirKept": not device_removed,
                    "mismatches": integrity_report.mismatches,
                    "probeError": integrity_report.probe_error,
                },
            )
            logger.warning(
                "run %s pull 完整性异常（%s），设备端目录保留: %s",
                self._run_id,
                reason,
                remote_dir,
            )

        # 本地数据保留（可配置）：仅清理本组件 run 目录，失败不阻断采集闭环
        try:
            collector.enforce_retention(
                kit.runs_dir,
                store,
                keep_runs=kit.keep_runs,
                keep_days=kit.keep_days,
            )
        except Exception:  # noqa: BLE001 - 清理失败只告警
            logger.exception("本地保留策略清理失败")

        # 报告最后落盘：events 已入库，随 run 数据返回供报告时间轴渲染
        report_data["events"] = store.fetch_events(self._run_id)
        report_module.write_report(self._run_dir, report_data)
        report_module.write_html(self._run_dir, report_data)
        self.report = report_data
        return report_data


class PerfKit:
    """组件门面：宿主与 CLI 的统一入口。

    adb/serial 未传时走默认解析；backend 未传时用 Harness 子进程后端。
    clock/sleep 可注入，用于无真实等待的测试。
    """

    def __init__(
        self,
        *,
        adb_path: str | None = None,
        serial: str | None = None,
        repo_root: str | Path | None = None,
        runs_dir: str | Path | None = None,
        backend: CollectBackend | None = None,
        store: PerfStore | None = None,
        db_path: str | Path | None = None,
        adb_runner: Any = None,
        keep_runs: int = DEFAULT_KEEP_RUNS,
        keep_days: float = DEFAULT_KEEP_DAYS,
        gate: DeviceGate | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        device_call: Callable[..., Any] | None = None,
        doctor_fn: Callable[[], Any] | None = None,
        dialog_probe: Callable[[], Any] | None = None,
        device_profile: str | None = None,
        logwatch_factory: Callable[..., Any] | None = None,
        foreground_restore: bool = True,
        dialog_dismiss: Callable[[], dict[str, Any]] | None = None,
    ):
        self.adb_path = resolve_adb(adb_path)
        if not serial:
            raise ValueError("必须提供设备 serial（显式参数或 CLI/env 解析）")
        self.serial = serial
        self._repo_root = Path(repo_root) if repo_root is not None else default_repo_root()
        self.runs_dir = Path(runs_dir) if runs_dir is not None else self._repo_root / "runs"
        self.db_path = db_path
        self._backend = backend
        self._store = store
        self._adb_runner = adb_runner
        self.keep_runs = keep_runs
        self.keep_days = keep_days
        self._gate = gate
        self._clock = clock
        self._sleep = sleep
        self._device_call = device_call
        self._doctor_fn = doctor_fn
        self._dialog_probe = dialog_probe
        self.device_profile = device_profile
        self._logwatch_factory = logwatch_factory
        self.foreground_restore = foreground_restore
        # 权限弹窗代点探针（A4，默认关）：dialog.make_dialog_dismiss_probe 构造，
        # 仅宿主显式传入才挂上 ensure_ready 巡检
        self._dialog_dismiss = dialog_dismiss
        self._keeper: Keeper | None = None
        self.last_readiness: ReadinessReport | None = None
        # 统一设备命令通道（B3）：_shell/对时兜底/补采轮询共用一条优先级链
        self._channel = self._build_device_channel()

    def _build_device_channel(self) -> Callable[..., Any]:
        """设备命令通道唯一优先级链：注入 runner > 注入 device_call > 默认。"""
        if self._adb_runner is not None:
            adb_path = str(self.adb_path)
            runner = self._adb_runner

            def via_runner(args: list[str], timeout_s: float = 15.0) -> Any:
                return runner([adb_path, *args], timeout_s)

            return via_runner
        if self._device_call is not None:
            return self._device_call
        return supplement_module.default_call_device(str(self.adb_path))

    @property
    def store(self) -> PerfStore:
        """组件的 SQLite 存储：注入优先，缺省 <repo>/data/mobile_perf.db。"""
        if self._store is None:
            self._store = PerfStore(self.db_path or self._repo_root / "data" / "mobile_perf.db")
        return self._store

    @property
    def backend(self) -> CollectBackend:
        if self._backend is None:
            self._backend = HarnessBackend(
                str(self.adb_path), self.serial, repo_root=self._repo_root
            )
        return self._backend

    @property
    def gate(self) -> DeviceGate:
        """设备准入：宿主可注入；缺省按 moblie-auto 锁文件协议对等参与。"""
        if self._gate is None:
            self._gate = FileLockGate()
        return self._gate

    def _borrowed(self, purpose: str, action: Callable[[], Any]) -> Any:
        """在短事务借用窗口内执行一个设备命令（ADR-0001：完成即还）。"""
        with self.gate.borrow(self.serial, purpose=purpose):
            return action()

    def _shell(self, args: list[str], timeout_s: float = 15.0) -> Any:
        """发起一次 adb shell 命令：走统一设备通道（runner > device_call > 默认）。"""
        return self._channel(args, timeout_s)

    def _read_foreground_state(self) -> dict[str, Any] | None:
        """短事务读取前台状态（包名 + 组件）；读不到或命令失败返回 None。"""
        try:
            result = self._borrowed(
                "mobile_perf.foreground",
                lambda: self._shell(
                    ["-s", self.serial, "shell", "dumpsys", "activity", "activities"]
                ),
            )
        except Exception:  # noqa: BLE001 - 读不到前台就放弃守卫，不阻断采集
            return None
        stdout = getattr(result, "stdout", "") or ""
        package = parse_foreground_package(stdout)
        if not package:
            return None
        return {"package": package, "component": parse_foreground_component(stdout)}

    def _restore_foreground(self, state: dict[str, Any]) -> str:
        """把前台还原为采集前状态；返回还原途径（component/monkey）。"""
        if state.get("component"):
            args = ["-s", self.serial, "shell", "am", "start", "-n", state["component"]]
            via = "component"
        else:
            args = [
                "-s",
                self.serial,
                "shell",
                "monkey",
                "-p",
                state["package"],
                "-c",
                "android.intent.category.LAUNCHER",
                "1",
            ]
            via = "monkey"
        self._borrowed("mobile_perf.foreground", lambda: self._shell(args))
        return via

    def _guard_foreground(self, scope: CollectScope, *, phase: str) -> None:
        """前台守卫：phase 时刻检测前台漂移，漂移即还原为采集前状态。"""
        scope.foreground_events.extend(
            self._guard_foreground_events(scope.foreground_before, phase=phase)
        )

    def _guard_foreground_events(
        self, before: dict[str, Any] | None, *, phase: str
    ) -> list[dict[str, Any]]:
        """守卫核心：返回本 phase 产生的前台事件明细（无漂移返回空）。

        守卫失败（读取/还原异常）只记事件，绝不阻断采集闭环（Q16：如实
        记录而非掩盖）。
        """
        if not self.foreground_restore or before is None:
            return []
        before_pkg = before["package"]
        try:
            state = self._read_foreground_state()
            observed = state["package"] if state else None
            if observed == before_pkg:
                return []
            via = self._restore_foreground(before)
            return [
                {
                    "phase": phase,
                    "before": before_pkg,
                    "observed": observed,
                    "restored": True,
                    "restoreVia": via,
                }
            ]
        except Exception as exc:  # noqa: BLE001 - 守卫失败不阻断采集
            logger.warning("前台守卫（%s）失败: %s", phase, exc)
            return [
                {
                    "phase": phase,
                    "before": before_pkg,
                    "observed": None,
                    "restored": False,
                    "error": str(exc),
                }
            ]

    def ensure_ready(self) -> ReadinessReport:
        """巡检自愈（幂等可重入）：doctor + tcpip 自愈 + 无障碍/充电/前台/时钟检查。

        所有巡检动作各为短事务借用（复用 _borrowed），完成即还、不持设备权。
        返回的 ReadinessReport 含 degraded_items（充电时 Battery/温度降级）与
        needs_attention（无障碍假开启），供编排层降级与告警；事件随报告返回，
        入库由持有 store 的编排层完成。
        """
        if self._keeper is None:
            from .keeper import default_call_device, default_dialog_probe, default_run_doctor

            raw_call = self._device_call or default_call_device(str(self.adb_path))
            raw_doctor = self._doctor_fn or default_run_doctor(
                str(self.adb_path), self.serial, repo_root=str(self._repo_root)
            )
            def borrowed_call(args, timeout_s=15.0):
                return self._borrowed(
                    KEEPER_BORROW_PURPOSE, lambda: raw_call(args, timeout_s)
                )
            self._keeper = Keeper(
                adb_path=str(self.adb_path),
                serial=self.serial,
                call_device=borrowed_call,
                run_doctor=lambda: self._borrowed(KEEPER_BORROW_PURPOSE, raw_doctor),
                # 未注入时默认接真实弹窗开关探针：forward 与其他巡检动作一样走
                # 短事务借用（ADR-0001），HTTP 读回在本机侧完成
                dialog_probe=self._dialog_probe or default_dialog_probe(
                    borrowed_call, self.serial
                ),
            )
        # 前台守卫（Q16）：doctor 子命令经 HarnessSession 启动 SoloPi 控制面，
        # 且本机 dumpsys window 报 mCurrentFocus=null 时 Harness 的
        # preserve_foreground 拿不到还原目标——巡检完成后由本组件还原前台，
        # 事件随报告返回（与 keeper 事件同形，入库由编排层完成）
        before = self._read_foreground_state() if self.foreground_restore else None
        report = self._keeper.run()
        if before is not None:
            events = self._guard_foreground_events(before, phase="after_ready")
            if events:
                report = dataclasses.replace(
                    report,
                    events=tuple(report.events)
                    + tuple(
                        {
                            "kind": "foreground",
                            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                            "detail": event,
                        }
                        for event in events
                    ),
                )
        report = self._run_dialog_dismiss(report)
        self.last_readiness = report
        self._record_device_status(report)
        return report

    def _run_dialog_dismiss(self, report: ReadinessReport) -> ReadinessReport:
        """权限弹窗代点探针（A4，默认关）：显式传入 dialog_dismiss 才挂上巡检。

        实际点按（或探针异常）才记 dialog_dismiss 事件供判读；失败不阻断巡检。
        """
        if self._dialog_dismiss is None:
            return report
        try:
            verdict = self._dialog_dismiss()
        except Exception as exc:  # noqa: BLE001 - 降级备选失败只记事件
            logger.warning("dialog_dismiss 探针异常: %s", exc)
            verdict = {"enabled": True, "present": None, "tapped": False, "error": str(exc)}
        event = {
            "kind": "dialog_dismiss",
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "detail": verdict,
        }
        return dataclasses.replace(
            report,
            checks=tuple(report.checks) + (CheckResult("dialog_dismiss", "ok", verdict),),
            events=tuple(report.events) + (event,),
        )

    def _record_device_status(self, report: ReadinessReport) -> None:
        """巡检结论落到 devices 表（§5）：needs_attention 如实入库，不粉饰。"""
        if report.needs_attention:
            status = "needs_attention"
        elif report.ok:
            status = "online"
        else:
            status = "offline"
        self.store.upsert_device(
            self.serial,
            status=status,
            device_profile=self.device_profile,
            health=report.as_dict(),
        )

    def trend(self, pkg: str, metric: str, profile: str | None = None) -> dict[str, Any]:
        """跨 run 的指标趋势对比（Q6：跨设备档案默认拒绝）。

        返回按时间升序的每 run 聚合对比（median/min/mean/max/p90/sampleCount）。
        profile 未传且目标包的 run 跨多个设备档案时拒绝（ProfileMismatchError）；
        显式传 profile 覆盖：只保留该档案的 run，其余被排除时标记
        profileMismatch=True（口径不一致）。device_profile 未记录的 run 归为
        None 档案参与判断。
        """
        spec = metrics_module.METRICS.get(metric)
        if spec is None:
            valid = ", ".join(sorted(metrics_module.METRICS))
            raise TrendError(f"未知指标 {metric!r}，可选 canonical key: {valid}")

        sessions = self.store.fetch_sessions(target=pkg, limit=TREND_MAX_RUNS)
        if not sessions:
            raise TrendError(f"包 {pkg} 没有已入库的 run，无法计算趋势")

        profiles = {session.get("device_profile") for session in sessions}
        profile_mismatch = False
        if profile is None:
            if len(profiles) > 1:
                shown = ", ".join(sorted(str(item) for item in profiles))
                raise ProfileMismatchError(
                    f"包 {pkg} 的 run 跨多个设备档案（{shown}），口径不一致；"
                    "请显式传 profile= 指定档案后重试"
                )
            chosen = sessions
        else:
            chosen = [s for s in sessions if s.get("device_profile") == profile]
            if not chosen:
                raise TrendError(
                    f"包 {pkg} 没有 device_profile={profile!r} 的 run；"
                    f"已有档案: {', '.join(sorted(str(item) for item in profiles)) or '未记录'}"
                )
            profile_mismatch = profiles != {profile}

        runs: list[dict[str, Any]] = []
        for session in reversed(chosen):  # started_at DESC 入库 → 反转为升序
            for row in self.store.fetch_metrics(session["run_id"]):
                if row["metric_key"] != metric:
                    continue
                runs.append(
                    {
                        "runId": session["run_id"],
                        "startedAt": session.get("started_at"),
                        "durationS": session.get("duration_s"),
                        "serial": session.get("serial"),
                        "deviceProfile": session.get("device_profile"),
                        "min": row["min"],
                        "median": row["median"],
                        "mean": row["mean"],
                        "max": row["max"],
                        "p90": row["p90"],
                        "sampleCount": row["sample_count"],
                    }
                )
                break
        if not runs:
            raise TrendError(f"包 {pkg} 的 run 中没有指标 {metric} 的聚合数据")

        return {
            "pkg": pkg,
            "metric": metric,
            "unit": spec.unit,
            "labelZh": spec.label_zh,
            "labelEn": spec.label_en,
            "profile": profile if profile is not None else chosen[0].get("device_profile"),
            "profileMismatch": profile_mismatch,
            "runs": runs,
        }

    def _measure_clock_offset_s(self) -> float | None:
        """无巡检结论时短事务实测 device-host 时钟偏移（对时兜底，keeper 同口径）。"""
        try:
            result = self._borrowed(
                "mobile_perf.clock",
                lambda: self._channel(
                    ["-s", self.serial, "shell", "date", "+%s"], 10.0
                ),
            )
            device_epoch = supplement_module.parse_device_epoch(result.stdout or "")
        except Exception:  # noqa: BLE001 - 测不到偏移就退回假设已同步
            return None
        return time.time() - device_epoch if device_epoch is not None else None

    def _start_supplement(
        self, enabled: bool, interval_s: float
    ) -> supplement_module.SupplementPoller | None:
        """按巡检结论构造并启动补采轮询器（紧随 session.start，起点即会话起点）。

        对时基准：巡检 clock_offset_s 优先；无巡检结论则短事务实测；实测失败
        退回 0（假设已同步，偏差如实留在 meta 供判读）。充电降级
        （degraded_items 含 Temperature）时温度补采跳过。轮询器不持 gate 设备权。
        """
        if not enabled:
            return None
        readiness = self.last_readiness
        keeper_offset = (
            readiness.clock_offset_s
            if readiness is not None and readiness.clock_offset_s is not None
            else None
        )
        charging = bool(readiness is not None and "Temperature" in readiness.degraded_items)
        if keeper_offset is not None:
            offset, offset_source = keeper_offset, "keeper"
        else:
            measured = self._measure_clock_offset_s()
            offset = measured if measured is not None else 0.0
            offset_source = "measured" if measured is not None else "assumed"
        poller = supplement_module.SupplementPoller(
            adb_path=str(self.adb_path),
            serial=self.serial,
            interval_s=interval_s,
            clock_offset_s=offset,
            collect_temperature=not charging,
            call_device=self._channel,
        )
        self._supplement_offset = (offset, offset_source)
        poller.start()
        return poller
    def save_baseline(
        self,
        report: dict[str, Any],
        name: str,
        metric_keys: list[str] | None = None,
    ) -> int:
        """把一个 run 的报告存为命名基线：每指标取 median 入 baselines 表。

        基线绑定报告的 target 与 device_profile（可比性单位）；thresholds 不入库。
        metric_keys 未传时保存报告里全部指标。返回写入行数。
        """
        metrics_summary = report.get("metricsSummary")
        if not metrics_summary:
            raise ValueError("报告缺少 metricsSummary 聚合数据，无法保存基线")
        rows = 0
        for metric_key, entry in sorted(metrics_summary.items()):
            if metric_keys is not None and metric_key not in metric_keys:
                continue
            self.store.record_baseline(
                name=name,
                target_pkg=report.get("target"),
                device_profile=report.get("deviceProfile"),
                metric_key=metric_key,
                value=entry.get("median"),
                source_run_id=report.get("runId"),
            )
            rows += 1
        return rows

    def judge(self, report: dict[str, Any], spec: JudgeSpec) -> Verdict:
        """门禁判定门面：从 store 解析基线（值与绑定档案）后交给纯函数 judge。

        基线不存在或无数据时以空解析结果传入，纯函数按前置不满足判 not_tested；
        宿主持有的 JudgeSpec 不被修改（解析结果经 resolved 副本注入）。
        """
        rows = self.store.fetch_baseline(spec.baseline)
        profile = next(
            (row.get("device_profile") for row in rows if row.get("device_profile")), None
        )
        data = {
            row["metric_key"]: {
                "value": row.get("value"),
                "sourceRunId": row.get("source_run_id"),
            }
            for row in rows
        }
        return _judge_report(report, spec.resolved(profile=profile, data=data))

    def collect(
        self,
        *,
        target: str,
        items: list[str],
        host_run_id: str | None = None,
        runs_dir: str | Path | None = None,
        supplement: bool = True,
        supplement_interval_s: float = supplement_module.DEFAULT_INTERVAL_S,
    ) -> CollectScope:
        """打开交互式采集窗口（设计方案 §6.2）：with 块内 UI 驱动 + mark。

        退出 with 块即 finalize（停止 → pull → 校验 → 归档 → 入库 → 报告）；
        报告同时在 scope.report 上留存。host_run_id 未传时自动生成。
        """
        return CollectScope(
            self,
            target=target,
            items=items,
            host_run_id=host_run_id,
            runs_dir=runs_dir,
            supplement=supplement,
            supplement_interval_s=supplement_interval_s,
        )

    def run_collect(
        self,
        *,
        target: str,
        items: list[str],
        duration_s: float,
        host_run_id: str | None = None,
        runs_dir: str | Path | None = None,
        supplement: bool = True,
        supplement_interval_s: float = supplement_module.DEFAULT_INTERVAL_S,
    ) -> dict[str, Any]:
        """一次完整采集闭环：巡检 → 启动 → 计时 → 停止 → pull → 校验 → 落盘 → 入库。

        collect() 的定时收口特例：窗口内 sleep duration_s，由作用域退出统一
        收口。开始前先 ensure_ready（设计方案 §6.2 流水线）：keeper 事件随本
        run 入库，充电降级结论供 metricsSummary 标注与判定使用。
        supplement=True 时（默认）采集期间由 SupplementPoller 旁路补采
        温度/GPU（不持 gate 设备权），stop 后合并入库（source=pc_supplement）
        并进报告 series；充电降级（巡检 degraded_items 含 Temperature）时
        温度补采跳过。
        """
        if duration_s <= 0:
            raise ValueError("duration_s 必须为正数")
        self.ensure_ready()
        with self.collect(
            target=target,
            items=items,
            host_run_id=host_run_id,
            runs_dir=runs_dir,
            supplement=supplement,
            supplement_interval_s=supplement_interval_s,
        ) as scope:
            self._sleep(duration_s)
        return scope.finalize()

    def _start_crash_watch(self, target: str, warnings: list[dict[str, Any]]) -> Any | None:
        """采集期间启动 crash/ANR 旁路监听；启动失败只告警，不阻断采集闭环。"""
        offset = self.last_readiness.clock_offset_s if self.last_readiness else None
        factory = self._logwatch_factory
        if factory is None:
            from .logwatch import LogWatch

            def factory(*, packages: list[str], clock_offset_s: float | None) -> Any:
                return LogWatch(
                    adb_path=str(self.adb_path),
                    serial=self.serial,
                    packages=packages,
                    clock_offset_s=clock_offset_s,
                )

        try:
            watcher = factory(packages=[target], clock_offset_s=offset)
            watcher.start()
            return watcher
        except Exception as exc:  # noqa: BLE001 - 监听缺失如实记录为事件
            logger.warning("logwatch 启动失败，crash/ANR 监听缺失: %s", exc)
            warnings.append({"reason": str(exc)})
            return None

    def _stop_crash_watch(self, watcher: Any | None) -> list[dict[str, Any]]:
        if watcher is None:
            return []
        try:
            return list(watcher.stop())
        except Exception:  # noqa: BLE001 - 停止失败不阻断采集闭环
            logger.exception("logwatch 停止异常")
            return []
