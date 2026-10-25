# 08: M2 集成验收——无人值守全流程

**What to build:** 串联 05/06/07 的集成验收责任田（§8 M2）：含 FPS 的采集全程零人工交互；手机重启后 `ensure_ready()` 自动恢复；自编译 APK 稳定替换官方版；crash 被记录。此票不做新功能，只做端到端验证与修复暴露的问题。

**Blocked by:** 05, 06, 07

**Status:** done（2026-10-25，perf/ticket-08 已合并进 mobile-perf-dev（289 unit/contract 全绿）。真机实测：cutover 切换 1.0.3→1.0.3.1 成功（回滚包已确认在 downloads/ 未动用）；弹窗开关写入读回；**含 FPS 无人值守采集闭环**（fps median 116、mem_pss 196.8MB、integrity passed、零人工交互）；am crash 入库对齐时间轴；adb reboot 后 ensure_ready 自动恢复。过程中解决三个设备侧阻断（内部 adb 密钥植入、deviceidle 白名单、存储权限）。待用户：/sdcard/ 下两个 b64 中转文件待批准清理；今后 am force-stop 会清无障碍授权需重跑 settings put 命令）

- [ ] 含 FPS 的采集从 ensure_ready 到 report 全程无人工交互
- [ ] 重启手机 → ensure_ready() 自动恢复（tcpip + 无障碍 + 弹窗开关）
- [ ] 人为制造 crash 被记录且对齐时间轴
- [ ] 设备上官方版 SoloPi 已被自编译版替换且工作正常

## Comments
