@echo off
rem Show a system's supervisor, services, the MT5 terminal, collector heartbeats, the other running systems and the
rem dashboard URL. Read-only.   status.bat  |  status.bat BTCUSDT  |  status_all.bat = every pair.
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
"%PY%" -m tradingsystem run --status %INST%
set "RC=%ERRORLEVEL%"
:end
if not defined NOPAUSE pause
exit /b %RC%
