@echo off
rem EMERGENCY STOP for new orders: the executor refuses every new order while the KILL_SWITCH file exists.
rem   kill_switch_on.bat           every system (data\KILL_SWITCH)
rem   kill_switch_on.bat BTCUSDT   that pair only (data\instances\BTCUSDT\KILL_SWITCH) - honoured by the pair's own
rem                                system and by the all-pairs system; the name is checked against config "instances:"
rem Open positions keep their stop-loss/take-profit at the broker. Undo with kill_switch_off.bat [PAIR].
setlocal
cd /d "%~dp0.."
set "PY=.venv\Scripts\python.exe"
set "PAIR="
set "NOPAUSE="
:args
if "%~1"=="" goto run
if /i "%~1"=="/nopause" (set "NOPAUSE=1") else (set "PAIR=%~1")
shift
goto args
:run
if defined PAIR (
    if not exist "%PY%" (
        echo Cannot check the pair name without the Python venv - nothing changed.
        echo Run kill_switch_on.bat without a pair: it covers every system.
        set "RC=1"
        goto end
    )
    "%PY%" -m tradingsystem config --instances | findstr /x /i /c:"%PAIR%" >nul
    if errorlevel 1 (
        echo Unknown pair "%PAIR%" - nothing changed. Configured pairs:
        "%PY%" -m tradingsystem config --instances
        echo Run kill_switch_on.bat without a pair to cover every system.
        set "RC=1"
        goto end
    )
)
set "KS=data"
if defined PAIR set "KS=data\instances\%PAIR%"
if not exist "%KS%" mkdir "%KS%"
echo engaged %DATE% %TIME% > "%KS%\KILL_SWITCH"
echo KILL SWITCH ON (%KS%\KILL_SWITCH) - no new orders will be sent.
echo Pending orders already at the broker can still fill - delete them in MetaTrader 5 if needed.
set "RC=0"
:end
if not defined NOPAUSE pause
exit /b %RC%
