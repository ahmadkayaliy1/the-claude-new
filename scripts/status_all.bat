@echo off
rem Status of every pair's system (config "instances:"). Read-only. The all-pairs system: status.bat
setlocal
cd /d "%~dp0.."
set "PY=.venv\Scripts\python.exe"
set "RC=0"
if not exist "%PY%" (
    echo Python venv not found: %CD%\%PY%
    set "RC=1"
    goto end
)
for /f "usebackq delims=" %%P in (`%PY% -m tradingsystem config --instances`) do (
    echo ===== %%P
    "%PY%" -m tradingsystem run --status --instance %%P
    if errorlevel 1 set "RC=1"
)
:end
if /i not "%~1"=="/nopause" pause
exit /b %RC%
