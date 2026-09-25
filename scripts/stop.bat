@echo off
rem Stop the trading system gracefully (services flush and exit; up to ~90 s, then forced).
rem Also pauses autostart until start.bat is run. Double-click it, or run "stop.bat /nopause".
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
set "RC=%ERRORLEVEL%"
:end
if /i not "%~1"=="/nopause" pause
exit /b %RC%
