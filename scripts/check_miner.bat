@echo off
rem Read-only check whether the miner is gone (no administrator needed).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0check_miner.ps1"
pause
