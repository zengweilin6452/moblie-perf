# 04: 报告双产物 + 趋势

**What to build:** report.py：每个 run 产出单文件 HTML（内嵌**离线 ECharts**，无 CDN 依赖，Q8）+ 同内容 JSON；`mobile-perf report <runId>`；趋势：`mobile-perf trend <pkg> --metric <key>` 与库 API `kit.trend(pkg, metric, profile=...)`，跨 profile 默认拒绝、显式传参覆盖并标记口径不一致（Q6）。

**Blocked by:** 03

**Status:** done（2026-10-25，perf/ticket-04 五笔提交已合并进 mobile-perf-dev @ 0346d2f；191 unit/contract + device 2 通过。离线 ECharts 5.5.1 已 vendor 进 assets/，断网验证含 agent-browser 实渲染 5 张曲线；trend 跨 profile 默认拒绝+显式覆盖。已知：HTML 内嵌 echarts 使每 run 报告约 +1MB 磁盘；events 时间轴渲染留 07 接入）

- [ ] 断网环境打开 HTML 报告，图表正常渲染（无外部资源请求）
- [ ] JSON 与 HTML 数据内容一致（同一份聚合数据源生成）
- [ ] 连续两次采集后，trend 输出两条 run 的对比（device 或既有 runs/ 数据验证）

## Comments
