@echo off
rem Stop every trading system: each pair's system, then the all-pairs one (if it runs). Pauses their autostart.
setlocal
cd /d "%~dp0.."
set "PY=.venv\Scripts\python.exe"
set "RC=0"
if not exist "%PY%" (
    echo Python venv not found: %CD%\%PY%
    set "RC=1"
    goto end
)
"%PY%" -m tradingsystem config --instances >nul
if errorlevel 1 (
    echo Could not read the configured pairs - fix config\config.yaml ^(the error is above^).
    set "RC=1"
    goto end
)
set "N=0"
for /f "usebackq delims=" %%P in (`%PY% -m tradingsystem config --instances`) do (
    set /a N+=1 >nul
    echo ===== %%P
    "%PY%" -m tradingsystem run --stop --instance %%P
    if errorlevel 1 set "RC=1"
)
if "%N%"=="0" (
    echo No pair is configured in config "instances:" - nothing done.
    set "RC=1"
)
echo ===== all-pairs system
"%PY%" -m tradingsystem run --stop
if errorlevel 1 set "RC=1"
:end
if /i not "%~1"=="/nopause" pause
exit /b %RC%
