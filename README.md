# mobile_perf — App 性能测试组件

基于 [SoloPi](https://github.com/alipay/SoloPi)（蚂蚁集团，本仓库内含其完整快照与少量二开）二次开发的 **App 性能测试 Python 组件包**。设计目标：可嵌入自动化测试平台（当前宿主为 [moblie-auto](https://github.com/) 的 UI 自动化体系），对被测 App 做 CPU / FPS / 内存 / 网络 / 电量温度的五项性能采集、归档、基线门禁与趋势对比，全程可无人值守。

> 设计文档：[docs/设计方案-App性能测试组件.md](docs/设计方案-App性能测试组件.md)（v0.3，经两轮 20 项决策盘问定稿）｜术语：[GLOSSARY.md](GLOSSARY.md)｜架构决策：[docs/adr/](docs/adr/)

## 能力一览

- **采集闭环**：`mobile-perf collect` 一条命令完成 启动 SoloPi 采集 → 计时 → 停止 → 自动 pull → 行数校验 → 聚合入库 → 清理设备端；
- **无人值守**：SoloPi 告知弹窗"不再提示"开关（App 二开）+ adb 读写通道、tcpip 自愈、无障碍巡检、充电降级、时钟偏移校准、crash/ANR 监听入时间轴；
- **门禁**：judge 纯函数三态结论（pass / fail / not_tested），阈值由宿主显式传入（不入库），基线绑定设备档案（机型+OS 大版本）严格匹配；
- **补采**：PC 侧 dumpsys 轮询电池温度（GPU 视机型能力如实降级），与 SoloPi 指标、事件三源同时间轴同图；
- **报告**：每 run 双产物——单文件 HTML（内嵌离线 ECharts，断网可看）+ 同内容 JSON；
- **设备准入**：DeviceGate 短事务协议，与宿主 UI 自动化的整生命周期租约互不阻塞（录制期间不持锁）。

## 安装

```bash
# Python ≥3.10，零运行时依赖
pip install -e .
```

ADB 三级解析：显式参数 > `MOBILE_PERF_ADB`/`ADB` 环境变量 > 随包 `SoloPi/platform-tools`（37.0.1）。

## 快速开始

### CLI

```bash
mobile-perf doctor --serial <serial>                       # 巡检 + 自愈（幂等）
mobile-perf collect tv.danmaku.bili --items CPU,FPS,Memory --duration 180
mobile-perf report <runId> --baseline bili-home-v3 --threshold mem_pss_mb:lt:220:fail
mobile-perf trend tv.danmaku.bili --metric mem_pss_mb
```

### 库 API

```python
from mobile_perf import PerfKit, SessionSpec, JudgeSpec

kit = PerfKit(serial="749622c9")
kit.ensure_ready()                        # 巡检 + 自愈 + 充电/无障碍/前台检查

spec = SessionSpec(target="tv.danmaku.bili", items=["CPU", "FPS", "Memory"])
with kit.collect(spec, host_run_id="pytest-001") as s:
    s.mark("scroll_home_done")            # 打标与事件共用统一时间轴
report = s.finalize()                     # 自动 pull/校验/归档/聚合/入库

verdict = kit.judge(report, JudgeSpec(
    baseline="bili-home-v3",
    thresholds={"mem_pss_mb": ("lt", 220, "fail"), "fps_frame": ("gt", 50, "fail")}))
```

### pytest 钩子（对称 moblie-auto 的 `--mobile-evidence`）

```python
def test_home_scroll(device, perf):
    with perf.session("tv.danmaku.bili", items=["CPU", "FPS"]) as s:
        device.launch_app(...); device.swipe_up(5)
        s.mark("scroll_done")
    # 自动 finalize；基线可用 @pytest.mark.mobile_perf_baseline("name") 或 --mobile-perf-baseline
```

运行：`pytest --mobile-perf --mobile-perf-serial <serial>`。

## 嵌入宿主（moblie-auto / 测试平台）

依赖方向单向：**mobile_perf 不知道宿主的存在**。宿主注入 `adb 路径 / serial / host_run_id / DeviceGate`。moblie-auto 侧适配见 [docs/嵌入指南.md](docs/嵌入指南.md)；DeviceGate 为短事务协议（发起设备命令的短窗口持有、采集进行中不持锁），独立 CLI 模式按 moblie-auto 锁文件协议对等互操作。

## SoloPi App 二开（单活线）

本仓库对 SoloPi 的改动保持最小 diff（基线 tag `solopi-baseline`，可 rebase）：

1. FPS 告知弹窗"本机不再提示"（SP 持久化）+ config scheme 的 adb 读写通道；
2. CSV 落盘英文文件名开关（列名不动，翻译归指标字典）；
3. 版本自 1.0.3.1 起。

构建与切换：`build.cmd`（JDK 11）出 APK 至 `dist/solopi/`；`cutover.cmd` 一键切换（卸载官方版 → 装自编译 → 开关写入；卸载丢失设置与无障碍授权，一次性损失）。签名约束见 [docs/PATCH_SIGNING.md](docs/PATCH_SIGNING.md)。**自编译与官方版签名不同，不能覆盖安装。**

## 数据与存储

- SQLite `data/mobile_perf.db`（WAL + busy_timeout=5s）：devices / sessions / metrics / events / baselines / runs_artifacts；阈值不入库；
- 原始 CSV 落 `runs/<runId>/`，设备端 pull 行数校验通过后自动删除，本地保留最近 200 run / 30 天（可配置）；
- 指标命名 `<对象>_<指标>_<单位>`（如 `mem_pss_mb`、`fps_frame`），中文列名 → canonical key 的映射只存在于 `src/mobile_perf/metrics.py`。

## 开发

```bash
python -m pytest tests/unit tests/contract    # 不需要设备
python -m pytest -m device                    # 真机门控（无设备自动跳过，不计通过）
ruff check src tests
```

测试三段式：unit（不碰 adb）/ contract（gate 锁协议、Harness JSON 契约、指标字典映射）/ device（`-m device` 门控）。

## 已知边界

- GPU 利用率补采依赖机型（kgsl 需 root），不支持时如实降级 `not_tested`，不伪造数据；
- 充电状态下 Battery/温度项自动标注 `not_tested`（其余照采）；
- SoloPi 内部 adb 走手机 tcpip 5555，与 PC adb 相互独立；keeper 可自动恢复；
- App 执行 `am force-stop` 会清除无障碍授权（重启不清），需重新授权。

## 致谢与许可

- 采集内核基于蚂蚁集团开源的 [SoloPi](https://github.com/alipay/SoloPi)，其原始许可与版权归原作者所有；本项目二开部分（弹窗免重复、英文名开关、构建脚本、`mobile_perf` 组件包）遵循项目约定；
- 报告图表使用 [Apache ECharts](https://echarts.apache.org/)（Apache-2.0，已 vendor 至 `src/mobile_perf/assets/`）。
