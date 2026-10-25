# 20: M6 集成、档位可比性与文档

**What to build:** 收口 M6：把 15–19 的新能力组装成一条命令可跑的全流程，并把**网络档位**确立为与设备档案并列的可比性维度。

范围：

①`kit.collect(..., net_profile=..., frame_poll=..., page=...)` 组装 netprof / frameinfo / pageflow 三者生命周期，任一模块不可用时各自降级、互不阻断；
②`sessions` 增列 `net_profile`（JSON：档位名 + 延迟/带宽/丢包参数），`kit.trend` / `kit.leak_trend` 默认按档位过滤，跨档需显式覆盖并标记口径不一致（语义与 `device_profile` 完全一致，复用 `_resolve_profile` 的形态）；baseline 记录来源 run 档位，judge 档位不匹配 → `not_tested`；
③`JudgeSpec` **不新增字段**——档位由 `kit.judge` 从报告与 baseline 来源 run 解析后注入，保持"纯函数只读报告"；
④CLI：`collect --net-profile/--frame-poll`、`netprof doctor`；`scripts/perf_full_flow.py` 支持弱网两轮对比；
⑤文档：M6 方案定稿、GLOSSARY 新增 5 条术语、嵌入指南补"弱网与逐帧"一节、ADR-0004（mitmproxy 子进程边界与可选依赖）。

**Blocked by:** 15, 16, 17, 18, 19

**Status:** ready-for-agent

- [ ] 一条命令跑通：巡检 → 弱网起 → 采集（逐帧 + GPU + 整页）→ 弱网恢复 → 基线 → 门禁 → 趋势
- [ ] 任一子能力不可用时其余照常（逐项降级组合至少覆盖三种：无 mitmproxy、无 kgsl、App 不信任 CA）
- [ ] 跨档 trend 默认拒绝、显式覆盖可查并标记；judge 档位不匹配 → `not_tested`（unit + 真机各一例）
- [ ] 弱网异常退出后设备代理必恢复（`netprof doctor` 复验）
- [ ] GLOSSARY 5 条新术语 + 嵌入指南新节 + ADR-0004 落地；README 能力一览更新
- [ ] 真机全流程实测：BYD academy 上整页耗时/逐帧/GPU 有值、接口级如实 `not_tested`，门禁结论产出

## Comments
