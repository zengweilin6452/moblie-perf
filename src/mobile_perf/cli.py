"""命令行：mobile-perf doctor / collect / report / trend / leak。

collect 一条命令完成：启动采集 → duration 计时 → 停止 → pull → runs/<runId>/ 落盘。
report 从 runs/ 与 store 组装 HTML+JSON 双产物；trend 输出跨 run 指标对比；
leak 输出内存泄漏趋势（median 随墙钟时间的 OLS 斜率 + 置信度 + 可选宿主阈值结论）。
进度与错误走 stderr，最终报告 JSON 走 stdout（机器可读契约）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .adb import default_repo_root, first_online_device, resolve_adb
from .gate import GateBusyError
from .judge import JudgeSpec
from .kit import CollectError, PerfKit, ProfileMismatchError, TrendError
from .report import assemble_run_report, write_html, write_report
from .store import PerfStore

DEFAULT_ITEMS = "CPU,Memory"  # 不含 FPS：FPS 每次启动弹告知框，无人值守不可用


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mobile-perf", description="App 性能采集组件（基于 SoloPi 端侧采集）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    doctor_parser = sub.add_parser(
        "doctor", help="巡检+自愈：doctor/tcpip 自愈/无障碍/充电/前台/时钟"
    )
    doctor_parser.add_argument("--serial", help="设备 serial")
    doctor_parser.add_argument("--adb", help="adb 可执行文件路径（默认走三级解析）")

    collect_parser = sub.add_parser(
        "collect", help="启动采集 → 计时 → 停止 → pull → runs/<runId>/ 落盘"
    )
    collect_parser.add_argument("pkg", help="被测应用包名")
    collect_parser.add_argument(
        "--items",
        default=DEFAULT_ITEMS,
        help=f"逗号分隔采集项（默认 {DEFAULT_ITEMS}；含 FPS 需人工点告知弹窗）",
    )
    collect_parser.add_argument("--duration", type=float, default=180.0, help="采集时长秒数")
    collect_parser.add_argument(
        "--serial", help="设备 serial（默认 env MOBILE_PERF_SERIAL > 唯一在线设备）"
    )
    collect_parser.add_argument("--adb", help="adb 可执行文件路径（默认走三级解析）")
    collect_parser.add_argument("--runs-dir", help="run 产物根目录（默认 <repo>/runs）")
    collect_parser.add_argument("--run-id", help="宿主运行标识（默认自动生成）")
    collect_parser.add_argument(
        "--start-mode",
        choices=("cold", "warm"),
        help=(
            "启动编排（ticket 12）：cold=采集窗口内 force-stop 后拉起（冷启动）；"
            "warm=退后台后再拉起（温启动）。启动发生在 SoloPi 采集窗口内，"
            "启动期资源曲线覆盖启动时刻；不传则不编排启动"
        ),
    )
    collect_parser.add_argument(
        "--jank-threshold",
        help=(
            "卡顿分级阈值（ticket 14）：light:100,heavy:300,severe:700（ms，严格大于"
            "才计数）；不传用组件默认 100/300/700。阈值归宿主、不入库"
        ),
    )
    collect_parser.add_argument(
        "--device-profile",
        help="设备档案（机型+OS 大版本；趋势与基线的可比性单位，默认不记录）",
    )
    collect_parser.add_argument(
        "--frame-poll",
        action="store_true",
        help=(
            "逐帧采集（ticket 15）：采集窗口内轮询 dumpsys gfxinfo framestats，"
            "产出帧耗时 P50/P90/P99 与超时帧占比。与卡顿分级口径不同、不可互算"
        ),
    )
    collect_parser.add_argument(
        "--frame-interval",
        type=float,
        default=None,
        help="逐帧轮询间隔秒（默认 2.0；dumpsys 不触发渲染，间隔过小无收益）",
    )

    report_parser = sub.add_parser(
        "report", help="从 runs/ 与 store 组装 run 的 HTML+JSON 双产物"
    )
    report_parser.add_argument("run_id", help="run 标识")
    report_parser.add_argument("--runs-dir", help="run 产物根目录（默认 <repo>/runs）")
    report_parser.add_argument("--db", help="SQLite 路径（默认 <repo>/data/mobile_perf.db）")
    report_parser.add_argument(
        "--adb", help="adb 可执行文件路径（默认走三级解析；仅构造 judge 门面用，不连设备）"
    )
    report_parser.add_argument(
        "--baseline",
        help="基线名（提供时对 run 执行门禁判定，结论附进报告与 stdout）",
    )
    report_parser.add_argument(
        "--threshold",
        action="append",
        default=[],
        metavar="METRIC:OP:VALUE:LEVEL",
        help=(
            "门禁阈值（宿主显式传入、不入库），可重复；"
            "OP ∈ lt/le/gt/ge，LEVEL ∈ warn/fail，如 mem_pss_mb:lt:220:fail"
        ),
    )

    trend_parser = sub.add_parser("trend", help="跨 run 的指标趋势对比（Q6 跨档案默认拒绝）")
    trend_parser.add_argument("pkg", help="被测应用包名")
    trend_parser.add_argument("--metric", required=True, help="canonical key（如 mem_pss_mb）")
    trend_parser.add_argument(
        "--profile", help="设备档案（跨档案 run 存在时必须显式指定）"
    )
    trend_parser.add_argument("--serial", help="设备 serial（趋势只读库，不连设备）")
    trend_parser.add_argument("--adb", help="adb 可执行文件路径（默认走三级解析）")
    trend_parser.add_argument("--db", help="SQLite 路径（默认 <repo>/data/mobile_perf.db）")

    leak_parser = sub.add_parser(
        "leak", help="内存泄漏趋势（同档案历史 run 的 median 斜率，跨档案默认拒绝）"
    )
    leak_parser.add_argument("pkg", help="被测应用包名")
    leak_parser.add_argument(
        "--profile", help="设备档案（跨档案 run 存在时必须显式指定）"
    )
    leak_parser.add_argument(
        "--min-runs", type=int, default=None, help="回归所需 run 数下限（默认 3）"
    )
    leak_parser.add_argument(
        "--slope-threshold",
        type=float,
        help="宿主斜率阈值（MB/day，超过即不满足）；不传则只输出斜率与置信度",
    )
    leak_parser.add_argument(
        "--level",
        choices=("fail", "warn"),
        default="fail",
        help="阈值不满足时的级别（默认 fail）",
    )
    leak_parser.add_argument("--serial", help="设备 serial（泄漏趋势只读库，不连设备）")
    leak_parser.add_argument("--adb", help="adb 可执行文件路径（默认走三级解析）")
    leak_parser.add_argument("--db", help="SQLite 路径（默认 <repo>/data/mobile_perf.db）")

    sub.add_parser(
        "serve",
        help="HTTP 服务（设计方案 §6.6：仅预留接口，未实现）",
    )
    return parser


def _resolve_serial(explicit: str | None, adb_path: str) -> str | None:
    if explicit:
        return explicit
    env_serial = os.environ.get("MOBILE_PERF_SERIAL")
    if env_serial:
        return env_serial
    return first_online_device(adb_path)


def _build_kit(kit_factory, **kwargs):
    """kit 构造公共口：测试注入 kit_factory，否则走真实 PerfKit。"""
    if kit_factory is not None:
        return kit_factory(**kwargs)
    return PerfKit(**kwargs)


def _run_doctor(args: argparse.Namespace, kit_factory=None) -> int:
    adb_path = str(resolve_adb(args.adb))
    serial = _resolve_serial(args.serial, adb_path)
    if not serial:
        print("[mobile-perf] 未找到在线设备，请用 --serial 指定", file=sys.stderr)
        return 1
    kit = _build_kit(kit_factory, adb_path=adb_path, serial=serial)
    try:
        report = kit.ensure_ready()
    except GateBusyError as exc:
        print(f"[mobile-perf] 设备忙: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    return 0 if report.ok else 1


def _run_collect(args: argparse.Namespace, kit_factory) -> int:
    adb_path = str(resolve_adb(args.adb))
    serial = _resolve_serial(args.serial, adb_path)
    if not serial:
        print(
            "[mobile-perf] 无法确定设备 serial：无 --serial、无 MOBILE_PERF_SERIAL，"
            "且当前不是恰好一台设备在线",
            file=sys.stderr,
        )
        return 1

    items = [item.strip() for item in args.items.split(",") if item.strip()]
    if not items:
        print("[mobile-perf] --items 至少需要一个采集项", file=sys.stderr)
        return 1

    try:
        jank_thresholds = _parse_jank_thresholds(args.jank_threshold)
    except ValueError as exc:
        print(f"[mobile-perf] collect 参数错误: {exc}", file=sys.stderr)
        return 1

    kit = _build_kit(
        kit_factory,
        adb_path=adb_path,
        serial=serial,
        runs_dir=args.runs_dir,
        device_profile=args.device_profile,
    )

    try:
        report = kit.run_collect(
            target=args.pkg,
            items=items,
            duration_s=args.duration,
            host_run_id=args.run_id,
            runs_dir=args.runs_dir,
            start_mode=args.start_mode,
            jank_thresholds_ms=jank_thresholds,
            frame_poll=args.frame_poll,
            **(
                {"frame_interval_s": args.frame_interval}
                if args.frame_interval is not None
                else {}
            ),
        )
    except CollectError as exc:
        print(f"[mobile-perf] collect 失败: {exc}", file=sys.stderr)
        return 1
    except GateBusyError as exc:
        print(f"[mobile-perf] 设备忙: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"[mobile-perf] collect 参数错误: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _parse_jank_thresholds(raw: str | None) -> dict[str, float] | None:
    """解析 ``light:100,heavy:300,severe:700`` 形式的分级阈值（宿主显式传入）。

    未传返回 None（组件默认口径）；格式错误或阈值非法抛 ValueError。
    """
    if raw is None:
        return None
    from . import jank as jank_module

    thresholds: dict[str, float] = {}
    for item in raw.split(","):
        grade, _, value = item.partition(":")
        if not grade or not value:
            raise ValueError(f"分级阈值 {item!r} 格式应为 <grade>:<ms>")
        try:
            thresholds[grade.strip()] = float(value)
        except ValueError:
            raise ValueError(f"分级阈值 {item!r} 的毫秒数不是数字") from None
    try:
        return jank_module.validate_thresholds(thresholds)
    except ValueError as exc:
        raise ValueError(str(exc)) from None


def _parse_thresholds(specs: list[str]) -> dict[str, tuple[str, float, str]]:
    """解析 METRIC:OP:VALUE:LEVEL 形式的 CLI 阈值参数（宿主显式传入）。"""
    thresholds: dict[str, tuple[str, float, str]] = {}
    for raw in specs:
        parts = raw.split(":")
        if len(parts) != 4:
            raise ValueError(f"阈值 {raw!r} 格式应为 METRIC:OP:VALUE:LEVEL")
        metric, op, value, level = parts
        try:
            thresholds[metric] = (op, float(value), level)
        except ValueError:
            raise ValueError(f"阈值 {raw!r} 的 VALUE 不是数字") from None
    return thresholds


def _run_report(args: argparse.Namespace, kit_factory=None) -> int:
    repo_root = default_repo_root()
    db_path = args.db or repo_root / "data" / "mobile_perf.db"
    runs_dir = Path(args.runs_dir or repo_root / "runs")
    store = PerfStore(db_path)
    sessions = store.fetch_sessions(limit=10000)
    session = next((s for s in sessions if s.get("run_id") == args.run_id), None)
    if session is None:
        print(
            f"[mobile-perf] report 失败: store 中没有 run_id={args.run_id} 的会话记录",
            file=sys.stderr,
        )
        return 1
    run_dir = session.get("run_dir") or str(runs_dir / args.run_id)
    report_data = assemble_run_report(args.run_id, runs_dir=runs_dir, store=store)

    verdict_payload = None
    if args.baseline:
        try:
            thresholds = _parse_thresholds(args.threshold)
        except ValueError as exc:
            print(f"[mobile-perf] report 失败: --threshold 参数错误: {exc}", file=sys.stderr)
            return 1
        adb_path = str(resolve_adb(getattr(args, "adb", None)))
        # 只读判定（B4）：serial 取 run 记录即可，未记录时以 None 构造（不触设备）
        kit = _build_kit(
            kit_factory, adb_path=adb_path, db_path=args.db, serial=session.get("serial")
        )
        try:
            spec = JudgeSpec(baseline=args.baseline, thresholds=thresholds)
        except ValueError as exc:
            print(f"[mobile-perf] report 失败: --threshold 参数错误: {exc}", file=sys.stderr)
            return 1
        verdict = kit.judge(report_data, spec)
        verdict_payload = verdict.to_dict()
        report_data["verdict"] = verdict_payload

    json_path = write_report(run_dir, report_data)
    html_path = write_html(run_dir, report_data)
    print(
        json.dumps(
            {
                "runId": args.run_id,
                "runDir": run_dir,
                "reportJson": str(json_path),
                "reportHtml": str(html_path),
                **({"verdict": verdict_payload} if verdict_payload is not None else {}),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _run_trend(args: argparse.Namespace, kit_factory=None) -> int:
    adb_path = str(resolve_adb(args.adb))
    # 只读趋势（B4）：不强制 serial——不探测在线设备，显式传入才注入
    kwargs: dict = {"adb_path": adb_path, "db_path": args.db}
    if args.serial:
        kwargs["serial"] = args.serial
    kit = _build_kit(kit_factory, **kwargs)
    try:
        trend = kit.trend(args.pkg, args.metric, profile=args.profile)
    except ProfileMismatchError as exc:
        print(f"[mobile-perf] trend 口径不一致: {exc}", file=sys.stderr)
        return 1
    except TrendError as exc:
        print(f"[mobile-perf] trend 失败: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(trend, ensure_ascii=False, indent=2))
    return 0


def _run_leak(args: argparse.Namespace, kit_factory=None) -> int:
    adb_path = str(resolve_adb(args.adb))
    # 只读分析（同 trend）：不强制 serial——不探测在线设备，显式传入才注入
    kwargs: dict = {"adb_path": adb_path, "db_path": args.db}
    if args.serial:
        kwargs["serial"] = args.serial
    kit = _build_kit(kit_factory, **kwargs)
    extra: dict = {}
    if args.min_runs is not None:
        extra["min_runs"] = args.min_runs
    if args.slope_threshold is not None:
        extra["slope_threshold"] = args.slope_threshold
        extra["level"] = args.level
    try:
        leak_trend = kit.leak_trend(args.pkg, profile=args.profile, **extra)
    except ProfileMismatchError as exc:
        print(f"[mobile-perf] leak 口径不一致: {exc}", file=sys.stderr)
        return 1
    except TrendError as exc:
        print(f"[mobile-perf] leak 失败: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(leak_trend, ensure_ascii=False, indent=2))
    return 0


def _run_serve(args: argparse.Namespace, kit_factory=None) -> int:
    """§6.6：serve HTTP 模式仅预留接口不实现（回环 + gate 门控，形态对齐 viewer）。"""
    print("[mobile-perf] serve 为预留接口，未实现（设计方案 §6.6）", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None, *, kit_factory=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        return _run_doctor(args, kit_factory)
    if args.command == "collect":
        return _run_collect(args, kit_factory)
    if args.command == "report":
        return _run_report(args, kit_factory)
    if args.command == "trend":
        return _run_trend(args, kit_factory)
    if args.command == "leak":
        return _run_leak(args, kit_factory)
    if args.command == "serve":
        return _run_serve(args, kit_factory)
    build_parser().error(f"未知命令: {args.command}")
    return 2
