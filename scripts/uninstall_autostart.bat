@echo off
rem Remove the autostart tasks. See uninstall_autostart.ps1.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall_autostart.ps1" %*
set "RC=%ERRORLEVEL%"
pause
exit /b %RC%
