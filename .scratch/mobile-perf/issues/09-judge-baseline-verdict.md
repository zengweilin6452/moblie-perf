# 09: judge 三态门禁 + 基线绑定设备档案

**What to build:** judge.py 纯函数（Q7）：`kit.judge(report, JudgeSpec(baseline, thresholds))` → pass / fail / not_tested 三态 + 证据引用；baselines 表绑定 device_profile（机型+OS 大版本，Q6）；口径归组件（median + 显式窗口 + 样本数下限），阈值归宿主不入库（§5）；样本不足 / profile 不匹配 / 前置不满足 → not_tested。库 API 与 `mobile-perf report <runId> --baseline <name>` 双入口。

**Blocked by:** 03, 04

**Status:** done（2026-10-25，perf/ticket-09 六笔提交已合并进 mobile-perf-dev；249 unit/contract 全绿。judge 纯函数三态全分支单测覆盖；baseline 绑定 device_profile 严格匹配；阈值 CLI 传入不入库。注意：warn 级越界不改三态（记入 warnings）；store 列名沿用 source_run_id（与设计文档 source_session_id 语义一致））

- [ ] 设阈值后对既有 run 产出 pass/fail 结论与证据引用（median 对比 + 窗口）
- [ ] 跨 device_profile 判定 → not_tested（严格匹配）
- [ ] 样本数低于下限 → not_tested
- [ ] judge 纯函数单测覆盖三态全部分支与 warn/fail 分级

## Comments
