@echo off
rem Quick environment check: bundled ADB, Python resolution, device doctor.
rem Usage: check-env.cmd
setlocal
echo === [1/3] Bundled ADB ===
if exist "%~dp0platform-tools\adb.exe" (
    "%~dp0platform-tools\adb.exe" devices
) else (
    echo FAIL: platform-tools\adb.exe not found.
    exit /b 1
)
echo === [2/3] Python + harness CLI ===
call "%~dp0solopi-skill\scripts\solopi-ai.cmd" --pretty actions | findstr /C:"totalDescriptors"
if errorlevel 1 (
    echo FAIL: harness CLI could not run. Install Python 3.9+ or set SOLOPI_PYTHON.
    exit /b 1
)
echo === [3/3] Device + SoloPi doctor ===
call "%~dp0solopi-skill\scripts\solopi-ai.cmd" --pretty doctor
