@echo off
rem ONE-TIME SWITCH from the all-pairs system to one independent system per pair (D-042):
rem   1. stops the all-pairs system (and pauses its autostart),
rem   2. gives every configured pair its own app.db (a copy of data\app.db with that pair's history, see
rem      tools\migrate_instance.py) and moves the AI usage into the shared ledger,
rem   3. replaces the autostart task "TradingSystem" with one task per pair (TradingSystem-BTCUSDT, ...),
rem   4. starts every pair's system.
rem Safe to re-run: a pair that already has its own app.db is left as it is.
setlocal
cd /d "%~dp0.."
set "PY=.venv\Scripts\python.exe"
set "RC=0"
if not exist "%PY%" (
    echo Python venv not found: %CD%\%PY%
    set "RC=1"
    goto end
)
echo ===== 1/4 stopping the all-pairs system
"%PY%" -m tradingsystem run --stop
if errorlevel 1 (
    echo The all-pairs system did not stop - nothing changed. See scripts\status.bat
    set "RC=1"
    goto end
)
echo ===== 2/4 one app.db per pair
"%PY%" tools\migrate_instance.py --all
if errorlevel 1 (
    echo Migration failed - nothing started. Fix the error above, then run this again.
    set "RC=1"
    goto end
)
echo ===== 3/4 autostart: one task per pair
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_autostart.ps1" -AllPairs
if errorlevel 1 (
    echo Autostart not changed - start the pairs by hand with start_all.bat, fix the autostart later.
    set "RC=1"
)
echo ===== 4/4 starting every pair
call "%~dp0start_all.bat" /nopause
if errorlevel 1 set "RC=1"
:end
if /i not "%~1"=="/nopause" pause
exit /b %RC%
