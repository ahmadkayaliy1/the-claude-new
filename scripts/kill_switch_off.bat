@echo off
rem Release the kill switch: new orders are allowed again (subject to the risk gate).
cd /d "%~dp0.."
del /q data\KILL_SWITCH 2>nul
echo KILL SWITCH OFF - orders allowed again.
pause
