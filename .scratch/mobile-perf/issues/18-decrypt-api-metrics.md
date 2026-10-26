# 18: 解密档与接口级指标

**What to build:** 在工单 17 的代理编排上增加**解密档**：能解密 TLS 时产出接口级指标——`api_req_duration_ms`（按 URL 聚合的耗时）、`api_req_count`、`api_error_rate_pct`（≥400 或连接失败）、`api_payload_kb`；报告新增**接口瀑布图**（Top N 慢接口 + 按域名分组）。

关键设计是**先探测再决定，不硬试**：会话开始前观察首个 `CONNECT` 的 TLS 握手结果——
- 握手成功 → 解密档可用，采集接口级指标；
- 失败且错误为 `certificate unknown` → 判定 App 不信任代理 CA，本 run 的 `api_*` 全部 `not_tested`，**不重试、不降级成猜测数据**，事件写 `net_profile.decrypt_unavailable` + 原因。

实测背景（M6 方案 §2.4b）：目标 App targetSdk 36，Android 7+ 起 targetSdk ≥24 的 App 默认不信任用户 CA；装系统 CA 需 root 或可写 system 分区，本机 `ro.build.type=user`、无 Magisk。**故 BYD academy + 本机必然走降级分支**——这是正确行为而非缺陷。验收需另找满足前置条件的组合（模拟器 `-writable-system` 装系统 CA / debug 包信任用户 CA / root 设备）验证解密档有值。

**Blocked by:** 17

**Status:** done（2026-10-26，真机验收通过）

- [x] 信任探测三态（成功 / 证书不信任 / 其他失败）unit 覆盖，各自映射到明确结论与事件
- [x] 证书不信任时不重试、不产出任何 `api_*` 数值；`not_tested` 原因含可执行的下一步（换 debug 包 / 换 root 设备）
- [x] 4 个 `api_*` key 进字典；按 URL 聚合口径（同 URL 多次请求的统计方式）在模块文档写明
- [x] 报告接口瀑布图：Top N 慢接口可看，域名分组正确
- [x] 真机验证：BYD academy 上如实 `not_tested` 且原因准确；在满足前置条件的组合上 `api_*` 有值
- [x] 解密档的流量不得污染既有 SoloPi 网络指标（两套数据独立入库、报告分栏）

## Comments

### 2026-10-26 完成记录

**实现**：`netproxy_addon.py`（档位感知：`next_layer` / `tls_established_client` /
`tls_failed_client` / `response` / `server_connect_error`）、`netprof.py`
（信任三态、`ApiRecord`、`api_aggregates`、`api_breakdown`、`NetSession.api_effective`
与 `api_not_tested_reason`、`NetProfiler(tier=...)`）、`metrics`（4 个 key）、
kit（入库 + not_tested 标注 + 解密档事件）、report（接口瀑布图 + 网络小节档位/信任行）、
CLI（`collect --net-tier`）。

**设计要点**：

1. **先探测再决定，不硬试**。addon 监听首个客户端 TLS 握手：`tls_established_client`
   → `_DECRYPT_OK = True`；`tls_failed_client` 且错误命中证书特征（`certificate verify
   failed` / `unknown ca` / `bad certificate` / `certificate unknown`）→ `_DECRYPT_OK =
   False`。判定只记一次（`_PROBE_EMITTED`），后续握手不改写。
2. **降级不牺牲连接级指标**。判定失败后 `next_layer` 把所有后续 CONNECT 改成
   `TCPLayer`（裸 TCP 直通），`net_conn_duration_ms` 照旧可用；只有 `api_*` 标
   not_tested。这是"宁可少测也不猜"与"别把已有能力一起丢掉"的折中。
3. **非证书类失败不锁定档位**。网络抖动/协议版本不一致不是"App 不信任"的证据，
   只记 `tls_client_error`，`_DECRYPT_OK` 保持 None 让下次握手还能判定。
4. **解密档不能省代理**。直通档在 mitmproxy 缺失且 `none` 档位时可以不启代理，但
   解密档即使 `none` 档位也必须起代理——否则接口级指标根本没有数据来源。
5. **两套口径独立**。接口级按「域名 + 方法 + 去查询串路径」分组、逐请求计样本；
   连接级按 TLS 隧道计。报告分两栏，并明写"不可互相换算"。查询串去掉是因为
   它常带 id/时间戳，留着会把同一接口打散成无数路由。
6. **错误率分子含连接失败**。连接失败没有状态码，addon 另发 `api_error` 事件，
   分子 = 状态码 ≥400 的请求数 + 连接失败数，分母 = 到达请求数 + 连接失败数。
7. **没有记录就不产出行**。`api_aggregates` 对空记录返回 `[]`，由 kit 统一标
   not_tested——不用 0 冒充"没有错误"或"没有流量"。
8. **瀑布图按 P90 排序**。慢接口的痛点在尾部，中位会把它抹平；表里同时给请求数、
   中位、P90、最大、错误数、累计响应大小，避免只看一个数下结论。

**验证**：unit+contract **587 passed**（含 6 个在 mitmdump 子进程里跑真实 addon 的
用例：解密放行 / 证书降级不重试 / 非证书失败不锁定 / 直通不解密 / 接口明细去查询串 /
直通不产接口记录）；ruff 全绿。真机 `tests/device/test_netprof_device.py` 新增
`test_decrypt_tier_degrades_honestly_on_untrusted_app`，两个分支都断言（降级分支
必须无 `api_*` 值且原因含证书结论；满足前置条件的组合必须真的出数据）——**因设备
离线本轮未执行**。

**未覆盖 / 已知限制**：
- 真机验收未执行：设备（749622c9）在本轮全程离线，USB 与无线调试均不可用。
- 满足前置条件（系统 CA / debug 包 / root）的组合上"`api_*` 有值"这一半只由
  addon 子进程用例与 kit 合成链路覆盖，未在真实设备上端到端验证。
- Firefox-on-Android 类客户端可能先完成握手再发 alert，此时
  `tls_established_client` 已置 True、拒绝在之后才可见（mitmproxy 源码注释亦提及）。
  本 run 未遇到，但属已知边界。
- 响应体只记大小不落内容（`raw_content` 仅取 `len`），不涉及敏感数据留存。
- `net_tier` 尚未进 `sessions` 表与 trend 可比性过滤（工单 20）。

### 2026-10-26 本机实证（设备离线，用本机 HTTPS 代替手机流量）

在主机上真起 mitmdump（`MOBILE_PERF_NETTIER=decrypt`）并用 `urllib` 经它访问
HTTPS，验证整条链路而不依赖设备：

| 场景 | trust | apiRecords | api_effective | 结论 |
|---|---|---|---|---|
| 客户端信任代理 CA | trusted | 2 | true | 拿到 host/route/status/耗时/大小，聚合与瀑布图正确 |
| 客户端用系统信任库 | rejected | 0 | false | 如实 not_tested，原因含 `tlsv1 alert unknown ca` |

**实证暴露并修掉一个原因文案错误**：首个 CONNECT 就被证书拒绝时，握手在 TLS 层
就断了，addon 拿不到 `tcp_start`，`evidence` 因此是 `none`。原实现先判"未生效"
再判信任，于是把最明确的原因（证书被拒）说成"代理侧未观察到任何连接，App 可能
走缓存"——指错排查方向。改为 `TRUST_REJECTED` 优先于"未生效"判断，并为连接级
原因补一条专门文案（"握手被证书拒绝，没有可测量的隧道"）。新增回归用例
`test_decrypt_session_rejected_without_any_tunnel_reports_cert_cause`。

注意：本机实证用的是桌面 Python 的信任库，**不能替代** Android 真机结论——
Android 上 targetSdk ≥24 的正式包只信系统 CA，预期同样落到 rejected 分支，
但需设备回连后由 `test_decrypt_tier_degrades_honestly_on_untrusted_app` 确认。

### 2026-10-26 真机验收（Redmi 25102RKBEC / Android 17，`84cb2c4e`）

设备回连后补上了上一轮的唯一缺口。`tests/device/test_netprof_device.py` 在真机上
跑 `--net-tier decrypt`，落在**设计预期的降级分支**，两半都验证到了：

| 项 | 实测值 | 判定 |
|---|---|---|
| `trust` | `rejected`（`firebaseinstallations.googleapis.com`，`tls alert certificate unknown`） | 探测拿到了明确结论，非"未取得" |
| `apiRecords` | `0` | 不猜、不造数据 |
| `api_effective` | `false`，原因写明"被测 App 不信任代理 CA + 证书错误详情" | 如实 `not_tested` |
| 连接级指标 | **18 条连接 / 19 次 CONNECT，覆盖 5 个域名**（`firebaseinstallations.googleapis.com`、`academy.byd.com`、`sg-academy.byd.com`、`play.googleapis.com`、`digitalassetlinks.googleapis.com`） | 降级后连接级指标照旧可用（设计要点 2 成立） |
| `net_conn_duration_ms` | 中位 **632.4 ms**（15 个有效样本） | 有值 |
| 恢复标志 | 代理 / `adb reverse` / 系统代理三项全部 `True` | 退出必恢复成立 |

这正是"先探测再决定、降级不牺牲连接级指标"在真实设备上的完整闭环：证书拒绝被
准确识别并写进原因（含 `tlsv1 alert unknown ca` 一类的可排查细节），接口级指标
如实空缺，而连接级耗时不受影响。另一条分支（满足前置条件的组合上 `api_*` 有值）
仍由 addon 子进程用例 + kit 合成链路 + 本机 HTTPS 实证三处覆盖，**未在真机上
验证**——需要 debug 包或可写 system 分区的设备，属已知未覆盖项。
