@echo off
setlocal EnableDelayedExpansion
rem ============================================================
rem SoloPi single-line cutover (ADR-0003, one-time operation)
rem
rem !! Uninstalling the official build loses all SoloPi settings
rem    and accessibility grant (ADR-0003 one-time loss).
rem !! The self-built APK has a different signature than the
rem    official release: uninstall-then-install is the only path.
rem !! Run only in the ticket-08 M2 integration window, with
rem    explicit human confirmation.
rem ============================================================

set "ROOT=%~dp0"
set "APK=%ROOT%dist\solopi\SoloPi-selfbuild-debug.apk"

rem ---- 0. Precheck ----
if not exist "%APK%" (
    echo [cutover] ERROR: self-built artifact not found: %APK%
    echo [cutover] Run build.cmd first.
    exit /b 1
)

set "ADB=adb"
if exist "%ROOT%SoloPi\platform-tools\adb.exe" set "ADB=%ROOT%SoloPi\platform-tools\adb.exe"
"%ADB%" get-state >nul 2>&1
if errorlevel 1 (
    echo [cutover] ERROR: no device connected. Connect the device and retry.
    exit /b 1
)

echo ============================================================
echo  About to run (IRREVERSIBLE):
echo   1. Uninstall official SoloPi (com.alipay.hulu)
echo      - loses settings and accessibility grant
echo   2. Install self-built APK: %APK%
echo   3. Guide: accessibility grant + dialog suppress switch + doctor
echo ============================================================
choice /c YN /m "Confirm cutover"
if errorlevel 2 (
    echo [cutover] Cancelled.
    exit /b 0
)

rem ---- 1. Uninstall the official build (ADR-0003 one-time loss) ----
echo [cutover] Uninstalling official com.alipay.hulu ...
"%ADB%" uninstall com.alipay.hulu

rem ---- 2. Install the self-built APK ----
echo [cutover] Installing self-built APK ...
"%ADB%" install -r "%APK%"
if errorlevel 1 (
    echo [cutover] ERROR: install failed, see adb output above.
    exit /b 1
)

rem ---- 3. Dialog suppress switch: write via config scheme, read back ----
echo [cutover] Enabling KEY_SUPPRESS_INFO_DIALOG ...
"%ADB%" shell "am start -n com.alipay.hulu/com.alipay.hulu.common.scheme.AdbSchemeActivity -d 'solopi://startaction/config?action=set&key=KEY_SUPPRESS_INFO_DIALOG&value=true'"
echo [cutover] Reading back switch state (after adb forward) ...
"%ADB%" forward tcp:23342 tcp:23342
curl -s "http://127.0.0.1:23342/scheme/config?action=get&key=KEY_SUPPRESS_INFO_DIALOG"

rem ---- 4. Remaining manual steps ----
echo.
echo [cutover] Remaining manual steps:
echo   1. Re-grant the accessibility permission on the device (lost with uninstall).
echo   2. Run mobile-perf doctor: python -m mobile_perf doctor
echo   3. To disable the suppress switch:
echo      adb shell "am start -n com.alipay.hulu/com.alipay.hulu.common.scheme.AdbSchemeActivity -d 'solopi://startaction/config?action=set&key=KEY_SUPPRESS_INFO_DIALOG&value=false'"
echo.
echo [cutover] Cutover done.
endlocal & exit /b 0
