<#
.SYNOPSIS
    Read-only check of the Windows settings the trading system needs (docs\ops_windows.md). Changes nothing.
    scripts\check_ops.bat  (double-click)
#>
[CmdletBinding()]
param()
$ErrorActionPreference = "Continue"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$script:todo = 0

function Get-PowerIndex([string]$Sub, [string]$Setting) {
    # `powercfg /qh` (not /q: the button settings are hidden); the last two hex values are current AC, DC
    $out = powercfg /qh SCHEME_CURRENT $Sub $Setting 2>$null
    $hex = @($out | Select-String -Pattern '0x([0-9a-fA-F]{8})\s*$' |
             ForEach-Object { [Convert]::ToInt64($_.Matches[0].Groups[1].Value, 16) })
    if ($hex.Count -lt 2) { return $null }
    return [pscustomobject]@{ AC = $hex[-2]; DC = $hex[-1] }
}

function Report([string]$Label, $Value, [bool]$Ok, [string]$Hint = "") {
    $mark = if ($Ok) { "ok  " } else { $script:todo++; "FIX " }
    Write-Host ("{0} {1,-34} {2}" -f $mark, $Label, $Value)
    if (-not $Ok -and $Hint) { Write-Host ("     -> {0}" -f $Hint) }
}

function Check-Power([string]$Label, [string]$Sub, [string]$Setting, [hashtable]$Names, [int[]]$Good, [string]$Hint) {
    $v = Get-PowerIndex $Sub $Setting
    if ($null -eq $v) { Report $Label "(not readable)" $false "powercfg /qh SCHEME_CURRENT $Sub $Setting"; return }
    $txt = { param($x) if ($Names.ContainsKey([int]$x)) { "$x=$($Names[[int]$x])" } else { "$x" } }
    Report $Label ("AC {0} | DC {1}" -f (& $txt $v.AC), (& $txt $v.DC)) (($Good -contains $v.AC) -and ($Good -contains $v.DC)) $Hint
}

$buttons = @{ 0 = "do nothing"; 1 = "SLEEP"; 2 = "HIBERNATE"; 3 = "shut down"; 4 = "display off" }
Write-Host "== Power plan (current scheme) - docs\ops_windows.md section 2"
Check-Power "Lid close" SUB_BUTTONS LIDACTION $buttons @(0) `
    "powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0 ; same with /setdcvalueindex ; powercfg /setactive SCHEME_CURRENT"
Check-Power "Sleep button" SUB_BUTTONS SBUTTONACTION $buttons @(0, 4) `
    "powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS SBUTTONACTION 0 ; same with /setdcvalueindex ; powercfg /setactive SCHEME_CURRENT"
Check-Power "Power button" SUB_BUTTONS PBUTTONACTION $buttons @(0, 3, 4) `
    "powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS PBUTTONACTION 4 ; same with /setdcvalueindex ; powercfg /setactive SCHEME_CURRENT"
Check-Power "Sleep after (s, 0 = never)" SUB_SLEEP STANDBYIDLE @{ 0 = "never" } @(0) `
    "powercfg /change standby-timeout-ac 0 ; powercfg /change standby-timeout-dc 0"
Check-Power "Hibernate after (s, 0 = never)" SUB_SLEEP HIBERNATEIDLE @{ 0 = "never" } @(0) `
    "powercfg /change hibernate-timeout-ac 0 ; powercfg /change hibernate-timeout-dc 0"
Check-Power "Wi-Fi power saving" 19cbb8fa-5279-450e-9fac-8a3d5fedd0c1 12bbebe6-58d6-4636-95bb-3217ef867c1a `
    @{ 0 = "max performance"; 1 = "low saving"; 2 = "medium saving"; 3 = "MAX SAVING" } @(0) `
    "powercfg /setacvalueindex SCHEME_CURRENT 19cbb8fa-5279-450e-9fac-8a3d5fedd0c1 12bbebe6-58d6-4636-95bb-3217ef867c1a 0 ; same with /setdcvalueindex ; powercfg /setactive SCHEME_CURRENT"
$crit = Get-PowerIndex SUB_BATTERY BATACTIONCRIT
if ($crit) { Write-Host ("info Critical battery action          AC {0} | DC {1}  (2 = hibernate: keep it)" -f $crit.AC, $crit.DC) }

Write-Host ""
Write-Host "== Power source"
try {
    Add-Type -AssemblyName System.Windows.Forms
    $ps = [System.Windows.Forms.SystemInformation]::PowerStatus
    Report "Power line" ("{0} (battery {1:P0})" -f $ps.PowerLineStatus, $ps.BatteryLifePercent) `
        ("$($ps.PowerLineStatus)" -ne "Offline") "plug the charger in; on battery the laptop hibernates at critical level"
    if ("$($ps.PowerLineStatus)" -eq "Offline") {
        # how long: only the monitor knows (data\shared\monitor_state.json battery.on_battery_since, UTC ms)
        $since = $null
        try {
            $ms = (Get-Content (Join-Path $root "data\shared\monitor_state.json") -Raw -ErrorAction Stop |
                   ConvertFrom-Json).battery.on_battery_since
            if ($ms) { $since = [DateTimeOffset]::FromUnixTimeMilliseconds([int64]$ms) }
        } catch { }
        if ($since) {
            Write-Host ("info Time on battery                    {0:N0} min at least (since {1:yyyy-MM-dd HH:mm} UTC, per the monitor)" -f `
                        ([DateTimeOffset]::UtcNow - $since).TotalMinutes, $since.UtcDateTime)
        } else { Write-Host "info Time on battery                    not known yet (the monitor records it on its next run)" }
    }
} catch { Write-Host "info power state not readable" }
# the last sleep any supervisor saw (run\supervisor.json last_gap of the all-pairs system and of each pair)
$last = $null
foreach ($f in @((Join-Path $root "data\run\supervisor.json")) + @(Get-ChildItem (Join-Path $root "data\instances") -Directory -ErrorAction SilentlyContinue |
                 ForEach-Object { Join-Path $_.FullName "run\supervisor.json" })) {
    if (-not (Test-Path $f)) { continue }
    try { $g = (Get-Content $f -Raw -ErrorAction Stop | ConvertFrom-Json).last_gap } catch { continue }
    if ($g -and $g.kind -eq "suspend" -and $g.ts -and (-not $last -or [int64]$g.ts -gt [int64]$last.ts)) { $last = $g }
}
if ($last) {
    Write-Host ("info Last suspend                       resumed {0:yyyy-MM-dd HH:mm} UTC after ~{1:N0} min asleep" -f `
                [DateTimeOffset]::FromUnixTimeMilliseconds([int64]$last.ts).UtcDateTime, ([double]$last.seconds / 60))
} else { Write-Host "info Last suspend                       none recorded by a running supervisor" }

Write-Host ""
Write-Host "== Wi-Fi adapter power management (Device Manager > adapter > Power Management)"
try {
    Get-NetAdapter -Physical -ErrorAction Stop | Where-Object { $_.Status -eq "Up" } | ForEach-Object {
        $pm = Get-NetAdapterPowerManagement -Name $_.Name -ErrorAction Stop
        Report ("{0}: may turn off device" -f $_.Name) $pm.AllowComputerToTurnOffDevice `
            ("$($pm.AllowComputerToTurnOffDevice)" -notmatch "Enabled") `
            "Device Manager > Network adapters > $($_.InterfaceDescription) > Power Management: untick 'Allow the computer to turn off this device'"
    }
} catch { Write-Host "info adapter power management not readable here (try an elevated PowerShell)" }

Write-Host ""
Write-Host "== Time sync - docs\ops_windows.md section 4"
$svc = Get-Service w32time -ErrorAction SilentlyContinue
if ($svc) {
    Report "Windows Time service" ("{0}, start {1}" -f $svc.Status, $svc.StartType) ($svc.Status -eq "Running") `
        "elevated: sc config w32time start= auto ; net start w32time ; w32tm /resync"
}
$w32 = @(w32tm /query /status 2>&1 | ForEach-Object { "$_" })
if ($w32 -match "Leap Indicator:\s*3") {
    Report "Clock synchronized" "no (leap indicator 3)" $false "elevated: w32tm /resync /force ; then run this check again"
}
$w32 | Select-Object -First 12 | ForEach-Object { Write-Host "     $_" }

Write-Host ""
Write-Host "== Autostart (Task Scheduler) - docs\ops_windows.md section 6"
# schtasks, not Get-ScheduledTask: PowerShell 5.1 cannot read back a trigger that repeats indefinitely (0x80041318)
$tsNames = @()
foreach ($line in (schtasks /Query /FO CSV /NH 2>$null)) {
    $n = ($line -split '","')[0].Trim('"').TrimStart('\')
    if ($n -eq "TradingSystem" -or ($n -like "TradingSystem-*" -and $n -ne "TradingSystem-MT5")) { $tsNames += $n }
}
$opsNames = @()      # Phase 4 operator tasks (scripts\install_operator_tasks.bat, docs\operator_sessions.md)
foreach ($line in (schtasks /Query /FO CSV /NH 2>$null)) {
    $n = ($line -split '","')[0].Trim('"').TrimStart('\')
    if ($n -like "TradingSystemOps-*") { $opsNames += $n }
}
if (-not $tsNames) { $tsNames = @("TradingSystem") }      # neither the all-pairs task nor a per-pair one (D-042)
foreach ($name in @("TradingSystem-MT5") + @($tsNames | Sort-Object -Unique)) {
    $q = schtasks /Query /TN $name /FO LIST /V 2>$null
    if ($LASTEXITCODE -eq 0) {
        $field = { param($k) (($q | Select-String "^\s*$k\s*:" | Select-Object -First 1).Line -replace "^\s*$k\s*:\s*", "").Trim() }
        Write-Host ("info {0,-22} {1,-8} last run {2}  result {3}  next {4}" -f $name, (& $field "Status"),
                    (& $field "Last Run Time"), (& $field "Last Result"), (& $field "Next Run Time"))
    } else { Write-Host ("info {0,-22} not registered (scripts\install_autostart.bat)" -f $name) }
}
if (-not $opsNames) {
    Write-Host "info TradingSystemOps-*      not registered (scripts\install_operator_tasks.bat - monitor, reviews, backup, recorder)"
}
foreach ($name in @($opsNames | Sort-Object -Unique)) {
    $q = schtasks /Query /TN $name /FO LIST /V 2>$null
    if ($LASTEXITCODE -eq 0) {
        $field = { param($k) (($q | Select-String "^\s*$k\s*:" | Select-Object -First 1).Line -replace "^\s*$k\s*:\s*", "").Trim() }
        Write-Host ("info {0,-28} {1,-8} last run {2}  result {3}  next {4}" -f $name, (& $field "Status"),
                    (& $field "Last Run Time"), (& $field "Last Result"), (& $field "Next Run Time"))
        if ((& $field "Last Result") -eq "3") {      # the tool could not read its config (config.local.yaml?)
            $script:todo++                           # counts like a Report FIX: the summary must not say all is right
            $log = if ($name -like "*Monitor*") { "logs\monitor-config-error.log" } else { "logs\operator-session-config-error.log" }
            Write-Host ("FIX  {0,-28} result 3 = config invalid - see {1}, then: .venv\Scripts\python.exe -m tradingsystem config" -f $name, $log)
        }
    }
}

Write-Host ""
Write-Host "== Trading system"
$py = Join-Path $root ".venv\Scripts\python.exe"
if (Test-Path $py) {
    Push-Location $root
    try {
        # the all-pairs system first (exit 0 = it runs); otherwise every pair that has its own system (D-042) - the
        # all-pairs block is then one line, not a "supervisor : not running" block that reads like a problem
        $allPairs = @(& $py -m tradingsystem run --status)
        if ($LASTEXITCODE -eq 0) {
            $allPairs | ForEach-Object { Write-Host $_ }
        } else {
            $pairs = @(& $py -m tradingsystem config --instances | Where-Object { $_.Trim() })
            $own = @($pairs | Where-Object { Test-Path (Join-Path $root "data\instances\$_\app.db") })
            if ($own.Count -gt 0) {
                Write-Host "all-pairs system: not in use (one system per pair below)"
                foreach ($p in $own) {
                    Write-Host ""
                    & $py -m tradingsystem run --status --instance $p
                }
            } else { $allPairs | ForEach-Object { Write-Host $_ } }
        }
    } finally { Pop-Location }
}

Write-Host ""
if ($script:todo) { Write-Host "$($script:todo) setting(s) to fix - commands above, details in docs\ops_windows.md" }
else { Write-Host "all checked settings look right" }
