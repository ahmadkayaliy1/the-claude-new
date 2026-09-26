@echo off
rem Release the kill switch: new orders are allowed again (subject to the risk gate).
rem   kill_switch_off.bat          the switch of every system      kill_switch_off.bat BTCUSDT   that pair's switch
setlocal
cd /d "%~dp0.."
set "PY=.venv\Scripts\python.exe"
set "PAIR="
set "NOPAUSE="
:args
if "%~1"=="" goto run
if /i "%~1"=="/nopause" (set "NOPAUSE=1") else (set "PAIR=%~1")
shift /1
goto args
:run
if defined PAIR (
    if not exist "%PY%" (
        echo Cannot check the pair name without the Python venv - nothing changed.
        echo Run kill_switch_off.bat without a pair: it covers every system.
        set "RC=1"
        goto end
    )
    "%PY%" -m tradingsystem config --instances | findstr /x /i /c:"%PAIR%" >nul
    if errorlevel 1 (
        echo Unknown pair "%PAIR%" - nothing changed. Configured pairs:
        "%PY%" -m tradingsystem config --instances
        echo Run kill_switch_off.bat without a pair to cover every system.
        set "RC=1"
        goto end
    )
)
set "KS=data"
if defined PAIR set "KS=data\instances\%PAIR%"
del /q "%KS%\KILL_SWITCH" 2>nul
echo KILL SWITCH OFF (%KS%\KILL_SWITCH) - orders allowed again.
if defined PAIR if exist data\KILL_SWITCH echo Note: the global data\KILL_SWITCH is still ON - no orders from any system until kill_switch_off.bat without a pair.
if not defined PAIR for /d %%D in (data\instances\*) do if exist "%%D\KILL_SWITCH" echo Note: %%D\KILL_SWITCH is still on - kill_switch_off.bat %%~nxD
set "RC=0"
:end
if not defined NOPAUSE pause
exit /b %RC%
