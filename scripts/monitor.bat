@echo off
rem Run the pure-Python monitor once by hand (no Claude). Task Scheduler runs it every 15 min
rem (TradingSystemOps-Monitor: pythonw tools\monitor.py --quiet, installed by scripts\install_operator_tasks.bat).
rem   monitor.bat                  every check; notifications, KILL_SWITCH files and diagnosis as the rules say
rem   monitor.bat --dry-run        check and print only: no switch, no notification, no state file, no diagnosis
rem   monitor.bat --no-diagnose    never start a Claude diagnosis session from this run
rem   monitor.bat --json           the result as JSON
rem Exit code: 0 no problem, 1 a warning or worse. Details: docs\monitoring.md
setlocal
cd /d "%~dp0.."
set "PY=.venv\Scripts\python.exe"
set "ARGS="
set "NOPAUSE="
:args
if "%~1"=="" goto run
if /i "%~1"=="/nopause" (set "NOPAUSE=1") else (set "ARGS=%ARGS% %~1")
shift /1
goto args
:run
if not exist "%PY%" (
    echo Python venv not found: %CD%\%PY%
    set "RC=2"
    goto end
)
"%PY%" tools\monitor.py %ARGS%
set "RC=%ERRORLEVEL%"
:end
if not defined NOPAUSE pause
exit /b %RC%
