<#
.SYNOPSIS
    Removes the autostart tasks created by install_autostart.ps1 (TradingSystem, TradingSystem-MT5).
    Does not stop anything that is running: use scripts\stop.bat for the trading system.
    Removing TradingSystem-MT5 while the terminal it started is open may close that terminal on some Windows
    builds - close MT5 first (or re-open it afterwards) if that matters.
#>
[CmdletBinding()]
param()
$ErrorActionPreference = "Stop"
foreach ($name in "TradingSystem", "TradingSystem-MT5") {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "removed  $name"
    } else {
        Write-Host "absent   $name"
    }
}
