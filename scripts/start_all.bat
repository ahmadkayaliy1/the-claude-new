@echo off
rem Start the independent system of every configured pair (config "instances:", D-042), one after the other.
rem One pair only: start.bat BTCUSDT. Refused while the all-pairs system runs, and for a pair without its own
rem app.db yet: switch once with switch_to_pairs.bat (it stops the all-pairs system and copies each pair's history).
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
set "N=0"
for /f "usebackq delims=" %%P in (`%PY% -m tradingsystem config --instances`) do (
    set /a N+=1 >nul
    echo ===== %%P
    "%PY%" -m tradingsystem run all --detach --instance %%P
    if errorlevel 1 set "RC=1"
)
if "%N%"=="0" (
    echo No pair is configured in config "instances:" - nothing done.
    set "RC=1"
)
:end
if /i not "%~1"=="/nopause" pause
exit /b %RC%
