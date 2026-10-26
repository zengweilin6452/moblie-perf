# 19: 整页加载耗时（末帧稳定口径）

**What to build:** 新增 `pageflow` 模块与语义化打标 `s.mark_page("home")`（落 `events.kind=page`，记录页面起点 t0），据此计算**整页加载耗时** `page_load_ms`：从 t0 起，取首个「相邻帧间隔 ≥ QUIET_GAP_MS（默认 300 ms）」的位置 t1，`page_load_ms = t1 − t0`。

口径说明（写进模块文档与报告）：

- 为什么不用截图差分：`screencap` 实测 0.45 s/次，采样率不足且每次拉起渲染管线会干扰被测 App；帧时刻是零成本旁路数据，精度更高。
- 网络侧"最后一条连接关闭时刻"只作**佐证字段**写入事件 detail，不参与主值——HTTP/2 多路复用会让连接存活远超请求完成。
- 已知局限如实保留：无限轮播/持续动画页面永不静默 → `not_tested`（不猜）；"稳定"是视觉稳定，不保证懒加载数据已就绪；首帧前空白期计入耗时（这是用户感知等待的真实组成）。
- 超时保护：`t1 − t0 > PAGE_TIMEOUT_MS`（默认 15 s）→ `not_tested`。

页面打标可多次调用（多页面），每个页面各出一条 `page_load_ms` 事件与一条聚合入库记录（以页面名为区分维度，写入事件 detail 与报告表格）。

**Blocked by:** 15（帧时刻序列）、17（弱网档位下整页耗时才有对比价值）

**Status:** done（2026-10-27，unit 全绿；真机验收阻塞——设备 84cb2c4e 离线）

- [x] 稳定判定纯函数：合成帧序列覆盖「快速加载」「慢速加载」「无限动画（超时）」「零帧」「首帧即静默」五分支（test_pageflow.py 16 例，另含中途停顿、乱序、观测不足、超限不裁剪）
- [x] `mark_page` 与既有 `mark` 语义区分清晰，事件 kind 与时间轴对齐正确（marks 仅 page 标带 kind 字段，普通标契约不变）
- [x] `page_load_ms` 进指标字典并入库（source=pc_orchestration）；多页面每页一个样本（metrics 表主键 (run_id, metric_key) 容不下多行，逐页值进报告 pages 与事件）
- [x] 超时与不可判定场景如实 `not_tested`，原因写明（无限动画/超时/未开逐帧/取不到 uptime 偏移，四路原因互不混淆）
- [ ] 真机验证：BYD academy 首页 `page_load_ms` 有值，且 `none` 与 `weak` 两档下数值差异符合预期（**阻塞**：验收期间设备 84cb2c4e adb 离线；unit 侧全链路已用合成 framestats+/proc/uptime 替身覆盖）
- [x] 报告呈现整页耗时（逐页表格 + 时间轴标注追加到启动竖线不互相覆盖），并与启动耗时区分展示

## Comments

## Comments

- **2026-10-27 实施记录**：新增 `pageflow.py`（纯计算：末帧稳定口径，四态 ok/timeout/no_frames/incomplete）；`frameinfo` 增 `completed_s` 列与括号式 `measure_clock_offset`（读 /proc/uptime 前后各测一次宿主时钟取中点，半程往返抵消，rttMs 随报告留档）；`session` 增 `mark_page/page_marks`（Mark.kind）；`kit.finalize` 在有页面标时换算帧时刻、算逐页结论、入库/事件/not_tested 标注；报告新增逐页表格与时间轴标注（与启动竖线追加共存）。
- 时钟域说明：`FrameCompleted` 是设备 CLOCK_MONOTONIC，`/proc/uptime` 是 CLOCK_BOOTTIME，二者差累计休眠时间；交互式会话屏幕常亮时按 uptime 换算，若会话中设备休眠过则整页值整体偏移（模块文档已记）。
- 遗留：真机两档对比验收待设备恢复后补（不宣称已验收）。
