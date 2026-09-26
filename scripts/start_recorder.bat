@echo off
rem Start the P1.12 price-matching recorder (research; Binance bookTicker + MT5 ticks) in its own minimised window.
rem Run scripts\start.bat first: the recorder must attach to the MT5 terminal the supervisor started, never
rem launch one itself. Stop it by creating data\research\price_matching\STOP (or closing its window).
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
start "TradingSystem price recorder" /min "%PY%" research\price_matching\recorder.py --flush-s 300 --mt5-interval-ms 50
echo Recorder started in a minimised window.
:end
if /i not "%~1"=="/nopause" pause
