@echo off
rem Register the autostart tasks (Task Scheduler, your account, not elevated). See install_autostart.ps1.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_autostart.ps1" %*
set "RC=%ERRORLEVEL%"
pause
exit /b %RC%
