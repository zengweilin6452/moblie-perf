# 05: keeper 巡检自愈 + doctor

**What to build:** keeper.py 设备可用性内聚（Q11）：doctor 巡检 + 自愈——`adb tcpip 5555` 重启失效自动补建重连（§4 #3）、无障碍"假开启"检测（`dumpsys accessibility` Bound 空 → 设备标 needs_attention + 事件，§4 #4）、充电状态检查（充电时 Battery/温度项 not_tested 降级，Q12）、前台应用检查、时钟偏移校准（会话开始校准 device-host 偏移，>2s 记 warning）、弹窗开关确认。`ensure_ready()` 幂等可重入；巡检动作走短事务 gate，不持设备权。

**Blocked by:** 02

**Status:** ready-for-agent

- [ ] 手机 adb tcpip 断开后，`ensure_ready()` / `mobile-perf doctor` 自动恢复连接
- [ ] 无障碍假开启 → 设备标 needs_attention 并产生事件（不代开，如实暴露）
- [ ] 充电状态下采集 → Battery/温度项 not_tested，其余照采
- [ ] doctor 重复执行无副作用（幂等）；巡检不阻塞宿主长任务（短事务）

## Comments
