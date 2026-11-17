@echo off
setlocal
cd /d "%~dp0"
title ClashNodePool - create desktop shortcut

echo ============================================
echo   ClashNodePool  -  create desktop shortcut
echo ============================================
echo.

if not exist "open-panel.bat" (
  echo [ERROR] open-panel.bat not found next to this script.
  echo         Put this file in the ClashNodePool folder.
  pause
  exit /b 1
)
if not exist "icon.ico" (
  echo [WARN] icon.ico not found, shortcut will use default icon.
)

set "VBS=%TEMP%\cnp_shortcut.vbs"

> "%VBS%" echo Set sh = CreateObject("WScript.Shell")
>> "%VBS%" echo desk = sh.SpecialFolders("Desktop")
>> "%VBS%" echo Set lnk = sh.CreateShortcut(desk ^& "\ClashNodePool.lnk")
>> "%VBS%" echo lnk.TargetPath = "%~dp0open-panel.bat"
>> "%VBS%" echo lnk.WorkingDirectory = "%~dp0"
>> "%VBS%" echo lnk.IconLocation = "%~dp0icon.ico,0"
>> "%VBS%" echo lnk.Description = "ClashNodePool - local node pool subscription"
>> "%VBS%" echo lnk.Save
>> "%VBS%" echo WScript.Echo "Desktop shortcut created."

cscript //nologo "%VBS%"
if errorlevel 1 (
  echo [ERROR] Failed to create shortcut.
  del "%VBS%" >nul 2>&1
  pause
  exit /b 1
)

del "%VBS%" >nul 2>&1
echo.
echo Done. You can now double-click the "ClashNodePool" icon on your desktop.
timeout /t 3 >nul
exit /b 0
