# 19: 整页加载耗时（末帧稳定口径）

**What to build:** 新增 `pageflow` 模块与语义化打标 `s.mark_page("home")`（落 `events.kind=page`，记录页面起点 t0），据此计算**整页加载耗时** `page_load_ms`：从 t0 起，取首个「相邻帧间隔 ≥ QUIET_GAP_MS（默认 300 ms）」的位置 t1，`page_load_ms = t1 − t0`。

口径说明（写进模块文档与报告）：

- 为什么不用截图差分：`screencap` 实测 0.45 s/次，采样率不足且每次拉起渲染管线会干扰被测 App；帧时刻是零成本旁路数据，精度更高。
- 网络侧"最后一条连接关闭时刻"只作**佐证字段**写入事件 detail，不参与主值——HTTP/2 多路复用会让连接存活远超请求完成。
- 已知局限如实保留：无限轮播/持续动画页面永不静默 → `not_tested`（不猜）；"稳定"是视觉稳定，不保证懒加载数据已就绪；首帧前空白期计入耗时（这是用户感知等待的真实组成）。
- 超时保护：`t1 − t0 > PAGE_TIMEOUT_MS`（默认 15 s）→ `not_tested`。

页面打标可多次调用（多页面），每个页面各出一条 `page_load_ms` 事件与一条聚合入库记录（以页面名为区分维度，写入事件 detail 与报告表格）。

**Blocked by:** 15（帧时刻序列）、17（弱网档位下整页耗时才有对比价值）

**Status:** done（2026-10-29，unit 全绿 + 真机单档验收与两档对比均实跑通过）

- [x] 稳定判定纯函数：合成帧序列覆盖「快速加载」「慢速加载」「无限动画（超时）」「零帧」「首帧即静默」五分支（test_pageflow.py 16 例，另含中途停顿、乱序、观测不足、超限不裁剪）
- [x] `mark_page` 与既有 `mark` 语义区分清晰，事件 kind 与时间轴对齐正确（marks 仅 page 标带 kind 字段，普通标契约不变）
- [x] `page_load_ms` 进指标字典并入库（source=pc_orchestration）；多页面每页一个样本（metrics 表主键 (run_id, metric_key) 容不下多行，逐页值进报告 pages 与事件）
- [x] 超时与不可判定场景如实 `not_tested`，原因写明（无限动画/超时/未开逐帧/无帧锚点，四路原因互不混淆）
- [x] 真机验证（2026-10-29，749622c9 / 2206123SC-Android14）：BYD 培训 App 两页 `page_load_ms` 有值（740/549 ms 与 2607/381 ms 两轮），`clockOffsetSource=frame_anchor`、9 个锚点、`clockPrecisionMs=47 ms`；两轮偏移差 28.5 s 与两次运行的墙钟间隔一致
- [x] `none` 与 `weak` 两档对比（2026-10-29，装 mitmproxy 12.3.1 后）：弱网档 `effective=true`（23 个 CONNECT、461 KB 下行，代理侧观察到），`net_conn_duration_ms` 中位 667 ms / p90 843 ms 对上 400 ms 延迟+400 kbps 整形；但两页 `page_load_ms` 未体现档位差异（对照 736/317，弱网 317/578）——该指标是**视觉**稳定，骨架屏先稳、数据后到，属已记录的口径局限，不是缺陷
- [x] 报告呈现整页耗时（逐页表格 + 时间轴标注追加到启动竖线不互相覆盖），并与启动耗时区分展示

## Comments

## Comments

- **2026-10-27 实施记录**：新增 `pageflow.py`（纯计算：末帧稳定口径，四态 ok/timeout/no_frames/incomplete）；`frameinfo` 增 `completed_s` 列与括号式 `measure_clock_offset`（读 /proc/uptime 前后各测一次宿主时钟取中点，半程往返抵消，rttMs 随报告留档）；`session` 增 `mark_page/page_marks`（Mark.kind）；`kit.finalize` 在有页面标时换算帧时刻、算逐页结论、入库/事件/not_tested 标注；报告新增逐页表格与时间轴标注（与启动竖线追加共存）。
- 时钟域说明：`FrameCompleted` 是设备 CLOCK_MONOTONIC，`/proc/uptime` 是 CLOCK_BOOTTIME，二者差累计休眠时间；交互式会话屏幕常亮时按 uptime 换算，若会话中设备休眠过则整页值整体偏移（模块文档已记）。
- **2026-10-29 真机复验推翻初版时钟换算**（两个缺陷，均已修）：
  1. `/proc/uptime` 是 BOOTTIME（含休眠），与 MONOTONIC 差**累计休眠**。实测该机帧时刻 86054 s 而 uptime 371789 s，差 285734 s（3.3 天），全部帧被推到页面起点之前，两页齐判 `no_frames`——**任何睡过觉的设备都会中招**。改为**帧锚点法**（`estimate_clock_offset`）：每轮把宿主会话时刻中点与该轮最新帧时刻配对，取候选最小值；不依赖任何额外设备时钟源，也不受休眠影响。顺带给出 `clockPrecisionMs`（选中轮的半个 adb 往返，实测 47 ms）。
  2. 锚点第一版把宿主侧取成**绝对单调读数**，而页面打标是**会话相对秒**，两套原点混算使帧时刻整体落在 +7357 s，两页齐判 `timeout`。锚点必须与 `mark_page` 同一条时间轴（`CollectScope` 把 `session.elapsed_s` 传给轮询器），回归测试用 50000 s 基数的假时钟守住这一点。
- `scripts/perf_full_flow.py`：页面打标原本放在 `sleep(swipe_gap)` **之后**，等于以"上一页已加载完"为起点；改为紧跟触发翻页的那一下。另加 `--allow-degraded`（SoloPi 端内 adb 权限未开时巡检 ok=False，但采集本身可用）。
- **2026-10-29 弱网档补验**：mitmproxy 装好后先证明机制（设备侧 curl：直连 0.09-0.12 s vs 走代理 5.1-6.3 s，HTTPS 才整形——明文 HTTP 按设计不在 addon 范围），再跑两轮对比。期间发现并修复 `perf_full_flow.py` 的编排顺序问题：冷启动原本在采集窗口**外**，首屏请求在代理施加前就发完，弱网轮代理侧 0 连接、档位等于没生效；改为窗口内冷启动后 23 个 CONNECT。
