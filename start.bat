@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
chcp 65001 >nul 2>&1
title ClashNodePool - first time setup

echo ============================================
echo   ClashNodePool  -  local Clash subscription
echo ============================================
echo.

if exist ".venv\Scripts\python.exe" goto :installdeps

set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY (
  where py >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
  if exist "C:\Users\%USERNAME%\.workbuddy\binaries\python\versions\3.13.12\python.exe" (
    set "PY=C:\Users\%USERNAME%\.workbuddy\binaries\python\versions\3.13.12\python.exe"
  )
)
if not defined PY (
  echo [ERROR] Python 3 not found.
  echo         Install Python 3.10+ and make sure "python" works in cmd.
  echo.
  pause
  exit /b 1
)

echo [1/3] Creating virtual environment ...
%PY% -m venv .venv
if errorlevel 1 (
  echo [ERROR] Failed to create venv.
  pause
  exit /b 1
)

:installdeps
echo [2/3] Installing dependencies ...
rem venv 里可能没有 pip（部分精简版 Python 如此），先补上
".venv\Scripts\python.exe" -m ensurepip --upgrade --default-pip >nul 2>&1
".venv\Scripts\python.exe" -m pip install --upgrade pip -q --timeout 120 --retries 10
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt --timeout 120 --retries 10
if errorlevel 1 (
  echo [WARN] Default PyPI failed, trying China mirror ...
  ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt --timeout 120 --retries 10 -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
)
if errorlevel 1 (
  echo [ERROR] pip install failed. Check your network / proxy settings.
  pause
  exit /b 1
)

echo [3/3] Starting at http://127.0.0.1:8787
echo       The service runs in the background (no console window).
echo       It stops automatically when you close the panel page.
echo.

set "PROBE=import socket,sys;s=socket.socket();s.settimeout(1);sys.exit(0 if s.connect_ex(('127.0.0.1',8787))==0 else 1)"
".venv\Scripts\python.exe" -c "%PROBE%" >nul 2>&1
if not errorlevel 1 goto :open

set "NP_AUTOSTOP=1"
start "" ".venv\Scripts\pythonw.exe" -m app.main

set /a tries=0
:wait
ping -n 2 127.0.0.1 >nul
".venv\Scripts\python.exe" -c "%PROBE%" >nul 2>&1
if not errorlevel 1 goto :open
set /a tries+=1
if %tries% geq 12 goto :timeout
goto :wait

:open
start "" http://127.0.0.1:8787
echo Done. This window will close in a moment.
timeout /t 2 >nul
exit /b 0

:timeout
echo [ERROR] Service did not start. Run run-server.bat to see the details.
pause
exit /b 1
