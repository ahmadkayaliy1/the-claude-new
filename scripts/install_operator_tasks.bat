@echo off
rem Register the Phase 4 operator tasks (monitor + daily/weekly Claude reviews; Task Scheduler, your account, not
rem elevated). -DryRun shows the plan, -Uninstall removes them. See install_operator_tasks.ps1 / docs\operator_sessions.md.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_operator_tasks.ps1" %*
set "RC=%ERRORLEVEL%"
pause
exit /b %RC%
