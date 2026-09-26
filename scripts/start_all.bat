@echo off
rem Start the independent system of every configured pair (config "instances:", D-042), one after the other.
rem One pair only: start.bat BTCUSDT. Refused while the all-pairs system runs (stop it first with stop.bat).
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
    "%PY%" -m tradingsystem run all --detach --instance %%P
    if errorlevel 1 set "RC=1"
)
:end
if /i not "%~1"=="/nopause" pause
exit /b %RC%
