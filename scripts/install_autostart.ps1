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
      or, one system per pair (-Pair / -AllPairs, D-042):
    TradingSystem-<PAIR> the same keep-alive for that pair's system (`... --instance <PAIR>`), paused by
                        scripts\stop.bat <PAIR>. The pairs' logon delays are 30 s apart (not all at once).

    The all-pairs task and the per-pair tasks exclude each other: registering one kind removes the other
    (the two systems may not run together).

    Both run on battery too, at normal priority, and start as soon as possible after a missed run (PC asleep).
    Re-running this script replaces both tasks. Remove them with scripts\uninstall_autostart.bat.
    If Register-ScheduledTask says "Access is denied", run it from an elevated PowerShell: the tasks still run
    as you, not elevated. Details: docs\ops_windows.md

.PARAMETER TerminalPath
    MT5 terminal to start at logon (default: config mt5.profiles[<data_profile>].terminal_path).
.PARAMETER NoMT5Task
    Do not create TradingSystem-MT5 (e.g. you start MT5 from the Startup folder yourself).
.PARAMETER Pair
    One system per pair: register TradingSystem-<PAIR> for each pair given (e.g. -Pair BTCUSDT,ETHUSDT); other
    per-pair tasks are kept, the all-pairs task TradingSystem is removed.
.PARAMETER AllPairs
    One system per pair for every pair in config "instances:" (per-pair tasks of pairs no longer configured are
    removed, as is TradingSystem).
.PARAMETER AllPairsSystem
    The single all-pairs system (task TradingSystem; every TradingSystem-<PAIR> task is removed). Without any of
    -Pair / -AllPairs / -AllPairsSystem the script keeps the layout in use: per pair when TradingSystem-<PAIR>
    tasks exist (those pairs are kept) or the pairs were switched (data\instances\<PAIR>\app.db + the all-pairs
    system stopped by the user: every pair), else the all-pairs system.
.PARAMETER DryRun
    Show what would be registered, change nothing.
#>
[CmdletBinding()]
param(
    [string]$TerminalPath = "",
    [int]$KeepAliveMinutes = 5,
    [int]$LogonDelaySeconds = 90,
    [switch]$NoMT5Task,
    [string[]]$Pair = @(),
    [switch]$AllPairs,
    [switch]$AllPairsSystem,
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

function Get-TsTasks {
    # schtasks, not Get-ScheduledTask: PowerShell 5.1 cannot read back a trigger that repeats indefinitely.
    # Local Continue: schtasks writes errors about OTHER tasks to stderr, which "Stop" would turn into a throw
    $ErrorActionPreference = "Continue"
    $names = @()
    foreach ($line in (schtasks /Query /FO CSV /NH 2>$null)) {
        $n = ($line -split '","')[0].Trim('"').TrimStart('\')
        if ($n -eq "TradingSystem" -or ($n -like "TradingSystem-*" -and $n -ne "TradingSystem-MT5")) { $names += $n }
    }
    return $names | Sort-Object -Unique
}

# ---- which keep-alive tasks: the all-pairs system, or one per pair (D-042)
$configured = @()
Push-Location $root
try {
    $configured = @(& { $ErrorActionPreference = "Continue"; & $py -m tradingsystem config --instances } |
                    ForEach-Object { "$_".Trim().ToUpper() } | Where-Object { $_ })
    $cfgExit = $LASTEXITCODE
} finally { Pop-Location }
if ($cfgExit -ne 0) { throw "could not read the configured pairs (config 'instances:')" }
$Pair = @($Pair | ForEach-Object { $_ -split "," } | ForEach-Object { $_.Trim().ToUpper() } | Where-Object { $_ })
if (($AllPairsSystem -and ($AllPairs -or $Pair.Count)) -or ($AllPairs -and $Pair.Count)) {
    throw "use only one of -Pair, -AllPairs, -AllPairsSystem"
}
if (-not $AllPairsSystem -and -not $AllPairs -and $Pair.Count -eq 0) {
    # keep the layout in use (a plain double-click must never undo the switch to one system per pair)
    $pairTasks = @(Get-TsTasks | Where-Object { $_ -ne "TradingSystem" })
    $migrated = @($configured | Where-Object { Test-Path (Join-Path $root "data\instances\$_\app.db") })
    $held = Test-Path (Join-Path $root "data\run\manual_stop")
    $kept = @($pairTasks | ForEach-Object { $_.Substring(14) } | Where-Object { $configured -contains $_ })
    if ($kept.Count) {
        $Pair = $kept                           # the pairs that have a task now, no more and no less
        Write-Host "layout  : one system per pair, kept: $($kept -join ', ') - -AllPairs adds every pair, -AllPairsSystem goes back"
    } elseif ($migrated.Count -and $held) {
        $AllPairs = $true
        Write-Host "layout  : one system per pair (switched) - -Pair chooses pairs, -AllPairsSystem goes back to the all-pairs task"
    }
}
if ($AllPairs) { $Pair = $configured }
foreach ($p in $Pair) {
    if ($configured -notcontains $p) { throw "pair $p is not in config 'instances:' ($($configured -join ', '))" }
}
$perPair = $Pair.Count -gt 0

$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

# ---- TradingSystem-MT5: the terminal, started by Task Scheduler (never inside our job / process tree)
$mt5Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -Priority 5
$mt5Trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$mt5Action = if ($TerminalPath) {
    New-ScheduledTaskAction -Execute $TerminalPath -WorkingDirectory (Split-Path $TerminalPath -Parent)
}

# ---- TradingSystem[-<PAIR>]: keep-alive for a supervisor (returns in seconds; the supervisor runs detached)
$tsSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -Priority 5
$keepAlive = @()          # (task name, arguments, logon delay s, first repetition minute)
if ($perPair) {
    $i = 0
    foreach ($p in $Pair) {
        $keepAlive += ,@("TradingSystem-$p", "-m tradingsystem run all --detach --auto --instance $p",
                         ($LogonDelaySeconds + 30 * $i), (1 + $i))
        $i++
    }
} else {
    $keepAlive += ,@("TradingSystem", "-m tradingsystem run all --detach --auto", $LogonDelaySeconds, 1)
}
$existing = @(Get-TsTasks)
$remove = if ($perPair) {
    @($existing | Where-Object { $_ -eq "TradingSystem" -or ($AllPairs -and $Pair -notcontains $_.Substring(14)) })
} else {
    @($existing | Where-Object { $_ -ne "TradingSystem" })
}

Write-Host "project : $root"
Write-Host "account : $user (interactive, not elevated)"
if (-not $NoMT5Task) { Write-Host "task    : TradingSystem-MT5 -> $TerminalPath (at logon, no time limit)" }
foreach ($k in $keepAlive) {
    Write-Host ("task    : {0} -> {1} {2}" -f $k[0], $pyw, $k[1])
    Write-Host ("          (at logon + {0} s, then every {1} min)" -f $k[2], $KeepAliveMinutes)
}
foreach ($n in $remove) { Write-Host "remove  : $n (the other kind of system - they may not run together)" }
if ($DryRun) { Write-Host "DryRun: nothing registered."; exit 0 }

if (-not $NoMT5Task) {
    Register-ScheduledTask -TaskName "TradingSystem-MT5" -Action $mt5Action -Trigger $mt5Trigger `
        -Principal $principal -Settings $mt5Settings -Force `
        -Description "Trading system: MetaTrader 5 terminal at logon, outside every trading-system process (docs\ops_windows.md)" | Out-Null
}
foreach ($n in $remove) {
    & { $ErrorActionPreference = "Continue"; schtasks /Delete /TN $n /F 2>$null | Out-Null }
    if ($LASTEXITCODE -eq 0) { Write-Host "removed   : $n" } else { Write-Host "FAILED    : could not remove $n (exit $LASTEXITCODE)" }
}
foreach ($k in $keepAlive) {
    $logon = New-ScheduledTaskTrigger -AtLogOn -User $user
    $logon.Delay = "PT$($k[2])S"
    $every = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes($k[3]) `
        -RepetitionInterval (New-TimeSpan -Minutes $KeepAliveMinutes)          # no duration = indefinitely
    $action = New-ScheduledTaskAction -Execute $pyw -Argument $k[1] -WorkingDirectory $root
    $stop = if ($perPair) { "scripts\stop.bat $($k[0].Substring(14))" } else { "scripts\stop.bat" }
    Register-ScheduledTask -TaskName $k[0] -Action $action -Trigger @($logon, $every) `
        -Principal $principal -Settings $tsSettings -Force `
        -Description "Trading system: start/keep the supervisor running; paused by $stop (docs\ops_windows.md)" | Out-Null
}

# schtasks, not Get-ScheduledTask: PowerShell 5.1 cannot read back a trigger that repeats indefinitely
# (0x80041318 "incorrectly formatted or out of range") although the task is valid and runs
foreach ($name in @("TradingSystem-MT5") + @($keepAlive | ForEach-Object { $_[0] })) {
    if ($NoMT5Task -and $name -eq "TradingSystem-MT5") { continue }
    $q = & { $ErrorActionPreference = "Continue"; schtasks /Query /TN $name /FO LIST 2>$null } | Select-String "Status|Next Run"
    Write-Host ("registered: {0}  {1}" -f $name, (($q | ForEach-Object { $_.Line.Trim() }) -join " | "))
}
if ($perPair) {
    Write-Host "Done. The keep-alive starts each pair's system within $KeepAliveMinutes min unless you stopped it"
    Write-Host "(scripts\stop.bat <PAIR>; resume: scripts\start.bat <PAIR>). Check: scripts\status_all.bat"
} else {
    Write-Host "Done. The keep-alive starts the system within $KeepAliveMinutes min unless you stopped it with stop.bat"
    Write-Host "(then run scripts\start.bat). Check: scripts\status.bat  |  remove: scripts\uninstall_autostart.bat"
}
