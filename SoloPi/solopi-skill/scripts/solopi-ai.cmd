@echo off
rem Windows wrapper for the SoloPi AI harness CLI (equivalent of the POSIX solopi-ai script).
rem Python resolution order: SOLOPI_PYTHON env var > python on PATH > py launcher.
rem Uses the project-bundled ADB only; never falls back to PATH.
rem Usage: solopi-ai.cmd [--serial <serial>] --pretty <command>
setlocal
set "PYTHONUTF8=1"
set "ADB=%~dp0..\..\platform-tools\adb.exe"
set "ENTRY=%~dp0harness\src\solopi_harness\solopi_ai.py"

if defined SOLOPI_PYTHON (
    "%SOLOPI_PYTHON%" "%ENTRY%" %*
    goto :eof
)
rem -c probe skips the Microsoft Store python.exe alias, which exists but cannot run scripts.
python -c "import sys" >nul 2>nul && (
    python "%ENTRY%" %*
    goto :eof
)
py -3 -c "import sys" >nul 2>nul && (
    py -3 "%ENTRY%" %*
    goto :eof
)
echo [solopi-ai] Python 3.9+ not found. Install Python, or set SOLOPI_PYTHON to its python.exe path.
exit /b 1
