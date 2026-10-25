# 18: 解密档与接口级指标

**What to build:** 在工单 17 的代理编排上增加**解密档**：能解密 TLS 时产出接口级指标——`api_req_duration_ms`（按 URL 聚合的耗时）、`api_req_count`、`api_error_rate_pct`（≥400 或连接失败）、`api_payload_kb`；报告新增**接口瀑布图**（Top N 慢接口 + 按域名分组）。

关键设计是**先探测再决定，不硬试**：会话开始前观察首个 `CONNECT` 的 TLS 握手结果——
- 握手成功 → 解密档可用，采集接口级指标；
- 失败且错误为 `certificate unknown` → 判定 App 不信任代理 CA，本 run 的 `api_*` 全部 `not_tested`，**不重试、不降级成猜测数据**，事件写 `net_profile.decrypt_unavailable` + 原因。

实测背景（M6 方案 §2.4b）：目标 App targetSdk 36，Android 7+ 起 targetSdk ≥24 的 App 默认不信任用户 CA；装系统 CA 需 root 或可写 system 分区，本机 `ro.build.type=user`、无 Magisk。**故 BYD academy + 本机必然走降级分支**——这是正确行为而非缺陷。验收需另找满足前置条件的组合（模拟器 `-writable-system` 装系统 CA / debug 包信任用户 CA / root 设备）验证解密档有值。

**Blocked by:** 17

**Status:** ready-for-agent

- [ ] 信任探测三态（成功 / 证书不信任 / 其他失败）unit 覆盖，各自映射到明确结论与事件
- [ ] 证书不信任时不重试、不产出任何 `api_*` 数值；`not_tested` 原因含可执行的下一步（换 debug 包 / 换 root 设备）
- [ ] 4 个 `api_*` key 进字典；按 URL 聚合口径（同 URL 多次请求的统计方式）在模块文档写明
- [ ] 报告接口瀑布图：Top N 慢接口可看，域名分组正确
- [ ] 真机验证：BYD academy 上如实 `not_tested` 且原因准确；在满足前置条件的组合上 `api_*` 有值
- [ ] 解密档的流量不得污染既有 SoloPi 网络指标（两套数据独立入库、报告分栏）

## Comments
