"""命令行：mobile-perf doctor / collect（report / trend 由后续工单补充）。

collect 一条命令完成：启动采集 → duration 计时 → 停止 → pull → runs/<runId>/ 落盘。
进度与错误走 stderr，最终报告 JSON 走 stdout（机器可读契约）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .adb import first_online_device, resolve_adb
from .gate import GateBusyError
from .kit import CollectError, PerfKit

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
    return parser


def _resolve_serial(explicit: str | None, adb_path: str) -> str | None:
    if explicit:
        return explicit
    env_serial = os.environ.get("MOBILE_PERF_SERIAL")
    if env_serial:
        return env_serial
    return first_online_device(adb_path)


def _run_doctor(args: argparse.Namespace, kit_factory=None) -> int:
    adb_path = str(resolve_adb(args.adb))
    serial = _resolve_serial(args.serial, adb_path)
    if not serial:
        print("[mobile-perf] 未找到在线设备，请用 --serial 指定", file=sys.stderr)
        return 1
    if kit_factory is not None:
        kit = kit_factory(adb_path=adb_path, serial=serial)
    else:
        kit = PerfKit(adb_path=adb_path, serial=serial)
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

    if kit_factory is not None:
        kit = kit_factory(adb_path=adb_path, serial=serial, runs_dir=args.runs_dir)
    else:
        kit = PerfKit(adb_path=adb_path, serial=serial, runs_dir=args.runs_dir)

    try:
        report = kit.run_collect(
            target=args.pkg,
            items=items,
            duration_s=args.duration,
            host_run_id=args.run_id,
            runs_dir=args.runs_dir,
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


def main(argv: list[str] | None = None, *, kit_factory=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        return _run_doctor(args, kit_factory)
    if args.command == "collect":
        return _run_collect(args, kit_factory)
    build_parser().error(f"未知命令: {args.command}")
    return 2
