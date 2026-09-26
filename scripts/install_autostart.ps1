<#
.SYNOPSIS
    Registers the trading system's autostart in Task Scheduler (P5.3 / H7). YOU run it, once:
        scripts\install_autostart.bat            (double-click)
    or  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_autostart.ps1

.DESCRIPTION
    Creates two tasks for the current Windows account (normal rights, "run only when user is logged on":
    MetaTrader 5 needs your desktop session):

    TradingSystem-MT5   at logon: starts the MT5 terminal (the mt5 data profile's terminal_path).
                        Task Scheduler is its parent, so no process of ours - watchdog kill, supervisor crash,
                        stop.bat - can ever close it. No time limit (the default 72 h would kill it).
                        The supervisor also uses this task to restart a terminal that was closed.

    TradingSystem       90 s after logon and then every 5 minutes: `run all --detach --auto`
                        = start the supervisor (hidden) if it is not running, replace it if its heartbeat is
                        dead for 5 min; do nothing after scripts\stop.bat until scripts\start.bat.

    Both run on battery too, at normal priority, and start as soon as possible after a missed run (PC asleep).
    Re-running this script replaces both tasks. Remove them with scripts\uninstall_autostart.bat.
    If Register-ScheduledTask says "Access is denied", run it from an elevated PowerShell: the tasks still run
    as you, not elevated. Details: docs\ops_windows.md

.PARAMETER TerminalPath
    MT5 terminal to start at logon (default: config mt5.profiles[<data_profile>].terminal_path).
.PARAMETER NoMT5Task
    Do not create TradingSystem-MT5 (e.g. you start MT5 from the Startup folder yourself).
.PARAMETER DryRun
    Show what would be registered, change nothing.
#>
[CmdletBinding()]
param(
    [string]$TerminalPath = "",
    [int]$KeepAliveMinutes = 5,
    [int]$LogonDelaySeconds = 90,
    [switch]$NoMT5Task,
    [switch]$DryRun
)
$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$py = Join-Path $root ".venv\Scripts\python.exe"
$pyw = Join-Path $root ".venv\Scripts\pythonw.exe"
foreach ($exe in $py, $pyw) {
    if (-not (Test-Path $exe)) { throw "Python venv not found: $exe" }
}
if (-not $NoMT5Task -and -not $TerminalPath) {
    Push-Location $root
    try {
        $TerminalPath = (& $py -c "from tradingsystem.core.settings import load_settings; print(load_settings().mt5_data_profile().terminal_path)").Trim()
    } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0 -or -not $TerminalPath) { throw "could not read the MT5 terminal path from config" }
}
if (-not $NoMT5Task) {
    $TerminalPath = [System.IO.Path]::GetFullPath($TerminalPath)
    if (-not (Test-Path $TerminalPath)) { throw "MT5 terminal not found: $TerminalPath (pass -TerminalPath or -NoMT5Task)" }
}

$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

# ---- TradingSystem-MT5: the terminal, started by Task Scheduler (never inside our job / process tree)
$mt5Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -Priority 5
$mt5Trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$mt5Action = if ($TerminalPath) {
    New-ScheduledTaskAction -Execute $TerminalPath -WorkingDirectory (Split-Path $TerminalPath -Parent)
}

# ---- TradingSystem: keep-alive for the supervisor (returns in seconds; the supervisor runs detached)
$logon = New-ScheduledTaskTrigger -AtLogOn -User $user
$logon.Delay = "PT$($LogonDelaySeconds)S"
$every = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $KeepAliveMinutes)          # no duration = indefinitely
$tsSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -Priority 5
$tsAction = New-ScheduledTaskAction -Execute $pyw -Argument "-m tradingsystem run all --detach --auto" `
    -WorkingDirectory $root

Write-Host "project : $root"
Write-Host "account : $user (interactive, not elevated)"
if (-not $NoMT5Task) { Write-Host "task    : TradingSystem-MT5 -> $TerminalPath (at logon, no time limit)" }
Write-Host "task    : TradingSystem     -> $pyw -m tradingsystem run all --detach --auto"
Write-Host "          (at logon + $LogonDelaySeconds s, then every $KeepAliveMinutes min)"
if ($DryRun) { Write-Host "DryRun: nothing registered."; exit 0 }

if (-not $NoMT5Task) {
    Register-ScheduledTask -TaskName "TradingSystem-MT5" -Action $mt5Action -Trigger $mt5Trigger `
        -Principal $principal -Settings $mt5Settings -Force `
        -Description "Trading system: MetaTrader 5 terminal at logon, outside every trading-system process (docs\ops_windows.md)" | Out-Null
}
Register-ScheduledTask -TaskName "TradingSystem" -Action $tsAction -Trigger @($logon, $every) `
    -Principal $principal -Settings $tsSettings -Force `
    -Description "Trading system: start/keep the supervisor running; paused by scripts\stop.bat (docs\ops_windows.md)" | Out-Null

Get-ScheduledTask -TaskName "TradingSystem*" | Format-Table TaskName, State -AutoSize
Write-Host "Done. The keep-alive starts the system within $KeepAliveMinutes min unless you stopped it with stop.bat"
Write-Host "(then run scripts\start.bat). Check: scripts\status.bat  |  remove: scripts\uninstall_autostart.bat"
