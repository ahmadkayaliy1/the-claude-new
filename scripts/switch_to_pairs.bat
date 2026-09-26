@echo off
rem ONE-TIME SWITCH from the all-pairs system to one independent system per pair (D-042):
rem   switch_to_pairs.bat                  every pair in config "instances:"
rem   switch_to_pairs.bat BTCUSDT          only these pairs run (e.g. one first: 3 systems need ~3x the RAM of one)
rem   1. stops every trading system (the all-pairs one, and any pair already running; pauses their autostart),
rem   2. gives EVERY configured pair its own app.db (a copy of data\app.db with that pair's history, see
rem      tools\migrate_instance.py; so a pair can be added later with start.bat PAIR) and moves the AI usage into
rem      the shared ledger,
rem   3. replaces the autostart task "TradingSystem" with one task per chosen pair (TradingSystem-BTCUSDT, ...),
rem   4. starts the chosen pairs' systems.
rem Safe to re-run: a pair that already has its own app.db is left as it is. Add /nopause from a script.
setlocal
cd /d "%~dp0.."
set "PY=.venv\Scripts\python.exe"
set "RC=0"
set "PAIRS="
set "PLIST="
set "NOPAUSE="
:args
if "%~1"=="" goto run
if /i "%~1"=="/nopause" (set "NOPAUSE=1") else (set "PAIRS=%PAIRS% %~1" & set "PLIST=%PLIST%,%~1")
shift /1
goto args
:run
if not exist "%PY%" (
    echo Python venv not found: %CD%\%PY%
    set "RC=1"
    goto end
)
if defined PAIRS for %%P in (%PAIRS%) do (
    "%PY%" -m tradingsystem config --instances | findstr /x /i /c:"%%P" >nul
    if errorlevel 1 (
        echo Unknown pair "%%P" - nothing changed. Configured pairs:
        "%PY%" -m tradingsystem config --instances
        set "RC=1"
        goto end
    )
)
echo ===== 1/4 stopping every trading system
call "%~dp0stop_all.bat" /nopause
if errorlevel 1 (
    echo A system did not stop - nothing else changed. See scripts\status.bat and scripts\status_all.bat
    set "RC=1"
    goto end
)
echo ===== 2/4 one app.db per pair
"%PY%" tools\migrate_instance.py --all --wait 60
if errorlevel 1 (
    echo Migration failed - nothing started. Fix the error above, then run this again.
    set "RC=1"
    goto end
)
echo ===== 3/4 autostart: one task per pair
set "ASARG=-AllPairs"
if defined PLIST set "ASARG=-Pair %PLIST:~1%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_autostart.ps1" %ASARG%
if errorlevel 1 (
    echo Autostart NOT changed - the pairs are started now, but nothing restarts them after a reboot.
    echo Register it by hand ^(elevated PowerShell if it said "Access is denied"^):
    echo   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 %ASARG%
    set "RC=1"
)
echo ===== 4/4 starting the pairs
if not defined PAIRS (
    call "%~dp0start_all.bat" /nopause
    if errorlevel 1 set "RC=1"
    goto end
)
for %%P in (%PAIRS%) do (
    echo ===== %%P
    "%PY%" -m tradingsystem run all --detach --instance %%P
    if errorlevel 1 set "RC=1"
)
:end
if not defined NOPAUSE pause
exit /b %RC%
