@echo off
rem Stop the trading system gracefully, then start it again in the background.
rem Details: docs\ops_windows.md
setlocal
cd /d "%~dp0.."
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo Python venv not found: %CD%\%PY%
    set "RC=1"
    goto end
)
"%PY%" -m tradingsystem run --stop
if errorlevel 1 (
    echo Stop failed - not starting again. See logs\supervisor-ctl.jsonl
    set "RC=1"
    goto end
)
"%PY%" -m tradingsystem run all --detach
set "RC=%ERRORLEVEL%"
:end
if /i not "%~1"=="/nopause" pause
exit /b %RC%
