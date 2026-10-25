#!/usr/bin/env python3
r"""Print a compact summary table from a perf-analyze JSON report.

Usage: python tools/perf-summary.py <path\to\perf_analysis.json>
"""
import json
import os
import sys

SKIP_COLUMNS = {"RecordTime", "SimpleTime"}


def main():
    if len(sys.argv) != 2:
        print("usage: perf-summary.py <perf_analysis.json>")
        return 1
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
    for item in data["files"]:
        name = os.path.basename(item["path"])
        for col in item["columns"]:
            if col["kind"] != "numeric" or col["name"] in SKIP_COLUMNS:
                continue
            print(f"{name[:28]:30s} {col['name'][:34]:36s} "
                  f"median={col['median']:.2f} mean={col['mean']:.2f} "
                  f"max={col['max']:.2f} p90={col['p90']:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
