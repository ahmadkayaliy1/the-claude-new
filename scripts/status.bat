@echo off
rem Show the supervisor, every service, the MT5 terminal, collector heartbeats and the dashboard URL. Read-only.
rem Details: docs\ops_windows.md
setlocal
cd /d "%~dp0.."
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo Python venv not found: %CD%\%PY%
    set "RC=1"
    goto end
)
"%PY%" -m tradingsystem run --status
set "RC=%ERRORLEVEL%"
:end
if /i not "%~1"=="/nopause" pause
exit /b %RC%
