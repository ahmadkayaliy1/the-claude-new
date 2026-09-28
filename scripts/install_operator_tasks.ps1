<#
.SYNOPSIS
    Registers the operator tasks in Task Scheduler (Phase 4: H19; Phase 5: backup + recorder keep-alive, H26a).
    YOU run it, from C:\the_claude_new (again after a merge that adds a task - it re-registers all of them):
        scripts\install_operator_tasks.bat -DryRun      (look first)
        scripts\install_operator_tasks.bat              (register)
        scripts\install_operator_tasks.bat -Uninstall   (remove them again)

.DESCRIPTION
    Five tasks for the current Windows account (normal rights, "run only when user is logged on": the Claude CLI
    sign-in lives in your user profile). Details: docs\operator_sessions.md, docs\monitoring.md, docs\ops_windows.md.

    TradingSystemOps-Monitor       every 15 minutes: pythonw tools\monitor.py --quiet (no AI; writes only kill
                                   switch files and its state; may start a diagnosis session)
    TradingSystemOps-ReviewDaily   04:30 UTC every day: tools\operator\run_session.ps1 -Kind daily (Claude, ~30 k tokens)
    TradingSystemOps-ReviewWeekly  Sunday 06:00 UTC: tools\operator\run_session.ps1 -Kind weekly
    TradingSystemOps-Backup        03:30 UTC every day: pythonw tools\backup_state.py --quiet (no AI; a zip of the
                                   state in backups\, the newest backup.keep kept; docs\ops_windows.md section 9)
    TradingSystemOps-Recorder      every 5 minutes: pythonw tools\recorder_keepalive.py --quiet (starts the P1.12 price
                                   recorder when the MT5 terminal runs, no STOP file exists and no recorder runs)

    The names are deliberately NOT "TradingSystem-...": install_autostart.ps1 treats every TradingSystem-* task
    (except TradingSystem-MT5) as a per-pair keep-alive and would delete them when the layout changes (that is why
    the recorder keep-alive lives here and not in install_autostart.ps1).
    The review and backup times are set in UTC (StartBoundary ending in "Z" = synchronized across time zones), so they
    do not move with daylight saving time; the script reads the stored StartBoundary back to verify it.
    Time limits: monitor 10 min; recorder keep-alive 4 min; backup 30 min; daily review 130 min, weekly review 190 min.
    The review limits are only a backstop
    (a hung runner): a session ends itself at operator.daily_timeout_min / weekly_timeout_min (config, default 20 / 40),
    and these limits lie 10 min beyond the largest value the config accepts (120 / 180), so the config's own deadline
    always governs and raising *_timeout_min needs no re-install. -DailyLimitMinutes / -WeeklyLimitMinutes override
    them (a lower value is reported: a session running longer would be cut off without a record or notification).
    Missed runs (PC asleep) start as soon as possible; a run still going is never started twice.
    Retire the desktop app's 3-hourly monitor task yourself once these run.

.PARAMETER DryRun
    Show what would be registered or removed, change nothing.
.PARAMETER Uninstall
    Remove the five tasks.
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Uninstall,
    [int]$MonitorMinutes = 15,
    [string]$DailyUtc = "04:30",
    [string]$WeeklyUtc = "06:00",
    [ValidateSet("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")]
    [string]$WeeklyDay = "Sunday",
    [int]$MonitorLimitMinutes = 10,
    [int]$DailyLimitMinutes = 130,
    [int]$WeeklyLimitMinutes = 190,
    [string]$BackupUtc = "03:30",
    [int]$BackupLimitMinutes = 30,
    [int]$RecorderMinutes = 5,
    [int]$RecorderLimitMinutes = 4
)
$ErrorActionPreference = "Stop"

# The review sessions end themselves at operator.<kind>_timeout_min (tools\operator\run_session.py), which the settings
# accept up to these values (src\tradingsystem\core\settings.py, OperatorCfg le=). Task Scheduler's limit is a backstop
# $MarginMinutes beyond them, so the config deadline always governs; the defaults above are max + margin (pinned by
# test_review_task_limits_are_a_backstop_beyond_every_config_value).
$ConfigMaxMinutes = @{ daily = 120; weekly = 180 }
$MarginMinutes = 10

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$py = Join-Path $root ".venv\Scripts\python.exe"
$pyw = Join-Path $root ".venv\Scripts\pythonw.exe"
$monitorPy = Join-Path $root "tools\monitor.py"
$sessionPs1 = Join-Path $root "tools\operator\run_session.ps1"
$backupPy = Join-Path $root "tools\backup_state.py"
$recorderPy = Join-Path $root "tools\recorder_keepalive.py"
$names = @("TradingSystemOps-Monitor", "TradingSystemOps-ReviewDaily", "TradingSystemOps-ReviewWeekly",
    "TradingSystemOps-Backup", "TradingSystemOps-Recorder")
# the names the spec text used first; they collide with install_autostart.ps1's per-pair pattern - removed if found
$legacy = @("TradingSystem-Monitor", "TradingSystem-Review-Daily", "TradingSystem-Review-Weekly")

function Test-Task([string]$name) {
    # schtasks, not Get-ScheduledTask: PowerShell 5.1 cannot read back a trigger that repeats indefinitely.
    # Local Continue: schtasks writes "cannot find" to stderr, which "Stop" would turn into a throw
    $ErrorActionPreference = "Continue"
    schtasks /Query /TN $name 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Remove-Task([string]$name) {
    & { $ErrorActionPreference = "Continue"; schtasks /Delete /TN $name /F 2>$null | Out-Null }
    if ($LASTEXITCODE -eq 0) { Write-Host "removed   : $name" } else { Write-Host "FAILED    : could not remove $name (exit $LASTEXITCODE)" }
}

function Get-StartBoundary([string]$name) {
    $xml = & { $ErrorActionPreference = "Continue"; schtasks /Query /TN $name /XML 2>$null }
    $m = [regex]::Match(($xml -join "`n"), "<StartBoundary>([^<]+)</StartBoundary>")
    if ($m.Success) { return $m.Groups[1].Value }
    return ""
}

function Get-NextUtc([string]$hhmm, [string]$day) {
    # the next occurrence of hh:mm UTC (on $day when given) - in the future, so registering never counts as a missed run
    $parts = $hhmm.Split(":")
    if ($parts.Count -ne 2) { throw "time '$hhmm' must be HH:MM (UTC)" }
    $now = [DateTime]::UtcNow
    $t = [DateTime]::SpecifyKind($now.Date.AddHours([int]$parts[0]).AddMinutes([int]$parts[1]), [DateTimeKind]::Utc)
    for ($i = 0; $i -lt 8; $i++) {
        if ($t -gt $now -and (-not $day -or $t.DayOfWeek.ToString() -eq $day)) { return $t }
        $t = $t.AddDays(1)
    }
    throw "no next occurrence for $hhmm $day"
}

# ---------------------------------------------------------------- uninstall
if ($Uninstall) {
    $present = @(($names + $legacy) | Where-Object { Test-Task $_ })
    if (-not $present.Count) { Write-Host "no operator task registered"; exit 0 }
    foreach ($n in $present) { Write-Host "remove  : $n" }
    if ($DryRun) { Write-Host "DryRun: nothing removed."; exit 0 }
    foreach ($n in $present) { Remove-Task $n }
    exit 0
}

# ---------------------------------------------------------------- plan
$problems = @()
foreach ($f in $py, $pyw, $sessionPs1) {
    if (-not (Test-Path $f)) { $problems += "missing: $f" }
}
foreach ($f in $monitorPy, $backupPy, $recorderPy) {
    if (-not (Test-Path $f)) { $problems += "missing: $f (its task would fail until it exists)" }
}
$limitNotes = @()
foreach ($lim in @(@{ kind = "daily"; minutes = $DailyLimitMinutes }, @{ kind = "weekly"; minutes = $WeeklyLimitMinutes })) {
    $floor = $ConfigMaxMinutes[$lim.kind] + $MarginMinutes
    if ($lim.minutes -lt $floor) {
        $limitNotes += ("{0} review limit {1} min is below {2}: keep operator.{0}_timeout_min at most {3}, or Task Scheduler cuts the session off (no session record, no notification)" -f $lim.kind, $lim.minutes, $floor, ($lim.minutes - $MarginMinutes))
    }
}
if ($problems.Count -and -not $DryRun) {
    foreach ($p in $problems) { Write-Host $p }
    throw "cannot register the operator tasks (run from the checkout that has the venv: C:\the_claude_new)"
}
$dailyAt = Get-NextUtc $DailyUtc ""
$weeklyAt = Get-NextUtc $WeeklyUtc $WeeklyDay
$backupAt = Get-NextUtc $BackupUtc ""
$fmt = "yyyy-MM-dd'T'HH:mm:ss'Z'"
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$legacyPresent = @($legacy | Where-Object { Test-Task $_ })

Write-Host "project : $root"
Write-Host "account : $user (interactive, not elevated)"
Write-Host ("task    : {0} -> {1} `"{2}`" --quiet (every {3} min, limit {4} min)" -f $names[0], $pyw, $monitorPy, $MonitorMinutes, $MonitorLimitMinutes)
Write-Host ("task    : {0} -> powershell -File `"{1}`" -Kind daily (daily {2} UTC, first {3}, limit {4} min)" -f $names[1], $sessionPs1, $DailyUtc, $dailyAt.ToString($fmt), $DailyLimitMinutes)
Write-Host ("task    : {0} -> powershell -File `"{1}`" -Kind weekly ({2} {3} UTC, first {4}, limit {5} min)" -f $names[2], $sessionPs1, $WeeklyDay, $WeeklyUtc, $weeklyAt.ToString($fmt), $WeeklyLimitMinutes)
Write-Host ("task    : {0} -> {1} `"{2}`" --quiet (daily {3} UTC, first {4}, limit {5} min)" -f $names[3], $pyw, $backupPy, $BackupUtc, $backupAt.ToString($fmt), $BackupLimitMinutes)
Write-Host ("task    : {0} -> {1} `"{2}`" --quiet (every {3} min, limit {4} min)" -f $names[4], $pyw, $recorderPy, $RecorderMinutes, $RecorderLimitMinutes)
foreach ($n in $legacyPresent) { Write-Host "remove  : $n (old name - collides with install_autostart.ps1)" }
foreach ($p in $problems) { Write-Host "WARNING : $p" }
foreach ($p in $limitNotes) { Write-Host "WARNING : $p" }
if ($DryRun) { Write-Host "DryRun: nothing registered."; exit 0 }

# ---------------------------------------------------------------- register
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
function New-OpsSettings([int]$minutes) {
    return New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes $minutes) -Priority 5
}
foreach ($n in $legacyPresent) { Remove-Task $n }

# monitor: a repetition that never ends (no -RepetitionDuration = indefinitely)
$monTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Minutes $MonitorMinutes)
$monAction = New-ScheduledTaskAction -Execute $pyw -Argument "`"$monitorPy`" --quiet" -WorkingDirectory $root
Register-ScheduledTask -TaskName $names[0] -Action $monAction -Trigger $monTrigger -Principal $principal `
    -Settings (New-OpsSettings $MonitorLimitMinutes) -Force `
    -Description "Trading system: pure-Python monitor every $MonitorMinutes min, no AI (docs\monitoring.md)" | Out-Null

# reviews: the trigger's StartBoundary in UTC ("Z") = synchronized across time zones - no DST shift
$sessionArgs = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$sessionPs1`" -Kind"
$dailyTrigger = New-ScheduledTaskTrigger -Daily -At $dailyAt.ToLocalTime()
$dailyTrigger.StartBoundary = $dailyAt.ToString($fmt)
$dailyAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "$sessionArgs daily" -WorkingDirectory $root
Register-ScheduledTask -TaskName $names[1] -Action $dailyAction -Trigger $dailyTrigger -Principal $principal `
    -Settings (New-OpsSettings $DailyLimitMinutes) -Force `
    -Description "Trading system: daily Claude review at $DailyUtc UTC (docs\operator_sessions.md)" | Out-Null

$weeklyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $WeeklyDay -At $weeklyAt.ToLocalTime()
$weeklyTrigger.StartBoundary = $weeklyAt.ToString($fmt)
$weeklyAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "$sessionArgs weekly" -WorkingDirectory $root
Register-ScheduledTask -TaskName $names[2] -Action $weeklyAction -Trigger $weeklyTrigger -Principal $principal `
    -Settings (New-OpsSettings $WeeklyLimitMinutes) -Force `
    -Description "Trading system: weekly Claude review, $WeeklyDay $WeeklyUtc UTC (docs\operator_sessions.md)" | Out-Null

# backup: daily in UTC like the reviews (the zip names are UTC stamps too)
$backupTrigger = New-ScheduledTaskTrigger -Daily -At $backupAt.ToLocalTime()
$backupTrigger.StartBoundary = $backupAt.ToString($fmt)
$backupAction = New-ScheduledTaskAction -Execute $pyw -Argument "`"$backupPy`" --quiet" -WorkingDirectory $root
Register-ScheduledTask -TaskName $names[3] -Action $backupAction -Trigger $backupTrigger -Principal $principal `
    -Settings (New-OpsSettings $BackupLimitMinutes) -Force `
    -Description "Trading system: state backup at $BackupUtc UTC, never .env (docs\ops_windows.md section 9)" | Out-Null

# recorder keep-alive: a repetition that never ends, like the monitor
$recTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(3) -RepetitionInterval (New-TimeSpan -Minutes $RecorderMinutes)
$recAction = New-ScheduledTaskAction -Execute $pyw -Argument "`"$recorderPy`" --quiet" -WorkingDirectory $root
Register-ScheduledTask -TaskName $names[4] -Action $recAction -Trigger $recTrigger -Principal $principal `
    -Settings (New-OpsSettings $RecorderLimitMinutes) -Force `
    -Description "Trading system: P1.12 price recorder keep-alive every $RecorderMinutes min (docs\ops_windows.md section 9)" | Out-Null

# ---------------------------------------------------------------- verify (schtasks, not Get-ScheduledTask)
$bad = 0
foreach ($name in $names) {
    $q = & { $ErrorActionPreference = "Continue"; schtasks /Query /TN $name /FO LIST 2>$null } | Select-String "Status|Next Run"
    Write-Host ("registered: {0}  {1}" -f $name, (($q | ForEach-Object { $_.Line.Trim() }) -join " | "))
}
foreach ($name in $names[1], $names[2], $names[3]) {
    $sb = Get-StartBoundary $name
    if ($sb.EndsWith("Z")) { Write-Host "verified  : $name StartBoundary $sb (UTC)" }
    else { Write-Host "WARNING   : $name StartBoundary '$sb' is not UTC - the run time will move with daylight saving"; $bad++ }
}
Write-Host "Done. Check: scripts\check_ops.bat | logs\operator-session.jsonl | data\reviews\ | backups\ | logs\recorder-keepalive.jsonl | remove: scripts\install_operator_tasks.bat -Uninstall"
if ($bad) { exit 1 }
exit 0
