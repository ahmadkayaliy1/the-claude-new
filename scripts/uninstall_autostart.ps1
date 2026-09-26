<#
.SYNOPSIS
    Removes the autostart tasks created by install_autostart.ps1: TradingSystem, every TradingSystem-<PAIR>
    (one system per pair) and TradingSystem-MT5.
    Does not stop anything that is running: use scripts\stop.bat / scripts\stop_all.bat for the trading system.
    Removing TradingSystem-MT5 while the terminal it started is open may close that terminal on some Windows
    builds - close MT5 first (or re-open it afterwards) if that matters.
.PARAMETER KeepMT5Task
    Remove only the keep-alive tasks; keep TradingSystem-MT5.
#>
[CmdletBinding()]
param([switch]$KeepMT5Task)
# Continue, not Stop: schtasks writes errors about OTHER tasks to stderr (Stop would abort); exit codes are checked
$ErrorActionPreference = "Continue"
# schtasks, not Get-ScheduledTask: PowerShell 5.1 cannot read back a trigger that repeats indefinitely (0x80041318)
$names = @()
foreach ($line in (schtasks /Query /FO CSV /NH 2>$null)) {
    $n = ($line -split '","')[0].Trim('"').TrimStart('\')
    if ($n -eq "TradingSystem" -or $n -like "TradingSystem-*") { $names += $n }
}
$names = @($names | Sort-Object -Unique | Where-Object { -not ($KeepMT5Task -and $_ -eq "TradingSystem-MT5") })
if (-not $names) { Write-Host "no TradingSystem task registered" }
foreach ($name in $names) {
    schtasks /Delete /TN $name /F 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host "removed  $name" } else { Write-Host "FAILED   $name (exit $LASTEXITCODE)" }
}
