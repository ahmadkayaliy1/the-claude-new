@echo off
rem Stop a trading system gracefully (services flush and exit; up to ~90 s, then forced).
rem   stop.bat             the all-pairs system        stop.bat BTCUSDT    the system of that pair
rem Also pauses its autostart until start.bat is run. stop_all.bat stops every system. Add /nopause from a script.
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
shift /1
goto args
:run
if defined PAIR set "INST=--instance %PAIR%"
if not exist "%PY%" (
    echo Python venv not found: %CD%\%PY%
    set "RC=1"
    goto end
)
"%PY%" -m tradingsystem run --stop %INST%
set "RC=%ERRORLEVEL%"
:end
if not defined NOPAUSE pause
exit /b %RC%
