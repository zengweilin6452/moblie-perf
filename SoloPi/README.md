# SoloPi 完整版 · App 源码 + AI Harness + Windows 工具链

蚂蚁开源 [alipay/SoloPi](https://github.com/alipay/SoloPi) 的完整工作副本，配置为 **Windows 原生可直接使用**，同时支持两类工作：

- **日常移动端性能测试**：开箱即用（自带 ADB、手机端 APK、自检脚本），零 pip 依赖；
- **App 二次开发**：含 `solopi-app` 全部 Android 源码与构建脚本，可改采集逻辑后自行编译 APK。

于 2026-10-25 在 Windows 11 + 小米 2206123SC（MIUI）真机完整验证：设备接入、启动耗时、五项性能采集（CPU/FPS/内存/网络/电池）全链路跑通。

## 目录结构

```
SoloPi/
├── README.md            # 本文档（合并版）
├── check-env.cmd        # ★ 一键自检：ADB/Python/设备/doctor
├── platform-tools/      # ★ 自带 Google 官方 ADB v37.0.1（不走 PATH）
├── apk/
│   ├── SoloPi_v1.0.3.apk    # ★ 手机端 App（免下载，含 SHA256 校验）
│   └── SHA256SUMS.txt
├── tools/
│   └── perf-summary.py  # ★ perf-analyze 结果压成一张指标表
├── solopi-app/          # Android App 全部源码（二开用，见模式 B）
│   ├── gradlew + gradlew.bat   # bat 为后补的官方 Gradle v6.1.1 Windows 启动器
│   ├── app/ shared/ common/ AdbLib/ agentmodel/ portal 等模块
│   └── README.md        # App 侧详细文档（上游）
├── solopi-skill/        # AI Harness（Agent 驱动的控制面）
│   ├── SKILL.md         # Agent 使用规则
│   ├── scripts/solopi-ai.cmd   # ★ Windows 入口：动态解析 Python + 固定项目内 ADB
│   ├── scripts/harness/ # 纯 Python 标准库实现（零第三方依赖）
│   └── references/      # CLI 参考、性能测试、协议、故障处理等文档
├── arm64-v8a.json 等    # App 插件 ABI 兼容清单（编译/插件机制用）
└── LICENSE 等           # Apache-2.0 与上游说明文档
```

带 ★ 的为本包新增；其余为上游原样。

## 模式 A：日常性能测试（开箱即用）

### 环境要求

| 项 | 要求 |
|---|---|
| Python | 3.9+（解析顺序：`SOLOPI_PYTHON` 环境变量 > PATH 的 `python` > `py -3`） |
| ADB | 无需安装（用包内 `platform-tools\adb.exe`） |
| 手机 | Android 真机，已开 USB 调试；无需 root |
| 第三方依赖 | 无（Harness 纯标准库） |

### 新电脑五分钟上手

1. 拷贝整个 `SoloPi/` 目录到目标机器，装 Python 3.9+；
2. 手机开 USB 调试连电脑，跑自检：
   ```bat
   check-env.cmd
   ```
   预期：设备列出、`totalDescriptors: 72`、doctor 返回 JSON；
3. 装手机端 App：
   ```bat
   platform-tools\adb.exe install -r apk\SoloPi_v1.0.3.apk
   ```
4. 一次性手机设置（有坑，见踩坑实录 §二）：
   - `platform-tools\adb.exe tcpip 5555`（手机重启后需重跑）
   - 开无障碍：设置 → 更多设置 → 无障碍 → 已下载的应用 → SoloPi
   - 开发者选项 → GPU呈现模式分析 → "在 adb shell dumpsys gfxinfo 中"（FPS 需要）
   - SoloPi 省电策略 → **无限制**（否则 MIUI 反复挂起无障碍）
5. 建立内部连接并复验：
   ```bat
   solopi-skill\scripts\solopi-ai.cmd --pretty adb-connect
   solopi-skill\scripts\solopi-ai.cmd --pretty doctor
   ```
   预期 `missingPermissions: []`。

### 工作流 A1：性能指标采集

```bat
:: 1. 列可测应用
solopi-skill\scripts\solopi-ai.cmd --pretty apps

:: 2. 目标应用切前台并开始采集（B站示例）
platform-tools\adb.exe shell monkey -p tv.danmaku.bili -c android.intent.category.LAUNCHER 1
solopi-skill\scripts\solopi-ai.cmd --pretty perf-start --target-package tv.danmaku.bili --items CPU,FPS,Memory,Network,Battery

:: ⚠ items 含 FPS 时手机会弹"权限申请(1/3)"告知框，点"我知道了"才开始记录

:: 3. 查询（记下 sessionId）
solopi-skill\scripts\solopi-ai.cmd --pretty perf-status --session-id <sessionId>

:: 4. 停止落盘，返回 /sdcard/solopi/records/performance-<sessionId>
solopi-skill\scripts\solopi-ai.cmd --pretty perf-stop --session-id <sessionId>

:: 5. 拉回本地（Git Bash 下加 MSYS_NO_PATHCONV=1 前缀）
platform-tools\adb.exe pull /sdcard/solopi/records/performance-<sessionId> ..\perf-output\

:: 6. 统计（描述性统计，不判达标）
solopi-skill\scripts\solopi-ai.cmd --pretty perf-analyze --input <本地CSV目录绝对路径>
python tools\perf-summary.py <perf_analysis.json 路径>
```

### 工作流 A2：启动耗时

```bat
solopi-skill\scripts\solopi-ai.cmd --pretty startup-time --target-package tv.danmaku.bili --mode cold --iterations 5 --interval 1
```

`am start -W` 口径（TotalTime/WaitTime，含逐轮字段校验与 min/median/mean/max/p90）。与录屏差分的"视觉完成时间"是两种口径，不能混用。

### 工作流 A3：通用排查

```bat
solopi-skill\scripts\solopi-ai.cmd --pretty doctor
platform-tools\adb.exe shell "dumpsys window | grep mCurrentFocus"    :: 看手机前台
platform-tools\adb.exe exec-out screencap -p > screen.png             :: 截屏定位弹窗
```

### 命令速查

| 命令 | 用途 | 需要设备 |
|---|---|---|
| `--pretty actions` | 66 枚举动作 + 6 Provider 动作 | 否 |
| `--pretty doctor` / `adb-connect` | 总检 / 内部连接 | 是 |
| `--pretty apps` / `perf-list` | 列应用 / 列指标 | 是 |
| `--pretty perf-start/status/stop` | 性能采集会话 | 是 |
| `--pretty perf-analyze --input <dir>` | CSV 描述性统计 | 否 |
| `--pretty startup-time` | 冷/暖启动耗时 | 仅 ADB |
| `--pretty stress-start/status/stop` | CPU/内存加压 | 是 |
| `--pretty screenshot` / `logs` | 截图 / 日志 | 仅 ADB |

更多（录制回放、动态 Agent、验证工程、托管执行、端侧模型）见 `solopi-skill/SKILL.md` 与 `solopi-skill/references/`。

## 模式 B：App 二次开发

适用：加自定义性能指标、改采集行为、适配特殊机型、改协议。

### 构建环境

| 项 | 要求 | 本机现状（2026-10-25） |
|---|---|---|
| JDK | 11 | ❌ 未安装（装后设 `JAVA_HOME`） |
| Android SDK | 装好并设 `ANDROID_HOME`，含 platform-tools/build-tools | ⚠ platform-tools 已有，SDK 整体未装 |
| Gradle | 6.1.1（wrapper 自动下载） | wrapper 就绪，`gradlew.bat` 为后补的官方 v6.1.1 脚本 |
| Android Studio | 4.0+ 推荐（直接 Open `solopi-app/` 目录导入） | 可选 |

> `gradlew.bat` 取自 gradle/gradle 官方仓库 v6.1.1 标签，本机无 JDK 暂未实测；首选方式是 Android Studio 导入构建。

### 构建 → 安装 → 验证 闭环

```bat
cd SoloPi

:: 1. 跑 App 单测 + 打 debug 包（与上游 README 相同的模块目标）
solopi-app\gradlew.bat -p solopi-app :app:testDebugUnitTest :agentmodel:testDebugUnitTest :portal:assembleDebug

:: 2. 安装到真机
platform-tools\adb.exe install -r solopi-app\portal\build\outputs\apk\debug\portal-debug.apk

:: 3. Harness 侧复验（协议兼容性 + 全链路）
check-env.cmd
solopi-skill\scripts\solopi-ai.cmd --pretty doctor
```

改完源码重复此闭环。若改动了设备控制协议（`solopi-app/app/src/main/java/com/alipay/hulu/scheme/` 下的 Resolver），同步检查 `doctor` 的 `protocolVersion` 与 Harness 是否兼容，并跑 Harness 单测：

```bat
python -m unittest discover -s solopi-skill\scripts\harness\tests
```

### 源码导读（改哪里）

- `solopi-app/app/`：业务逻辑与 scheme 协议入口（`scheme/HarnessSchemeResolver.java` = doctor/health/perf 的 App 侧实现；`scheme/PerformanceSchemeResolver.java` = 性能会话状态机）；
- `solopi-app/shared/`：核心能力——`display/items/` 下是**性能指标实现**（`FpsTools.java`/`CpuTools` 等，`@DisplayItem` 注解声明指标与权限，加指标从这里入手）、`event/` 事件监控、`node/` 页面节点；
- `solopi-app/common/`：框架能力（ADB 包装、权限 `PermissionUtil.java`、`PermissionDialogActivity`）；
- `solopi-app/agentmodel/`：动态 Agent 执行内核；
- `solopi-app/AdbLib/`：App 内嵌 adb 协议库（内部 adb 连接）；
- 仓库根 `arm64-v8a.json` 等：插件 ABI 兼容清单，涉及插件机制改动时注意 `PATCH_SIGNING.md` 的签名校验要求。

### 与上游同步

本目录是上游 master 的 tarball 快照（2026-10 下旬）+ 本包增强。更新上游时：下载新 tarball → 覆盖 `solopi-app/`、`solopi-skill/`、根 json 与文档 → **保留**本包的 `check-env.cmd`、`platform-tools/`、`apk/`、`tools/`、`solopi-skill/scripts/solopi-ai.cmd`、`solopi-app/gradlew.bat` 与本 README。

---

## 踩坑实录与经验教训（实测，2026-10-25）

### 一、Windows 兼容篇

1. **上游只声明 macOS/Linux，Windows 能跑吗？** 能，零源码修改。入口是两层 POSIX sh 包装，最终就一句 `python3 src/solopi_harness/solopi_ai.py`，`solopi-ai.cmd` 即等价包装。源码无 SIGKILL/fcntl/fork/shell=True，纯标准库。
2. **官方单测 446 个中 15 个失败的归因**：9 个 symlink（WinError 1314，需开发者模式）、2 个 exec sh 脚本（WinError 193）、1 个 POSIX 权限位（NTFS 不支持 0o700）、3 个 manifest version 断言（上游自带 bug，Linux 同挂）。全部非功能缺陷。**教训：测试挂了先分类归因，别急着改源码。**
3. **控制台中文乱码**：GBK 所致，包装脚本 `set PYTHONUTF8=1` 解决。
4. **Git Bash 路径陷阱**：`/tmp` 是 MSYS 虚拟路径（Windows 原生 Python 看不到）；`adb shell` 里的 `/sdcard` 会被转义，加 `MSYS_NO_PATHCONV=1`。
5. **别把本机绝对路径写进脚本**：包装脚本初版写死 python.exe 路径，换机即废。改为动态解析，并用 `python -c "import sys"` 实测探测（`where python` 会命中 Microsoft Store 假命令）。**写包装脚本永远问：换台机器还能跑吗？**

### 二、手机端 MIUI 篇（坑最多）

6. **无障碍"假开启"（会复发）**：开关是开的、doctor 却报 false → 系统启用列表有、`Bound services:{}` 空，服务实际没绑定。SoloPi 判定的是进程内服务实例，不查设置项。诱因：adbd 重启（`adb tcpip`）或省电策略。修复：无障碍先关再开；根治：省电策略"无限制"。诊断：`adb shell dumpsys accessibility | grep -E "Bound|Enabled"`——**Enabled 有、Bound 空 = 假开启**。
7. **内部 adb 前置**：`adb-connect` 失败提示 RSA/地址不可连，九成是没跑 `adb tcpip 5555`（官方首次设置步骤；CLI 按安全设计不代跑）。**手机重启后失效要重跑**。
8. **FPS 隐藏前置**：需开发者选项"GPU呈现模式分析"（App 靠 `dumpsys gfxinfo` 取帧数据）；且**每次**含 FPS 的 `perf-start` 都弹告知框，必须点"我知道了"才会 `starting → recording`，不点则 start 超时、stop 报 "cancellation is waiting for the permission request"。
9. **权限会话卡住定位三板斧**：`perf-status` 看卡在哪 → `dumpsys window | grep mCurrentFocus` 看前台（`PermissionDialogActivity` = 有弹窗等你）→ `screencap` 截屏看内容。
10. **MIUI 系统应用测不了冷启动**：权限弹窗抢占前台，Harness 检测组件不符**如实判失败**（不编数据）——特性不是 bug，换第三方应用。
11. **采集时目标应用要在前台**：FPS 依赖前台渲染；中途去设置页会产生"后台期"数据，分析时剔除或重采。

### 三、网络与工具链篇

12. **GitHub 访问**：`git clone` 直连常失败，但 `api.github.com`、`codeload.github.com`（tarball）、`raw.githubusercontent.com`、`gh` CLI 往往可用。本包已内置 APK + ADB，日常不再依赖 GitHub。
13. **adb 不在 PATH 不是问题**：包装脚本固定包内 adb；手动调用用 `--adb <路径>` 或 `ADB` 环境变量。
14. **`perf-analyze` 边界**：只做描述性统计、明确不判达标；要基线判定需自加阈值契约或扩展 `tools/perf-summary.py`。

### 四、设计观察（为什么适合接 AI Agent）

- **三态结果**（passed/failed/not_tested）：无法可靠观察返回 not_tested，不硬判；
- **Result Judge 独立裁决**：Agent 只能提议动作、无权判通过——防大模型"自信地胡说"；
- **证据链**：结论必须引用本次执行的检查点/回执/截图，不跨执行拼接；MIUI 抢前台、Android 16 缺 ThisTime 都如实失败/null；
- **动作安全边界**：无任意 Shell/Scheme/HTTP 后门，高风险操作需显式 `--confirm-high-risk`。

**教训：给 Agent 配工具，优先"判定权与执行权分离 + 证据可核"的设计，而不是把 adb shell 全权交给 Agent。**

---

## 本机验证记录（供对照）

- 环境：Windows 11（10.0.26200）/ Python 3.12 & 3.13 / ADB 37.0.1 / 小米 2206123SC（mayfly，MIUI）/ SoloPi App v1.0.3（SHA256: d1486161…0e4acf）
- 启动耗时（B站冷启动 ×3）：TotalTime 中位数 643ms、均值 744.7ms、p90 952ms
- 性能采集（B站 171.7s，80 采样）：PSS 中位 177.8MB（max 226.0）；进程 CPU 中位 2.6%（max 22.4%）；帧率中位 107fps；最长帧延迟中位 25ms（max 62ms）
- Harness 单测：446 个，431 通过 / 1 跳过 / 15 失败（全部为环境差异或上游 bug，见 §一.2）
- App 构建：**未验证**（本机无 JDK；装好 JDK 11 后按模式 B 闭环走一遍即可）

## 迁移到其他电脑 · 检查清单

- [ ] 整个 `SoloPi/` 目录已拷贝
- [ ] Python 3.9+（或 `SOLOPI_PYTHON`）
- [ ] `check-env.cmd` 三步全过
- [ ] 手机装 APK → `adb tcpip 5555` → 无障碍开启且 doctor 报 true → 省电无限制 → GPU呈现模式分析（要 FPS）
- [ ] `adb-connect` 返回 connected: true
- [ ] （仅二开需要）JDK 11 + `JAVA_HOME`、Android SDK + `ANDROID_HOME`、Android Studio 导入 `solopi-app/`

## 来源与许可

- 上游：https://github.com/alipay/SoloPi （master tarball，2026-10 下旬；App 为 v1.0.3 Release）
- 许可：Apache License 2.0；`solopi-app/`、`solopi-skill/` 为上游原样代码
- 本包新增：`check-env.cmd`、`solopi-skill/scripts/solopi-ai.cmd`、`tools/perf-summary.py`、`apk/`、`platform-tools/`、`solopi-app/gradlew.bat`（取自 gradle/gradle v6.1.1 官方标签）、本 README
