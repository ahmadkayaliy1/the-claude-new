@echo off
rem Start the P1.12 price-matching recorder (research; Binance bookTicker + MT5 ticks), hidden (no window).
rem Run scripts\start.bat first: the recorder must attach to the MT5 terminal the supervisor started, never
rem launch one itself. Stop it only by creating data\research\price_matching\STOP: the TradingSystemOps-Recorder
rem task restarts (hidden) a recorder that was closed or ended any other way.
rem Never two recorders: the start goes through tools\recorder_keepalive.py (the task's own check), which starts
rem nothing while a recorder.py process runs. A hung one (alive, no flush for 15 min) never reads STOP: end it with
rem taskkill /PID N /T /F (N = the pid it prints), then run this again.
setlocal
cd /d "%~dp0.."
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo Python venv not found: %CD%\%PY%
    goto end
)
echo Waiting for the MT5 terminal (started by scripts\start.bat)...
for /l %%i in (1,1,60) do (
    tasklist /FI "IMAGENAME eq terminal64.exe" 2>nul | find /i "terminal64.exe" >nul && goto run
    timeout /t 2 /nobreak >nul
)
echo MT5 terminal is not running - run scripts\start.bat first.
goto end
:run
del /q "data\research\price_matching\STOP" 2>nul
rem The keep-alive's own check: STOP (just removed), MT5, a recorder already running, the crash-loop guard.
echo Starting the recorder unless one already runs - never two:
"%PY%" tools\recorder_keepalive.py
if errorlevel 1 echo The recorder did not start - see logs\recorder-keepalive.jsonl and logs\recorder.jsonl.
:end
if /i not "%~1"=="/nopause" pause
