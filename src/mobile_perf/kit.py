"""门面：组装 adb 解析、Harness 适配层、采集会话与归档，提供 run_collect 编排。"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from . import collector, solo
from . import report as report_module
from .adb import default_repo_root, resolve_adb
from .gate import DeviceGate, FileLockGate
from .session import CollectSession
from .store import PerfStore

logger = logging.getLogger(__name__)

DEFAULT_KEEP_RUNS = 200
DEFAULT_KEEP_DAYS = 30.0


class CollectError(RuntimeError):
    """采集闭环失败（启动、停止、pull 校验等任一环节）。"""


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
        adb_runner: Any = None,
        keep_runs: int = DEFAULT_KEEP_RUNS,
        keep_days: float = DEFAULT_KEEP_DAYS,
        gate: DeviceGate | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.adb_path = resolve_adb(adb_path)
        if not serial:
            raise ValueError("必须提供设备 serial（显式参数或 CLI/env 解析）")
        self.serial = serial
        self._repo_root = Path(repo_root) if repo_root is not None else default_repo_root()
        self.runs_dir = Path(runs_dir) if runs_dir is not None else self._repo_root / "runs"
        self._backend = backend
        self._store = store
        self._adb_runner = adb_runner
        self.keep_runs = keep_runs
        self.keep_days = keep_days
        self._gate = gate
        self._clock = clock
        self._sleep = sleep

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

    def run_collect(
        self,
        *,
        target: str,
        items: list[str],
        duration_s: float,
        host_run_id: str | None = None,
        runs_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        """一次完整采集闭环：启动 → 计时 → 停止 → pull → 校验 → 落盘 → 入库。"""
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
        try:
            self._sleep(duration_s)
        finally:
            # 中断也尽力停止端侧采集，避免留下无人停掉的录制会话
            record = session.stop(str(artifacts_dir))

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
        metrics_summary = {
            agg["metric_key"]: {
                "unit": agg["unit"],
                "source": agg["source"],
                "min": agg["min"],
                "median": agg["median"],
                "mean": agg["mean"],
                "max": agg["max"],
                "p90": agg["p90"],
                "sampleCount": agg["sample_count"],
            }
            for agg in aggregates
        }

        meta = {
            "runId": run_id,
            "target": target,
            "items": list(items),
            "serial": self.serial,
            "hostRunId": host_run_id,
            "startedAt": record.started_at,
            "stoppedAt": record.stopped_at,
            "durationS": round(record.duration_s, 3),
            "soloSessionId": record.session_id,
            "marks": record.marks_as_list(),
            "status": "stopped",
            "integrity": integrity,
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
        )
        report_module.write_report(run_dir, report_data)

        store = self._store or PerfStore(self._repo_root / "data" / "mobile_perf.db")
        store.record_session(
            {
                "run_id": run_id,
                "solo_session_id": record.session_id,
                "serial": self.serial,
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
        store.record_metrics(run_id, aggregates)
        store.record_run_artifacts(run_id, [str(artifacts_dir), str(run_dir)])
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
