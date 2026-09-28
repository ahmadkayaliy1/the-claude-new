@echo off
rem Register the operator tasks (monitor + daily/weekly Claude reviews + daily state backup + price recorder keep-alive;
rem Task Scheduler, your account, not elevated). -DryRun shows the plan, -Uninstall removes them.
rem See install_operator_tasks.ps1 / docs\operator_sessions.md / docs\ops_windows.md section 9.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_operator_tasks.ps1" %*
set "RC=%ERRORLEVEL%"
pause
exit /b %RC%
