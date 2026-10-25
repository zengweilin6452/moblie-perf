# 13: 内存泄漏趋势分析（跨 run 斜率）

**What to build:** 数据已在库（mem_pss_mb / mem_privdirty_mb 序列），只差分析层：对同 target_pkg+device_profile 的历史 run 做回归斜率（median 随墙钟时间的增长，`mem_pss_slope_mb_per_day`），并提供轮内多 run 序列视图；暴露为 `kit.leak_trend(pkg, profile=None)` 与 `mobile-perf leak <pkg>`（跨 profile 默认拒绝语义与 trend 一致）；斜率阈值由宿主经 judge 扩展点判定（如 slope > N MB/day → fail），组件只负责口径（回归方法、样本下限）。纯函数分析 + 现有 store 查询，不新增采集。

**Blocked by:** None (can start immediately)

**Status:** done（2026-10-25，perf/ticket-13 五笔提交已合并进 main；395 unit/contract 全绿。OLS 斜率 + 样本下限 3 + <24h 低置信度标记 + 三态判定；kit.leak_trend 与 mobile-perf leak 落地，跨档案语义与 trend 完全一致（共用 _resolve_profile）。真机现有 2 轮数据如实报 insufficient（不假装可信）。注意：OLS 对离群 run 不鲁棒，宿主设门禁前应看 confidence，建议跨天采集）

- [ ] 斜率纯函数 unit：合成序列（平稳/线性增长/跳变）三分支，样本下限生效
- [ ] 真机既有数据（device 门控或本地库）输出 leak_trend；跨 profile 默认拒绝、显式覆盖标记口径不一致
- [ ] judge 可对斜率产出三态结论（宿主阈值）
- [ ] 双语标签与报告/CLI 呈现；嵌入指南补充一节

## Comments
