# 05 · 热状态/降频档补采

Status: resolved
Type: task
Blocked by: 04

## 目标

SupplementPoller 新增 thermal/cpufreq 段：`device_thermal_status`（级，0–6）、`cpu_freq_max_mhz`（MHz，各核当前频率最大）。**只记录不判定**——热状态是性能可比性的混杂因子，默认无阈值，阈值归宿主。

## 改动面

- supplement.py：`collect_thermal=True` 默认开；段 `dumpsys thermalservice` 与 `cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq 2>/dev/null`。
- 解析：`parse_thermal_status`（thermal status: 行，0–6 越界 → None）、`parse_cpu_freq_max_mhz`（kHz→MHz 取最大；空/全非正 → None）。
- 降级：thermalservice 无输出 → unparseable_or_unsupported（老版本无此服务，C-116）；sysfs 不可读 → not_readable；stages 留痕。
- SAMPLE_KEYS +2；_collect_degraded 覆盖；metrics.py 登记；make_poller 默认关，新测试类显式开。
- GLOSSARY 增「热状态档」术语（混杂因子、只记录不判定）。

## 验收

- 预制 thermalservice/cpufreq 输出解析对照；垃圾输出 → None + 降级；0 档是有效读数不降级。
- 既有测试全绿；真机验收（有设备时）确认两源可读性与版本降级。

## Answer

2026-11-10 完成：collect_thermal 默认开，thermal/cpufreq 段 + parse_thermal_status（0–6，0 档有效）/parse_cpu_freq_max_mhz（各核最大 kHz→MHz）+ 逐级降级；字典/GLOSSARY「热状态档」（混杂因子只记录不判定）同步。解析 7 例 + 轮询 6 例绿；kit 夹具补两段。ruff 绿。**未收口：真机验收（thermalservice/sysfs 可读性按机型/版本降级）——无设备在线；下次有真机跑一次 collect 看 degraded 即可补验。**

## Comments