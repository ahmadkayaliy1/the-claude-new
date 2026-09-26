@echo off
rem Start a trading system in the background (hidden console): every service + its dashboard.
rem   start.bat            the all-pairs system (every enabled pair in one system)
rem   start.bat BTCUSDT    the independent system of one pair (config "instances:", D-042); start_all.bat = every pair
rem Also resumes autostart after stop.bat. Add /nopause when calling it from a script. Details: docs\ops_windows.md
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
"%PY%" -m tradingsystem run all --detach %INST%
set "RC=%ERRORLEVEL%"
:end
if not defined NOPAUSE pause
exit /b %RC%
