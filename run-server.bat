@echo off
cd /d "%~dp0"
title ClashNodePool - debug console (close this window to stop)
echo ================================================
echo   ClashNodePool server  -  DEBUG MODE
echo   http://127.0.0.1:8787
echo.
echo   This mode does NOT stop when you close the page.
echo   Close this window to stop the service.
echo ================================================
echo.
".venv\Scripts\python.exe" -m app.main
if errorlevel 1 (
  echo.
  echo [ERROR] Server exited unexpectedly. See the message above.
  pause >nul
)
