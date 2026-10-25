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

## App 侧

**弹窗免重复（Dialog Suppress）**:
SoloPi App 声明"本机不再重复弹出告知型权限弹窗"的开关状态，可被巡检确认。
_Avoid_: 权限白名单

**归档（Run Artifacts）**:
一次宿主运行落盘的原始产物集合（采集 CSV、日志、截图等）。
_Avoid_: 失败证据、冻结快照（均为 moblie-auto 语境）
