# PATCH_SIGNING：自编译 APK 签名约束与构建说明

适用范围：SoloPi App 自编译单活线（ADR-0003）。签名约束与构建流程随本文件维护；动态插件（patch）签名契约见 [`SoloPi/PATCH_SIGNING.md`](../SoloPi/PATCH_SIGNING.md)，两者互不替代。

## 1. APK 签名约束（ADR-0003 单活线）

- 自编译 APK 使用本地 debug keystore 签名，与官方 v1.0.3 发布签名不同，**无法覆盖安装**官方版；切换只能"卸载官方版 → 安装自编译版"（`cutover.cmd`）。
- 卸载丢失 SoloPi 设置与无障碍授权，属**一次性损失**；此后所有设备只装自编译版，不再与官方版共存。
- 版本号自 **1.0.3.1** 起（`appVersionCode 39` / `appVersionName 1.0.3.1`，定义在 `SoloPi/solopi-app/build.gradle`）；debug 变体不再追加时间戳后缀，保证版本号可判定。
- 自编译签名私钥即本机 Android debug keystore（`%USERPROFILE%\.android\debug.keystore`），不进仓库、不入补丁包；如需发布正式签名，另行引入独立 keystore 并同步更新本文件。
- 插件（patch）签名约束不受本次切换影响：`desc.json` + `manifest.sig`（SHA256withRSA，pin 公钥 `solopi-patch-2026-v1`）的验证契约、私钥"不入库/不外传/需异地备份"的要求，继续以 `SoloPi/PATCH_SIGNING.md` 为准。

## 2. 构建说明

- 一键构建：仓库根执行 `build.cmd`（自动定位 JDK 11（默认 `D:\Java\jdk-11.0.32.1+1`）、确保 `local.properties`、跑 `gradlew assembleDebug`、复制产物并核对版本号）。
- 构建链：Gradle 6.1.1 + AGP 4.0.2，JDK 11；不升级 Gradle/AGP。
- Android SDK 需要组件：`platforms;android-29`、`build-tools;29.0.2`、NDK `21.1.6352462`（`shared` 模块含 native 代码）。
- 产物：`dist/solopi/SoloPi-selfbuild-debug.apk`（`dist/` 不入库）。
- 版本核对：`aapt dump badging dist/solopi/SoloPi-selfbuild-debug.apk` 应显示 `versionCode='39' versionName='1.0.3.1'`。

## 3. 切换说明

- 一键切换：仓库根执行 `cutover.cmd`（卸载官方版 → 安装自编译版 → 弹窗开关写入/读回 → doctor 与无障碍授权引导）。**仅票 08 的 M2 集成窗口执行，需人工确认。**
- 弹窗免重复开关（`KEY_SUPPRESS_INFO_DIALOG`）读写通道：
  - 写（adb，DUMP 保护入口）：
    `adb shell "am start -n com.alipay.hulu/com.alipay.hulu.common.scheme.AdbSchemeActivity -d 'solopi://startaction/config?action=set&key=KEY_SUPPRESS_INFO_DIALOG&value=true'"`
  - 读（本机控制面，默认端口 23342）：
    `adb forward tcp:23342 tcp:23342 && curl "http://127.0.0.1:23342/scheme/config?action=get&key=KEY_SUPPRESS_INFO_DIALOG"`
- 性能 CSV 英文文件名开关（`KEY_CSV_ENGLISH_NAME`）同一通道读写；开启后文件名为 ASCII（列名保持中文，由指标字典翻译）。文件名英文前缀映射表在 `SoloPi/solopi-app/app/src/main/java/com/alipay/hulu/util/RecordUtil.java`，与 `src/mobile_perf/metrics.py` 指标字典的双端对应属联动契约：改动任一端须同步另一端并回归指标字典契约测试。
