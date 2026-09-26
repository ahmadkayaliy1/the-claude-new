@echo off
rem Re-arm trading after the account-wide drawdown stop tripped (risk.account_drawdown_stop_pct, D-042): every
rem system refuses new trades once the account's equity fell that far below its peak, until you run this.
rem Shows the recorded peaks first; the peak then restarts at the current equity. Also run it after a withdrawal.
rem It re-arms every recorded account: the MT5 account shared by all pairs, and each system's paper account.
setlocal
cd /d "%~dp0.."
set "PY=.venv\Scripts\python.exe"
set "PAIR="
set "INST="
set "NOPAUSE="
:args
if "%~1"=="" goto run
if /i "%~1"=="/nopause" (set "NOPAUSE=1") else (set "PAIR=%~1")
shift
goto args
:run
if defined PAIR set "INST=--instance %PAIR%"
if not exist "%PY%" (
    echo Python venv not found: %CD%\%PY%
    set "RC=1"
    goto end
)
if defined PAIR echo Note: the drawdown stop is per account, not per pair - every recorded account is re-armed.
"%PY%" -m tradingsystem.execution.drawdown --reset
set "RC=%ERRORLEVEL%"
:end
if not defined NOPAUSE pause
exit /b %RC%
