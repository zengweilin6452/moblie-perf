# 06: SoloPi App 弹窗免重复二开 + 自编译 APK 单活线

**What to build:** App 侧唯一硬二开（ADR-0003 单活线，**不可逆**）：FPS 告知型权限弹窗加"本机不再提示"（SP 持久化，Q10）+ adb 可读写开关通道（既有 scheme 加动作或 broadcast），keeper 可经 adb 确认开关状态；CSV 落盘英文名开关（§4 #2，列名不动）；JDK 11 一键 build（已装 D:\Java）；一键切换脚本：卸载官方版 → 装自编译（版本 1.0.3.1 起）→ doctor → 开关引导（卸载丢设置与无障碍授权，一次性损失）。二开保持最小 diff 可 rebase 基线 tag。

**Blocked by:** 04（软依赖：等 M1 组件稳定后再动 App 与真机切换）

**Status:** ready-for-agent

- [ ] 勾选"不再提示"后重启 App，弹窗不再出现；keeper 经 adb 读到开关状态
- [ ] CSV 落盘英文名（列名保持中文，由指标字典翻译）
- [ ] build 一键出 APK；切换脚本跑通，doctor 通过，真机稳定替换官方版
- [ ] App 二开 diff 最小；PATCH_SIGNING 约束随构建文档维护

## Comments
