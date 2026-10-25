# SoloPi Harness 以子进程进程边界集成

mobile_perf 通过 subprocess 调用 harness CLI（`solopi_ai.py`）并以 JSON stdout 为契约，不 import 其内部模块。原因：harness 在 import 时把自身目录注入 `sys.path` 并以顶级名导入兄弟模块（`timing` 等，宿主同名模块会被劫持），动作函数返回 `(dict, int)` 但耦合 `argparse.Namespace`，导入耦合风险高。子进程损失的只是流式状态回调与连接复用（每次约 150ms 进程启动，对分钟级采集无关），且 `perf-stop` 自带产物拉取，采集闭环无损；适配集中在 `solo.py` 单模块并锁定 harness 快照版本，上游升级只需回归该模块的契约测试。import 直连留作将来真需要流式事件时再评估。
