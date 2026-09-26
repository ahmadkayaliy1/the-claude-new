@echo off
rem EMERGENCY STOP for new orders: the executor refuses every new order while data\KILL_SWITCH exists.
rem Open positions keep their stop-loss/take-profit at the broker. Undo with kill_switch_off.bat.
cd /d "%~dp0.."
if not exist data mkdir data
echo engaged %DATE% %TIME% > data\KILL_SWITCH
echo KILL SWITCH ON - no new orders will be sent.
pause
