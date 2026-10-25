"""全流程性能测试脚本：巡检 → 两轮采集（交互打标）→ 固化基线 → 门禁判定 → 趋势。

用法：
    PYTHONIOENCODING=utf-8 PYTHONPATH=src python scripts/perf_full_flow.py \
        --package com.sinosoft.trainapp --activity .MainActivity \
        --serial 749622c9 --baseline trainapp-main-v1 --duration-swipe 8

不带参数时使用 --help 查看全部选项。两轮采集各含若干次滑动交互，模拟浏览负载。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from mobile_perf import JudgeSpec, PerfKit, SessionSpec

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ADB = REPO_ROOT / "SoloPi" / "platform-tools" / "adb.exe"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="mobile_perf 全流程性能测试")
    p.add_argument("--package", required=True, help="目标包名")
    p.add_argument("--activity", default=".MainActivity", help="启动 Activity（相对包名）")
    p.add_argument("--serial", required=True, help="设备 serial")
    p.add_argument("--adb", default=str(DEFAULT_ADB), help="adb 路径")
    p.add_argument("--items", default="CPU,FPS,Memory,Network,Battery", help="采集项，逗号分隔")
    p.add_argument("--baseline", default=None, help="基线名（缺省 <包名末段>-main-v1）")
    p.add_argument("--profile", default=None, help="设备档案（缺省自动用 serial 探测的机型/OS）")
    p.add_argument("--swipes", type=int, default=8, help="每轮滑动次数")
    p.add_argument("--swipe-gap", type=float, default=5.0, help="滑动间隔秒")
    p.add_argument("--threshold", action="append", default=[],
                   metavar="METRIC:OP:VALUE:LEVEL", help="门禁阈值，可重复")
    p.add_argument("--start-mode", choices=["cold", "warm"], default=None,
                   help="采集窗口内执行启动计时（cold/warm）")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    adb = args.adb
    baseline = args.baseline or f"{args.package.rsplit('.', 1)[-1]}-main-v1"

    def sh(*cmd: str) -> str:
        return subprocess.run(
            [adb, "-s", args.serial, *cmd], capture_output=True, text=True, timeout=60
        ).stdout.strip()

    profile = args.profile
    if not profile:  # 未显式指定时按机型+OS 大版本自动构造设备档案
        model = sh("shell", "getprop", "ro.product.model")
        os_ver = sh("shell", "getprop", "ro.build.version.release")
        profile = f"{model}-Android{os_ver.split('.')[0]}" if model and os_ver else None
        print(f"== 0. 设备档案自动探测：{profile}")

    kit = PerfKit(serial=args.serial, device_profile=profile)

    print("== 1. ensure_ready（巡检/自愈/充电/弹窗开关/时钟校准）")
    readiness = kit.ensure_ready()
    for attempt in range(2, 5):  # tcpip 自愈会重启 adbd，USB 设备瞬断，第二次巡检才稳定
        if readiness.ok is not False:
            break
        print(f"  第 {attempt - 1} 次未就绪（healed={readiness.healed}），2s 后重试")
        time.sleep(2)
        readiness = kit.ensure_ready()
    print(f"  ok={readiness.ok} degraded={readiness.degraded_items} "
          f"clock_offset={readiness.clock_offset_s}s")
    if readiness.ok is False:
        print("设备未就绪，中止", file=sys.stderr)
        return 2

    def drive(phase: str, session) -> None:
        for i in range(args.swipes):
            sh("shell", "input", "swipe", "540", "1600", "540", "400", "600")
            time.sleep(args.swipe_gap)
            if i == args.swipes // 2 - 1:
                session.mark(f"{phase}_half")

    def launch() -> None:
        sh("shell", "am", "force-stop", args.package)
        time.sleep(1)
        sh("shell", "am", "start", "-n", f"{args.package}/{args.activity}")
        time.sleep(4)

    spec = SessionSpec(target=args.package, items=args.items.split(","))
    stamp = time.strftime("%H%M%S")

    print("== 2. 启动目标 App")
    launch()

    print("== 3. 第一轮采集（建立基线）")
    with kit.collect(spec, host_run_id=f"{baseline}-run1-{stamp}",
                     start_mode=args.start_mode) as s:
        s.mark("run1_start")
        drive("run1", s)
        s.mark("run1_end")
    run1 = s.finalize()
    print(f"  run1={run1['runId']} samples={run1.get('sampleCount')}")

    print("== 4. 固化基线")
    kit.save_baseline(run1, baseline)
    print(f"  baseline={baseline}")

    print("== 5. 第二轮采集（同口径复测）")
    launch()
    with kit.collect(spec, host_run_id=f"{baseline}-run2-{stamp}",
                     start_mode=args.start_mode) as s:
        s.mark("run2_start")
        drive("run2", s)
        s.mark("run2_end")
    run2 = s.finalize()
    print(f"  run2={run2['runId']} samples={run2.get('sampleCount')}")

    print("== 6. 门禁判定")
    thresholds: dict[str, tuple] = {}
    for raw in args.threshold:
        key, op, value, level = raw.split(":")
        thresholds[key] = (op, float(value), level)
    if not thresholds:
        print("  （未提供 --threshold，跳过门禁）")
        verdict_status = None
    else:
        verdict = kit.judge(run2, JudgeSpec(baseline=baseline, thresholds=thresholds,
                                           min_samples=1))
        data = verdict.to_dict() if hasattr(verdict, "to_dict") else dict(verdict)
        verdict_status = data.get("status")
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))

    print("== 7. 趋势（两轮对比）")
    metric = "fps_frame"
    print(json.dumps(kit.trend(args.package, metric, profile=profile),
                     ensure_ascii=False, indent=2, default=str))

    print(f"== 报告：runs/{run2['runId']}/report.html 与 report.json")
    return 0 if verdict_status in ("passed", None) else 1


if __name__ == "__main__":
    sys.exit(main())
