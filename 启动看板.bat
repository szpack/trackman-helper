@echo off
setlocal
chcp 65001 >nul
title TrackMan Live Dashboard
cd /d "%~dp0"

rem keep the browser away from any system proxy
set "PYTHONIOENCODING=utf-8"
set "no_proxy=127.0.0.1,localhost"
set "NO_PROXY=127.0.0.1,localhost"
set "ARGS=%*"

rem ---------------- locate python ----------------
rem 顺序有讲究：球杆要读 TPS 的 LocalDB（pyodbc），Normalize 要读 TPS 按钮底色
rem （uiautomation）。这两个是可选依赖，不是每个 Python 都装了 —— 所以先试外部装的，
rem 最后才用自带的。都装不上也能跑，看板会自己再找一个装了的解释器，只是多绕一层。
set "PY="

py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=py -3"

if not defined PY (
    python -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PY=python"
)

if not defined PY (
    for /d %%D in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do (
        if exist "%%D\python.exe" if not defined PY set "PY=%%D\python.exe"
    )
)

if not defined PY (
    if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" (
        set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
    )
)

if not defined PY goto nopython

rem ---------------- is TPS running? ----------------
netstat -ano | findstr ":21581" | findstr "LISTENING" >nul 2>nul
if not errorlevel 1 goto launch
if not "%ARGS%"=="" goto launch

echo.
echo   TrackMan Performance Studio is NOT running.
echo   ^(port 21581 is idle^)
echo.
choice /c YN /t 8 /d Y /n /m "  Show DEMO data instead? [Y/N] "
if errorlevel 2 goto launch
set "ARGS=--demo"

:launch
echo.
echo   Dashboard  -^>  http://127.0.0.1:8770
echo   Keep this window open. Close it or press Ctrl+C to stop.
echo.
%PY% -u "%~dp0trackman_dashboard.py" --open %ARGS%
echo.
echo   Dashboard stopped.
pause
exit /b 0

:nopython
echo.
echo   [ERROR] Python not found.
echo.
echo   Install Python 3.9 or newer:
echo       https://www.python.org/downloads/
echo   During setup, tick "Add python.exe to PATH".
echo.
pause
exit /b 1
