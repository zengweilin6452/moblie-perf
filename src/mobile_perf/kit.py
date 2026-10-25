"""门面：组装 adb 解析、Harness 适配层、采集会话与归档，提供 run_collect 编排。"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from . import collector, solo
from . import metrics as metrics_module
from . import report as report_module
from . import supplement as supplement_module
from .adb import default_repo_root, resolve_adb
from .gate import DeviceGate, FileLockGate
from .keeper import Keeper, ReadinessReport
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
        self._keeper: Keeper | None = None
        self.last_readiness: ReadinessReport | None = None

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

    def ensure_ready(self) -> ReadinessReport:
        """巡检自愈（幂等可重入）：doctor + tcpip 自愈 + 无障碍/充电/前台/时钟检查。

        所有巡检动作各为短事务借用（复用 _borrowed），完成即还、不持设备权。
        返回的 ReadinessReport 含 degraded_items（充电时 Battery/温度降级）与
        needs_attention（无障碍假开启），供编排层降级与告警；事件随报告返回，
        入库由持有 store 的编排层完成。
        """
        if self._keeper is None:
            from .keeper import default_call_device, default_run_doctor

            raw_call = self._device_call or default_call_device(str(self.adb_path))
            raw_doctor = self._doctor_fn or default_run_doctor(
                str(self.adb_path), self.serial, repo_root=str(self._repo_root)
            )
            self._keeper = Keeper(
                adb_path=str(self.adb_path),
                serial=self.serial,
                call_device=lambda args, timeout_s=15.0: self._borrowed(
                    KEEPER_BORROW_PURPOSE, lambda: raw_call(args, timeout_s)
                ),
                run_doctor=lambda: self._borrowed(KEEPER_BORROW_PURPOSE, raw_doctor),
                dialog_probe=self._dialog_probe,
            )
        report = self._keeper.run()
        self.last_readiness = report
        return report

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

    def _start_supplement(
        self, enabled: bool, interval_s: float
    ) -> supplement_module.SupplementPoller | None:
        """按巡检结论构造并启动补采轮询器（紧随 session.start，起点即会话起点）。

        巡检 clock_offset_s 为统一时间轴换算基准（未巡检按 0）；充电降级
        （degraded_items 含 Temperature）时温度补采跳过。轮询器不持 gate 设备权。
        """
        if not enabled:
            return None
        readiness = self.last_readiness
        offset = readiness.clock_offset_s if readiness is not None else None
        charging = bool(readiness is not None and "Temperature" in readiness.degraded_items)
        call = None
        if self._adb_runner is not None:
            adb_path = str(self.adb_path)

            def call(args: list[str], timeout_s: float = 10.0) -> Any:
                return self._adb_runner([adb_path, *args], timeout_s)

        poller = supplement_module.SupplementPoller(
            adb_path=str(self.adb_path),
            serial=self.serial,
            interval_s=interval_s,
            clock_offset_s=offset if offset is not None else 0.0,
            collect_temperature=not charging,
            call_device=call,
        )
        poller.start()
        return poller

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
        """一次完整采集闭环：启动 → 计时 → 停止 → pull → 校验 → 落盘 → 入库。

        supplement=True 时（默认）采集期间由 SupplementPoller 旁路补采温度/GPU
        （不持 gate 设备权），stop 后合并入库（source=pc_supplement）并进报告
        series；充电降级（巡检 degraded_items 含 Temperature）时温度补采跳过。
        """
        if duration_s <= 0:
            raise ValueError("duration_s 必须为正数")
        run_id = host_run_id or collector.new_run_id()
        runs_dir_path = Path(runs_dir) if runs_dir is not None else self.runs_dir
        run_dir = collector.create_run_dir(runs_dir_path, run_id)
        artifacts_dir = run_dir / "artifacts"

        backend = self.backend
        session = CollectSession(
            target=target,
            items=items,
            serial=self.serial,
            start=lambda: self._borrowed(
                "mobile_perf.start", lambda: backend.start(target, items)
            ),
            stop=lambda session_id, output_dir: self._borrowed(
                "mobile_perf.stop_pull",
                lambda: backend.stop(session_id, output_dir),
            ),
            clock=self._clock,
        )
        session.start()
        poller = self._start_supplement(supplement, supplement_interval_s)
        supplement_result = None
        try:
            self._sleep(duration_s)
        finally:
            try:
                # 中断也尽力停止端侧采集，避免留下无人停掉的录制会话
                record = session.stop(str(artifacts_dir))
            finally:
                if poller is not None:
                    supplement_result = poller.stop()

        artifact_files = collector.validate_artifacts(artifacts_dir)

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
                str(self.adb_path),
                self.serial,
                remote_dir,
                artifacts_dir,
                runner=self._adb_runner,
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
        aggregates = collector.aggregate_artifacts(artifacts_dir)

        # 补采合并：样本落 artifacts（供报告重建 elapsed 时间轴），聚合并入本 run
        supplement_info: dict[str, Any] = {"enabled": bool(poller)}
        supplement_aggregates: list[dict[str, Any]] = []
        if supplement_result is not None:
            supplement_info.update(
                {
                    "intervalS": supplement_interval_s,
                    "rounds": supplement_result.rounds,
                    "errors": supplement_result.errors,
                    "degraded": supplement_result.degraded,
                    "clockOffsetS": poller.clock_offset_s if poller else None,
                }
            )
            if supplement_result.samples:
                supplement_aggregates = supplement_module.aggregate_samples(
                    supplement_result.samples
                )
            if supplement_aggregates:
                csv_path = supplement_module.write_supplement_csv(
                    artifacts_dir / "supplement_thermal_gpu.csv", supplement_result.samples
                )
                supplement_info["csv"] = str(csv_path)

        merged_aggregates = aggregates + supplement_aggregates
        metrics_summary = report_module.summarize_aggregates(merged_aggregates)

        meta = {
            "runId": run_id,
            "target": target,
            "items": list(items),
            "serial": self.serial,
            "deviceProfile": self.device_profile,
            "hostRunId": host_run_id,
            "startedAt": record.started_at,
            "stoppedAt": record.stopped_at,
            "durationS": round(record.duration_s, 3),
            "soloSessionId": record.session_id,
            "marks": record.marks_as_list(),
            "status": "stopped",
            "integrity": integrity,
            "supplement": supplement_info,
        }
        collector.write_meta(run_dir, meta)

        report_data = report_module.build_report(
            meta=meta,
            record={
                "sessionId": record.session_id,
                "marks": record.marks_as_list(),
            },
            artifacts=[str(path) for path in artifact_files],
            metrics_summary=metrics_summary,
            integrity=integrity,
            series=report_module.series_from_artifacts(artifacts_dir),
            # 事件时间轴归 07 号工单接入；此处保留空注入点
            events=[],
        )
        report_module.write_report(run_dir, report_data)
        report_module.write_html(run_dir, report_data)

        store = self.store
        store.record_session(
            {
                "run_id": run_id,
                "solo_session_id": record.session_id,
                "serial": self.serial,
                "device_profile": self.device_profile,
                "target": target,
                "items": list(items),
                "started_at": record.started_at,
                "stopped_at": record.stopped_at,
                "duration_s": record.duration_s,
                "host_run_id": host_run_id,
                "status": "stopped",
                "run_dir": str(run_dir),
            }
        )
        store.record_metrics(run_id, merged_aggregates)
        store.record_run_artifacts(run_id, [str(artifacts_dir), str(run_dir)])
        for degraded in supplement_info.get("degraded", []):
            store.record_event(
                run_id,
                kind="supplement_degraded",
                detail={
                    "key": degraded["key"],
                    "reason": degraded["reason"],
                    "rounds": supplement_info.get("rounds"),
                    "errors": supplement_info.get("errors"),
                },
            )
        if integrity_report is not None and not integrity_report.ok:
            reason = (
                "row_count_probe_failed" if integrity_report.probe_error else "row_count_mismatch"
            )
            store.record_event(
                run_id,
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
                run_id,
                reason,
                remote_dir,
            )

        # 本地数据保留（可配置）：仅清理本组件 run 目录，失败不阻断采集闭环
        try:
            collector.enforce_retention(
                self.runs_dir,
                store,
                keep_runs=self.keep_runs,
                keep_days=self.keep_days,
            )
        except Exception:  # noqa: BLE001 - 清理失败只告警
            logger.exception("本地保留策略清理失败")
        return report_data
