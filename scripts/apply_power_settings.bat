@echo off
rem Apply the power settings of docs\ops_windows.md section 2 to the current power plan, for plugged-in (AC) and
rem battery (DC): closing the lid and the sleep button do nothing, the power button only turns the display off,
rem Wi-Fi power saving off. The user runs this (the agent never changes system settings). Undo: set the values back
rem in Control Panel > Power Options. Verify with scripts\check_ops.bat.
setlocal
echo Applying power settings to the current power plan...
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setdcvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS SBUTTONACTION 0
powercfg /setdcvalueindex SCHEME_CURRENT SUB_BUTTONS SBUTTONACTION 0
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS PBUTTONACTION 4
powercfg /setdcvalueindex SCHEME_CURRENT SUB_BUTTONS PBUTTONACTION 4
powercfg /setacvalueindex SCHEME_CURRENT 19cbb8fa-5279-450e-9fac-8a3d5fedd0c1 12bbebe6-58d6-4636-95bb-3217ef867c1a 0
powercfg /setdcvalueindex SCHEME_CURRENT 19cbb8fa-5279-450e-9fac-8a3d5fedd0c1 12bbebe6-58d6-4636-95bb-3217ef867c1a 0
powercfg /setactive SCHEME_CURRENT
if errorlevel 1 (
    echo.
    echo Some settings were refused. Right-click this file and choose "Run as administrator".
) else (
    echo.
    echo Done: lid close = do nothing, sleep button = do nothing, power button = display off, Wi-Fi saving off.
)
echo.
echo Clock sync (needs administrator): right-click this file ^> Run as administrator also runs it.
w32tm /resync /force >nul 2>&1 && echo Clock synchronised. || echo Clock sync skipped (not administrator) - optional.
pause
