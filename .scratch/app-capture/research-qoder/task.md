# 只读调研任务书：APP 抓包接入点

任务：为 mobile_perf 组件的「APP 抓包（HAR 请求/响应）」做只读代码基调研，产出一份中文设计/影响面报告 `research-qoder.md`。你由编排方通过根仓库 `dispatch.sh qoder` 调用，模型已锁定为 `Qwen3.8-Flash`。

## 硬约束

- 只读：禁止修改主项目任何源码、配置、文档、数据、设备状态；唯一允许创建/修改的文件是**当前工作目录内**的 `research-qoder.md`。
- 禁止 git 操作、安装依赖、改设备状态、commit/push/发布；禁止删除任何文件。
- 可读取 `D:/workspace/claudecode-test/moblie-xn` 的主项目源码、测试、文档；可运行只读命令（如 pytest 收集、静态阅读），但不得写出到主项目其它路径。
- 中文输出；明确区分代码可证事实与设计建议。

## 调研重点

1. 精确追踪现有代理链路：`src/mobile_perf/cli.py` → `kit.py::run_collect/collect` → `CollectScope.__enter__/finalize` → `netprof.py::NetProfiler.start/stop` → `netproxy_addon.py` 的事件；标出增加 capture 开关、产物路径与报告元数据的最小改动点及函数/参数名。
2. 检查 `netproxy_addon.py` 在 `TIER_DECRYPT`、`_DECRYPT_OK`、`TRUST_*`、`response()`、`error()`、`http_connect()` 下的行为；说明为什么 passthrough/证书拒绝时没有 HAR 请求/响应，如何不改变现有弱网档位、信任探测与 not_tested 语义。
3. 检查 `report.py::build_report/write_html/assemble_run_report` 的 `network/api/artifacts` 契约、`collector.py` 的 `runs/<runId>` 归档约定、`store.py` 的 `record_run_artifacts/record_event` 约定，给出 HAR 文件挂进报告与归档的最小安全方案（兼容旧 run）。
4. 阅读 `tests/unit/test_netprof.py`、`test_kit_netprof.py`、`test_cli.py`、`test_report.py`、`tests/device/test_netprof_device.py`，提出新增单测/契约测试/真机门控用例的文件位置与关键断言；注意保持「代理侧日志是生效唯一证据」与恢复顺序不变。
5. 提出完整 CLI 与库 API 设计建议：`--capture-har` 或等价开关、`capture_body_limit_bytes`、`capture_max_bytes`、敏感头脱敏策略；参数校验与错误退出码；默认关闭；mitmproxy 找不到或证书拒绝时的降级结论。
6. 评估 Windows / Git Bash 下 `mitmdump.exe`、路径空格、JSON/UTF-8、子进程终止与 HAR 文件 durability 的集成风险，给出可由编排方复验的检查点。

## 输出

- 报告第一行写 `RESEARCH: DONE`；按「现状证据 / 最小改动清单 / 风险与降级 / 测试计划 / 真机验收清单」分节。
- 至少列出 10 条具体文件级建议，并给出验收标准。不得输出代码补丁；建议须能直接转化为开发任务书。
- 完成后在回复中报告文件路径与一句话结论。验证标准：仅此一份报告文件、无其它文件改动，编排方可据此划定写作者白名单。
