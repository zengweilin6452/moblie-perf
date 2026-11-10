# 04 · 线程数 + FD 数补采

Status: resolved
Type: task

## 目标

SupplementPoller 同一 adb 短事务新增 proc 段：`app_thread_count`（个）、`app_fd_count`（个），时序采样入聚合与 supplement CSV。

## 改动面

- supplement.py：`collect_proc_stats=True` 默认开；pidof 段条件扩为 (collect_gpu_mem or collect_proc_stats) and target_pkg；新增段 `cat /proc/$(pidof <pkg>)/status 2>/dev/null` 与 `ls -1 /proc/$(pidof <pkg>)/fd 2>&1`。
- 解析：`parse_thread_count`（Threads: 行）、`parse_fd_count`（含 Permission denied/No such file → None，否则非空行计数——**不伪造 0**）。
- pid 不唯一/缺失 → 沿用 pid_ambiguous/pid_unavailable 降级；status 空 → proc_not_found；fd 错误 → fd_not_readable；stages 逐级留痕。
- SAMPLE_KEYS +2（CSV 列/聚合自动跟上）；_collect_degraded 覆盖两 key。
- metrics.py 登记；tests make_poller 默认关新开关（镜像 collect_gpu_mem 模式），新测试类显式开。

## 验收

- 预制 status/ls 输出解析对照；Permission denied → None + 降级原因；多 pid → 不猜。
- 既有 supplement 测试全绿（CSV 表头测试随新列显式更新）。
- 真机验收（有设备时）：确认 `$(pidof)` 内联替换在设备 shell 的语义。

## Answer

2026-11-10 完成：SupplementPoller collect_proc_stats 默认开，procstatus/procfd 段 + parse_thread_count/parse_fd_count（空段/错误 → None 不伪造 0）+ 逐级降级；pidof 与内联 $(pidof) 均过包名白名单（顺带堵了既有 pidof 段的注入口）。解析 6 例 + 轮询 7 例（含注入反例）绿；kit 夹具补 proc 段。ruff 绿。**未收口：真机验收（$(pidof) 设备 shell 语义、/proc 权限）——当前无设备在线；影响：仅真机可读性未证，单测已锁解析与降级；下次有真机时跑 tests/device 与一次 collect 即可补验。**

## Comments