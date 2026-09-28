@echo off
rem Restart every pair's system: back up the state (tools\backup_state.py; a failure only warns), stop each one, then
rem start_all.bat. The all-pairs system is not touched (to switch from it to one system per pair use
rem switch_to_pairs.bat).
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
if not exist "tools\backup_state.py" goto nobackup
echo ===== backing up the state first (backups\, logs\backup.jsonl)
"%PY%" tools\backup_state.py --quiet
set "BRC=%ERRORLEVEL%"
if "%BRC%"=="0" (echo backup done) else (echo WARNING: the backup ended with exit code %BRC% - restarting anyway. See logs\backup.jsonl)
goto stopping
:nobackup
echo WARNING: tools\backup_state.py not found - no backup before the restart
:stopping
set "N=0"
for /f "usebackq delims=" %%P in (`%PY% -m tradingsystem config --instances`) do (
    set /a N+=1 >nul
    echo ===== stopping %%P
    "%PY%" -m tradingsystem run --stop --instance %%P
    if errorlevel 1 set "RC=1"
)
if "%RC%"=="1" (
    echo A stop failed - not starting again. See status_all.bat
    goto end
)
call "%~dp0start_all.bat" /nopause
set "RC=%ERRORLEVEL%"
:end
if /i not "%~1"=="/nopause" pause
exit /b %RC%
