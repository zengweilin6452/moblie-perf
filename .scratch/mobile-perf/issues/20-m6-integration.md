# 20: M6 集成、档位可比性与文档

**What to build:** 收口 M6：把 15–19 的新能力组装成一条命令可跑的全流程，并把**网络档位**确立为与设备档案并列的可比性维度。

范围：

①`kit.collect(..., net_profile=..., frame_poll=..., page=...)` 组装 netprof / frameinfo / pageflow 三者生命周期，任一模块不可用时各自降级、互不阻断；
②`sessions` 增列 `net_profile`（JSON：档位名 + 延迟/带宽/丢包参数），`kit.trend` / `kit.leak_trend` 默认按档位过滤，跨档需显式覆盖并标记口径不一致（语义与 `device_profile` 完全一致，复用 `_resolve_profile` 的形态）；baseline 记录来源 run 档位，judge 档位不匹配 → `not_tested`；
③`JudgeSpec` **不新增字段**——档位由 `kit.judge` 从报告与 baseline 来源 run 解析后注入，保持"纯函数只读报告"；
④CLI：`collect --net-profile/--frame-poll`、`netprof doctor`；`scripts/perf_full_flow.py` 支持弱网两轮对比；
⑤文档：M6 方案定稿、GLOSSARY 新增 5 条术语、嵌入指南补"弱网与逐帧"一节、ADR-0004（mitmproxy 子进程边界与可选依赖）。

**Blocked by:** 15, 16, 17, 18, 19

**Status:** done（2026-10-27，unit/contract 全绿；真机全流程验收阻塞——设备离线）

- [x] 一条命令跑通：巡检 → 弱网起 → 采集（逐帧 + GPU + 整页）→ 弱网恢复 → 基线 → 门禁 → 趋势（`scripts/perf_full_flow.py` 支持 `--net-profile/--net-tier/--frame-poll/--pages`：第一轮无弱网对照、第二轮弱网，跨档不进门禁改为逐指标并排对照并分档固化基线；真机实跑待设备恢复）
- [x] 任一子能力不可用时其余照常（逐项降级组合至少覆盖三种：无 mitmproxy、无 kgsl、App 不信任 CA；另加组合用例：mitmproxy 缺席 + weak 档 + 页面打标同跑，弱网 not_tested 与整页出值互不阻断——test_kit_pageflow.test_degraded_capabilities_do_not_block_each_other）
- [x] 跨档 trend 默认拒绝、显式覆盖可查并标记；judge 档位不匹配 → `not_tested`（unit 6 例：trend/leak 跨档默认拒绝、显式 net_profile= 过滤并标 netProfileMismatch、judge 同档照判/异档 not_tested/来源 run 跨档 not_tested、老基线不参与档位比对；真机一例待设备恢复）
- [x] 弱网异常退出后设备代理必恢复（工单 17 已实现退出必恢复与 `netprof doctor` 清理，本工单回归通过）
- [x] GLOSSARY 5 条新术语（工单 17/18 已落：网络剖面、直通/解密档、信任探测、接口级指标、接口瀑布图、连接建立耗时、代理侧证据）+ 嵌入指南 §3.2「弱网与逐帧」+ ADR-0004（mitmproxy 子进程边界与可选依赖）落地；README 能力一览与 CLI 示例更新；M6 方案标注实施收口日期
- [ ] 真机全流程实测：BYD academy 上整页耗时/逐帧/GPU 有值、接口级如实 `not_tested`，门禁结论产出（**阻塞**：设备 84cb2c4e adb 离线；整页/逐帧/GPU 三项在工单 15/16/17/18 的真机验收中已各自通过，本条是组合链路实跑）

## Comments

## Comments

- **2026-10-27 实施记录**：store `sessions` 增 `net_profile` 列（JSON：档位名+整形参数，建库早于本工单的库自动补列）；`_resolve_profile` 泛化为维度无关（getter+label），trend/leak_trend 增 `net_profile=` 参数与 `netProfile/netProfileMismatch` 输出；`kit.judge` 从 baseline 来源 run 解析档位与报告 network 段比对，不匹配 → not_tested、来源 run 跨档 → not_tested（JudgeSpec 零新增字段，纯函数未动）；CLI trend/leak 增 `--net-profile`；ADR-0004、嵌入指南 §3.2、README 落地。
- 设计取舍：judge 的档位比对放在 kit 门面而非纯函数——纯函数"只读报告"的边界不动，档位信息也不进 JudgeSpec（宿主构造态保持纯净）；来源 run 查不到（老基线）不参与比对，不因为历史数据缺列而拒绝判定。
- 遗留：真机组合链路实跑待设备恢复后补（不宣称已验收）。
