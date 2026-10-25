# 11: M4 嵌入联调——pytest 钩子 + moblie-auto 联动 + 前台干扰专项

**What to build:** pytest 钩子 `--mobile-perf`（对称 moblie-auto 的 `--mobile-evidence`，§6.5）；moblie-auto 侧 DeviceGate 适配文件（依赖方向：moblie-auto → mobile_perf，ADR-0001）；在 moblie-auto 仓库跑通"UI 滑动 + 性能采集 + 门禁结论"同 run 用例；**前台干扰专项**（Q16）：探明 HarnessSession 默认拉起 SoloPi 抢前台的手段并规避，验收"采集命令执行前后被测应用前台状态不变"；pytest-xdist 并发验证（SQLite WAL 串行写者）；接入文档。

**Blocked by:** 08, 09

**Status:** done（2026-10-25，perf/ticket-11 五笔提交已合并进 mobile-perf-dev（300 unit/contract 全绿）。pytest 钩子 --mobile-perf 落地；前台专项实证：Harness doctor/perf-start/perf-stop 均会抢前台且本机 mCurrentFocus=null 使 preserve_foreground 失效 → 已用前台守卫（am start 还原 + foreground 事件入库）实现"采集前后前台不变"（真机验证通过）；xdist 同设备 fast-fail/异设备全绿，修复并发首连 WAL 排他锁缺陷。moblie-auto 工作区新增 3 文件未提交（_perf_gate.py + 2 测试）；宿主 scrcpy 缺 touch_down 是既有缺口绕过未修）

- [ ] moblie-auto 仓库内用例：UI 滑动 + 采集 + 门禁结论，全程零人工干预
- [ ] 采集命令执行前后被测应用前台状态不变（专项记录证据）
- [ ] xdist 两 worker 并发采集不死锁、不脏写
- [ ] moblie-auto 侧 gate 适配文件 + 接入文档齐

## Comments
