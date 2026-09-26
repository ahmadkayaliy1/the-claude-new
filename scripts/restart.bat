@echo off
rem Stop a trading system gracefully, then start it again in the background.
rem   restart.bat          the all-pairs system        restart.bat BTCUSDT the system of that pair
rem Details: docs\ops_windows.md
setlocal
cd /d "%~dp0.."
set "PY=.venv\Scripts\python.exe"
set "PAIR="
set "INST="
set "NOPAUSE="
:args
if "%~1"=="" goto run
if /i "%~1"=="/nopause" (set "NOPAUSE=1") else (set "PAIR=%~1")
shift
goto args
:run
if defined PAIR set "INST=--instance %PAIR%"
if not exist "%PY%" (
    echo Python venv not found: %CD%\%PY%
    set "RC=1"
    goto end
)
"%PY%" -m tradingsystem run --stop %INST%
if errorlevel 1 (
    echo Stop failed - not starting again. See the supervisor-ctl log in logs\ or logs\%PAIR%\
    set "RC=1"
    goto end
)
"%PY%" -m tradingsystem run all --detach %INST%
set "RC=%ERRORLEVEL%"
:end
if not defined NOPAUSE pause
exit /b %RC%
