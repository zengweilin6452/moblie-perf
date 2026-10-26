# mitmproxy 以子进程为可选依赖，缺席降级不阻断

弱网与解密档（工单 17/18）依赖 mitmproxy 的 `mitmdump` 做流量整形与 TLS 解密，但它是唯一需要独立 Python ≥3.12 venv 的外部程序，不能进入组件的零依赖承诺（ADR-0001）。决策：`netprof` 以 subprocess 拉起 `mitmdump`（解析顺序：显式参数 > env `MOBILE_PERF_MITMDUMP` > PATH），以 JSONL 日志文件为唯一数据契约，不 import mitmproxy 的任何 Python 模块——与 ADR-0002 对 Harness 的处理同构，劫持 `sys.path` 与依赖冲突的风险归零。代价是逐连接事件只能经日志文件回流（秒级延迟，对分钟级采集无关）。

依赖可选性落在行为上：`mitmdump` 缺席时 passthrough 档的 `none` 档位照常采集（无连接级指标，note 说明）；整形档把 `net_conn_duration_ms` 如实 not_tested 并附安装提示，采集闭环与其余指标不受影响。解密档因没有代理就没有数据来源，同样 not_tested。这条边界让弱网能力可以按环境渐进启用（CI 无 mitmproxy 照常绿，真机全流程装上才解锁弱网指标），而不必把 mitmproxy 变成硬依赖或把它塞进组件的 venv。
