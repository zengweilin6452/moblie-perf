@echo off
setlocal EnableDelayedExpansion
rem ============================================================
rem SoloPi self-built one-click build (JDK 11 + Gradle 6.1.1)
rem Output: dist\solopi\SoloPi-selfbuild-debug.apk
rem Prints a readable error and returns non-zero on failure.
rem ============================================================

set "ROOT=%~dp0"

rem ---- 1. Locate JDK 11: reuse existing JAVA_HOME, else local default ----
if "%JAVA_HOME%"=="" goto setdefault
if not exist "%JAVA_HOME%\bin\java.exe" goto setdefault
goto javahome-ok

:setdefault
set "JAVA_HOME=D:\Java\jdk-11.0.32.1+1"
if not exist "%JAVA_HOME%\bin\java.exe" (
    echo [build] ERROR: JDK 11 not found. Install Temurin 11 or set JAVA_HOME.
    exit /b 1
)
:javahome-ok
echo [build] JAVA_HOME=%JAVA_HOME%
"%JAVA_HOME%\bin\java" -version 2>&1 | findstr /i "version"

rem ---- 2. Ensure local.properties points to the Android SDK ----
set "APP_DIR=%ROOT%SoloPi\solopi-app"
if exist "%APP_DIR%\local.properties" goto sdk-ok
if "%ANDROID_HOME%"=="" (set "SDK_DIR=%LOCALAPPDATA%\Android\Sdk") else set "SDK_DIR=%ANDROID_HOME%"
if not exist "!SDK_DIR!\platform-tools" (
    echo [build] ERROR: Android SDK not found ^(missing !SDK_DIR!\platform-tools^).
    echo [build] Required SDK components: platforms;android-29 / build-tools;29.0.2 / ndk;21.1.6352462 / cmake;3.10.2
    exit /b 1
)
echo sdk.dir=!SDK_DIR!>"%APP_DIR%\local.properties"
echo [build] Wrote local.properties: sdk.dir=!SDK_DIR!
:sdk-ok

rem ---- 3. Build the debug variant ----
pushd "%APP_DIR%"
call "%APP_DIR%\gradlew.bat" assembleDebug --console=plain
set "RC=%ERRORLEVEL%"
popd
if not "%RC%"=="0" (
    echo.
    echo [build] ERROR: gradlew assembleDebug failed ^(exit code %RC%^).
    echo [build] First build downloads Gradle and dependencies; retry on network timeout.
    exit /b %RC%
)

rem ---- 4. Copy artifact to the fixed output dir and verify the version ----
set "OUT_DIR=%ROOT%dist\solopi"
if not exist "%OUT_DIR%" mkdir "%OUT_DIR%"
set "APK_SRC=%APP_DIR%\portal\build\outputs\apk\debug\portal-debug.apk"
if not exist "%APK_SRC%" (
    echo [build] ERROR: build artifact not found: %APK_SRC%
    exit /b 1
)
copy /y "%APK_SRC%" "%OUT_DIR%\SoloPi-selfbuild-debug.apk" >nul

set "AAPT=%LOCALAPPDATA%\Android\Sdk\build-tools\29.0.2\aapt.exe"
if exist "%AAPT%" (
    echo [build] Version check:
    "%AAPT%" dump badging "%OUT_DIR%\SoloPi-selfbuild-debug.apk" | findstr /b "package:"
)

echo.
echo [build] OK: %OUT_DIR%\SoloPi-selfbuild-debug.apk
endlocal & exit /b 0
