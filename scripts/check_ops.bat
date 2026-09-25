@echo off
rem Read-only check of the Windows settings the trading system needs (sleep, lid, Wi-Fi, time sync, autostart).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0check_ops.ps1" %*
set "RC=%ERRORLEVEL%"
pause
exit /b %RC%
