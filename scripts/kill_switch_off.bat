@echo off
rem Release the kill switch: new orders are allowed again (subject to the risk gate).
rem   kill_switch_off.bat          the switch of every system      kill_switch_off.bat BTCUSDT   that pair's switch
setlocal
cd /d "%~dp0.."
set "PAIR="
set "NOPAUSE="
:args
if "%~1"=="" goto run
if /i "%~1"=="/nopause" (set "NOPAUSE=1") else (set "PAIR=%~1")
shift
goto args
:run
set "KS=data"
if defined PAIR set "KS=data\instances\%PAIR%"
del /q "%KS%\KILL_SWITCH" 2>nul
echo KILL SWITCH OFF (%KS%\KILL_SWITCH) - orders allowed again.
if not defined PAIR for /d %%D in (data\instances\*) do if exist "%%D\KILL_SWITCH" echo Note: %%D\KILL_SWITCH is still on - kill_switch_off.bat %%~nxD
set "RC=0"
:end
if not defined NOPAUSE pause
exit /b %RC%
