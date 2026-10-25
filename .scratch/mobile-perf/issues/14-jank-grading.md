# 14: 卡顿分级统计（掉帧严重度）

**What to build:** 现有 frame_delay 系列只有 count/max/ratio 三列（粒度受 SoloPi CSV 限制）。工单：①以真实 CSV 列能力为准设计分级——可实现的口径例如对"最长延迟"按阈值分桶（light/mid/severe：>100ms/>300ms/>700ms，阈值可配）跨样本统计；若单列粒度不足以支撑逐帧分桶，**如实采用"会话内超阈值次数"口径并记录设计偏差，不硬造**；②canonical key 新增（如 `app_jank_severe_count`，命名法合规）经字典入库；③judge 可设阈值三态；④报告双语呈现与曲线。

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] 分级口径经真实 CSV（tests/fixtures/solopi_csv_sample）校验，粒度限制如实记录
- [ ] 新 canonical key 入字典入库（聚合行），unit 覆盖分桶边界
- [ ] judge 三态 + report/trend 双语呈现
- [ ] 若发现 SoloPi 端侧可低成本增强（现有二开位），单列建议不动手

## Comments
