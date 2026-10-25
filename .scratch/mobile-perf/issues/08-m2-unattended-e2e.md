# 08: M2 集成验收——无人值守全流程

**What to build:** 串联 05/06/07 的集成验收责任田（§8 M2）：含 FPS 的采集全程零人工交互；手机重启后 `ensure_ready()` 自动恢复；自编译 APK 稳定替换官方版；crash 被记录。此票不做新功能，只做端到端验证与修复暴露的问题。

**Blocked by:** 05, 06, 07

**Status:** ready-for-agent

- [ ] 含 FPS 的采集从 ensure_ready 到 report 全程无人工交互
- [ ] 重启手机 → ensure_ready() 自动恢复（tcpip + 无障碍 + 弹窗开关）
- [ ] 人为制造 crash 被记录且对齐时间轴
- [ ] 设备上官方版 SoloPi 已被自编译版替换且工作正常

## Comments
