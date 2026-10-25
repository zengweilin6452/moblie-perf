# 03: 指标字典 + SQLite 聚合入库 + 自动清理

**What to build:** metrics.py 指标字典（canonical key `<对象>_<指标>_<单位>` ↔ 中文列名，全组件唯一翻译层，§6.4/§7），对 `perf-output/` 真实 CSV 校准定稿。collector：stop 后自动 pull → 行数校验 → 聚合（min/median/mean/max/p90）→ SQLite（`<repo>/data/mobile_perf.db`，WAL + busy_timeout=5s）入库 → 删设备端会话目录；行数不一致保留 + 告警。本地保留 200 run / 30 天（可配置，仅清理本组件数据）。

**Blocked by:** 01

**Status:** ready-for-agent

- [ ] 指标字典对真实 CSV 校准定稿；contract 测试锁住中文列名映射（上游列名变化即测试红）
- [ ] 连续两次 collect 后 sessions/metrics 表有正确聚合行（device 验证）
- [ ] 设备端会话目录 pull 校验后被删除；人为破坏一处文件 → 保留 + 告警事件
- [ ] 本地清理策略生效（unit 用可配置阈值验证）

## Comments
