# mobile_perf（App 性能采集组件）

本上下文覆盖基于 SoloPi 端侧采集的性能测试组件：采集会话的生命周期、设备准入、指标标准化与门禁判定。与 moblie-auto（设备会话与 UI 自动化）通过设备准入协议协作，但不共享代码。

## 采集与运行

**采集会话（Collect Session）**:
一次端侧性能采集从开始到停止的完整生命周期，由组件发起、SoloPi App 执行；内含 SoloPi 的 sessionId 作为端侧身份。
_Avoid_: 设备会话（moblie-auto 语境）、录制

**宿主运行（Host Run）**:
宿主（测试用例或平台）的一次执行，可包含多个采集会话与打标，是所有产物的上层归属身份。
_Avoid_: 任务（未来平台语境另定）

**打标（Mark）**:
宿主在采集会话内声明的时刻点，用于把宿主步骤与采集时间轴对齐。
_Avoid_: 检查点

**启动模式（Start Mode）**:
编排层在采集窗口内对被测 App 施加的启动方式：cold=强制停止后拉起（冷启动），warm=退后台后再拉起（温启动）；启动发生在采集窗口内，使启动期资源曲线覆盖启动时刻。计得的启动毫秒数经指标字典入库（source=pc_orchestration）。
_Avoid_: 启动测试、am start 参数

**卡顿分级（Jank Grading）**:
按每个采样窗口的「最长延迟时间」是否超过阈值（默认 >100 / >300 / >700 ms，宿主可配）分档计数，得到轻/重/严重三档窗口数。分桶互斥，只计最高档。SoloPi CSV 没有逐帧延迟分布，因此口径是"窗口级超阈次数"而非逐帧卡顿数；粒度限制如实记录，不硬造逐帧数据。
_Avoid_: 掉帧率、逐帧卡顿（当前数据粒度不支持）、ANR（另一概念）

## 设备与准入

**设备准入（Device Gate）**:
发起设备命令前获取、完成后即归还的使用权协议；采集进行中不持有设备权。
_Avoid_: 设备租约（moblie-auto 的实现名）、锁

**设备档案（Device Profile）**:
机型与操作系统大版本的组合，是基线可比性的最小单位。
_Avoid_: Harness 内部的 DeviceProfile（模型能力兼容用途）、设备会话

**巡检（Keeper）**:
组件的设备可用性职责：连接自愈、无障碍巡检、充电与前台检查；按需触发，不持有设备权。
_Avoid_: 守护进程、_LeaseGuard（moblie-auto 语境）

## 指标与判定

**SoloPi 采集项（Collect Items）**:
发起采集时传给 SoloPi 的原始项名（如 CPU/FPS/Memory/Battery）；库 API 与 CLI 参数名 `items` 沿用上游契约。经指标字典翻译为指标（Metric）。
_Avoid_: 指标（Metric，规范化后的测量量）、CSV 列名

**指标（Metric）**:
由规范英文键、单位与来源定义的标准测量量；SoloPi 的中文原始列名仅存在于指标字典中。
_Avoid_: 采集项（items）、CSV 列名

**指标字典（Metric Dictionary）**:
规范指标与原始列名、补采来源之间的唯一映射。
_Avoid_: 翻译表、对照表

**统计口径（Statistic Scope）**:
指标统计的窗口、聚合方式（中位数）与样本数下限的技术契约，归组件所有。
_Avoid_: 阈值（业务决策，归宿主）

**基线（Baseline）**:
某设备档案下某指标的参照值，来源于一个历史采集会话。
_Avoid_: 模板版本（moblie-auto 语境）

**门禁契约（Judge Spec）**:
宿主显式传入的判定输入：基线与阈值列表。

**门禁结论（Verdict）**:
passed / failed / not_tested 三态判定结果，附证据引用；产生它的机制称 Judge。
_Avoid_: doctor（设备就绪检查，另一概念）

**泄漏趋势（Leak Trend）**:
同 target_pkg + device_profile 的历史 run 中，指标 median 随墙钟时间做
普通最小二乘回归得到的增长速率（`mem_pss_slope_mb_per_day`，MB/day），
附样本数与置信度标记；回归方法与样本下限归组件，斜率阈值归宿主。
_Avoid_: 内存曲线（单 run 序列，不含跨 run 回归）、内存增长（未给时间口径）

**逐帧口径（Per-frame Scope）**:
以单帧耗时（`gfxinfo framestats` 的 FrameCompleted − IntendedVsync）为基础
统计的卡顿口径，给出 P50/P90/P99 与超时帧占比。与「卡顿分级」的窗口级口径
粒度不同、不可互算，两者在报告中并列展示。
_Avoid_: 卡顿分级（窗口级口径，另一概念）、掉帧率

**整页加载耗时（Page Load Time）**:
从页面打标（Page Mark）到画面进入稳定（连续无新帧）的耗时。稳定判定是视觉
稳定，不保证数据已全部就绪；持续动画页面不可判定，如实标 not_tested。
_Avoid_: 启动耗时（进程冷/温启动，另一概念）、首屏时间（未定义稳定判据）

**页面打标（Page Mark）**:
宿主声明的页面起点，作为整页加载耗时的计时原点；与普通打标共用统一时间轴，
事件类别为 page。
_Avoid_: 打标（不含页面语义的普通时刻点）

## 弱网与网络剖面

**网络剖面（Network Profile）**:
一次采集会话施加的网络条件（档位名 + 延迟/带宽/丢包参数），是与设备档案并列
的可比性维度；不同档位的 run 不可直接比趋势、不可共用基线。
_Avoid_: 弱网开关、网络配置

**直通档 / 解密档（Passthrough / Decrypt Tier）**:
弱网代理是否解密 TLS 的两种工作模式。直通档只能拿到连接级指标（连接建立与
传输耗时、字节数）；解密档才能拿到接口级指标（URL、状态码、响应大小）。
_Avoid_: 抓包模式、代理模式

**信任探测（Trust Probe）**:
会话开始前对被测 App 是否信任代理证书的一次性判定。失败即降级为直通档并
如实标注接口级指标 not_tested，不重试、不猜测。
_Avoid_: 证书校验、证书安装

## App 侧

**弹窗免重复（Dialog Suppress）**:
SoloPi App 声明"本机不再重复弹出告知型权限弹窗"的开关状态，可被巡检确认。
_Avoid_: 权限白名单

**归档（Run Artifacts）**:
一次宿主运行落盘的原始产物集合（采集 CSV、日志、截图等）。
_Avoid_: 失败证据、冻结快照（均为 moblie-auto 语境）
