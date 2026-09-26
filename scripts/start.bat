@echo off
rem Start the trading system in the background (hidden console): every service + the dashboard.
rem Also resumes autostart after stop.bat. Double-click it, or run "start.bat /nopause" from a script.
rem Details: docs\ops_windows.md
setlocal
cd /d "%~dp0.."
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo Python venv not found: %CD%\%PY%
    set "RC=1"
    goto end
)
"%PY%" -m tradingsystem run all --detach
set "RC=%ERRORLEVEL%"
:end
if /i not "%~1"=="/nopause" pause
exit /b %RC%
