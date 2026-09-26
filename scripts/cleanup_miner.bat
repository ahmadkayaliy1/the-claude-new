@echo off
rem Runs scripts\cleanup_miner.ps1 as administrator (UAC prompt). See PROJECT_STATUS.md H12.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -NoExit -File \"%~dp0cleanup_miner.ps1\"'"
