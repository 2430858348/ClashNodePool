@echo off
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"

if not exist "%PY%" goto :nosetup

rem 服务已经在跑：直接开面板，本窗口立刻关闭
"%PY%" "%~dp0probe.py" >nul 2>&1
if not errorlevel 1 goto :open

rem 没在跑：让 Python 自己以无窗口方式再起一份，本窗口随即关闭
"%PY%" -m app.main --daemon
if errorlevel 1 goto :timeout

set /a tries=0
:wait
ping -n 2 127.0.0.1 >nul
"%PY%" "%~dp0probe.py" >nul 2>&1
if not errorlevel 1 goto :open
set /a tries+=1
if %tries% geq 12 goto :timeout
goto :wait

:open
start "" http://127.0.0.1:8787
exit /b 0

:timeout
echo.
echo [ERROR] The service did not start within 12 seconds.
echo Details: data\daemon.log and data\server.log
echo Or run run-server.bat to watch it live.
echo.
pause
exit /b 1

:nosetup
echo.
echo [ERROR] Python environment not found:
echo   %~dp0.venv
echo Run start.bat once to set it up.
echo.
pause
exit /b 1
