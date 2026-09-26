# Remove the crypto-miner found on this laptop on 2026-09-26 (see PROJECT_STATUS.md H12).
# Run AS ADMINISTRATOR (double-click scripts\cleanup_miner.bat). Everything it does is logged to
# logs\cleanup_miner.log. Files are MOVED to C:\ProgramData\_quarantine_<date> (not deleted), so nothing is lost
# if something turns out to be legitimate; delete the quarantine folder yourself after a clean scan.
#
# What it does: 1) stops every process running from the miner folders, 2) removes Defender exclusions that hide
# them, 3) removes their autostart entries (registry Run keys, scheduled tasks, services/drivers), 4) quarantines
# the folders, 5) updates Defender and starts a full scan, 6) prints what is left. It does NOT touch KMSAuto
# (see the end of the output) and does NOT delete your own programs.
$ErrorActionPreference = 'Continue'
$MINER_DIRS = @('C:\ProgramData\WindowsTask', 'C:\ProgramData\ReaItekHD')
$root = Split-Path -Parent $PSScriptRoot
$log = Join-Path $root 'logs\cleanup_miner.log'
New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null
function Say($m) { $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m; Write-Host $line; Add-Content $log $line }
function IsBad($p) { if (-not $p) { return $false }; foreach ($b in $MINER_DIRS) { if ($p -like "$b*") { return $true } }; return $false }

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "This must run as administrator: right-click scripts\cleanup_miner.bat -> Run as administrator" -ForegroundColor Red
    exit 1
}
Say "=== cleanup start (admin) ==="

# 1. stop the miner processes (matched by PATH, never by name: audiodg.exe / taskhost.exe also exist legitimately)
$procs = @(Get-CimInstance Win32_Process | Where-Object { (IsBad $_.ExecutablePath) -or ($_.CommandLine -match 'stratum\+tcp|WindowsTask|ReaItekHD') })
foreach ($p in $procs) {
    try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; Say "STOPPED  pid $($p.ProcessId) $($p.ExecutablePath)" }
    catch { Say "FAILED to stop pid $($p.ProcessId) $($p.ExecutablePath): $($_.Exception.Message)" }
}
if (-not $procs) { Say "no miner process running" }
Start-Sleep -Seconds 2

# 2. Defender exclusions that hide the folders
try {
    $pref = Get-MpPreference
    foreach ($e in @($pref.ExclusionPath)) { if (IsBad $e) { Remove-MpPreference -ExclusionPath $e; Say "REMOVED Defender path exclusion $e" } }
    foreach ($e in @($pref.ExclusionProcess)) { if ((IsBad $e) -or ($e -match 'MicrosoftHost|AppHost|taskhostw')) { Remove-MpPreference -ExclusionProcess $e; Say "REMOVED Defender process exclusion $e" } }
    if (-not @($pref.ExclusionPath) -and -not @($pref.ExclusionProcess)) { Say "no Defender exclusions found" }
} catch { Say "Defender preferences not readable: $($_.Exception.Message)" }

# 3a. registry autostart entries pointing into the miner folders
foreach ($k in 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run', 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce',
               'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run', 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run',
               'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce') {
    if (-not (Test-Path $k)) { continue }
    $item = Get-ItemProperty $k
    foreach ($prop in $item.PSObject.Properties) {
        if ($prop.Name -like 'PS*') { continue }
        if (IsBad ([string]$prop.Value)) {
            Remove-ItemProperty -Path $k -Name $prop.Name
            Say "REMOVED autostart $k\$($prop.Name) = $($prop.Value)"
        }
    }
}

# 3b. scheduled tasks whose actions run from the miner folders (all tasks are visible as admin)
foreach ($t in Get-ScheduledTask) {
    $hit = $false
    foreach ($a in $t.Actions) { if ((IsBad $a.Execute) -or (IsBad $a.Arguments) -or ($a.Arguments -match 'WindowsTask|ReaItekHD')) { $hit = $true } }
    if ($hit) { Unregister-ScheduledTask -TaskName $t.TaskName -TaskPath $t.TaskPath -Confirm:$false; Say "REMOVED scheduled task $($t.TaskPath)$($t.TaskName)" }
}

# 3c. services and drivers installed from the miner folders (e.g. the WinRing0 driver)
foreach ($s in Get-CimInstance Win32_Service) { if ((IsBad $s.PathName) -or ($s.PathName -match 'WinRing0')) { sc.exe stop $s.Name | Out-Null; sc.exe delete $s.Name | Out-Null; Say "REMOVED service $($s.Name) ($($s.PathName))" } }
foreach ($d in Get-CimInstance Win32_SystemDriver) { if ((IsBad $d.PathName) -or ($d.PathName -match 'WinRing0')) { sc.exe stop $d.Name | Out-Null; sc.exe delete $d.Name | Out-Null; Say "REMOVED driver $($d.Name) ($($d.PathName))" } }
foreach ($f in Get-ChildItem "$env:SystemRoot\System32\drivers" -Filter 'WinRing0*' -ErrorAction SilentlyContinue) { try { Move-Item $f.FullName "$($f.FullName).quarantined" -Force; Say "QUARANTINED driver file $($f.FullName)" } catch { Say "FAILED to move $($f.FullName): $($_.Exception.Message)" } }

# 4. quarantine the folders (move, not delete)
$q = "C:\ProgramData\_quarantine_$(Get-Date -Format 'yyyyMMdd_HHmm')"
New-Item -ItemType Directory -Force $q | Out-Null
foreach ($b in $MINER_DIRS) {
    if (Test-Path $b) {
        try { Move-Item $b (Join-Path $q (Split-Path $b -Leaf)) -Force -ErrorAction Stop; Say "QUARANTINED $b -> $q" }
        catch {
            Say "could not move the whole folder ($($_.Exception.Message)); moving file by file"
            $dest = Join-Path $q (Split-Path $b -Leaf); New-Item -ItemType Directory -Force $dest | Out-Null
            foreach ($f in Get-ChildItem $b -Force) {
                try { Move-Item $f.FullName $dest -Force -ErrorAction Stop; Say "  moved $($f.Name)" }
                catch { Say "  FAILED $($f.Name): $($_.Exception.Message)  (a loaded driver: it is released after the reboot; run this script once more then)" }
            }
        }
    } else { Say "already gone: $b" }
}

# 5. Defender: fresh signatures + full scan in the background
try { Update-MpSignature -ErrorAction Stop; Say "Defender signatures updated" } catch { Say "signature update failed: $($_.Exception.Message)" }
try { Start-MpScan -ScanType FullScan -AsJob | Out-Null; Say "Defender FULL scan started in the background (1-2 hours; the PC may be slow meanwhile)" } catch { Say "could not start the scan: $($_.Exception.Message)" }

# 6. what is left
Say "--- verification ---"
$left = @(Get-CimInstance Win32_Process | Where-Object { (IsBad $_.ExecutablePath) -or ($_.CommandLine -match 'stratum\+tcp') })
if ($left) { foreach ($p in $left) { Say "STILL RUNNING pid $($p.ProcessId) $($p.ExecutablePath)" } } else { Say "OK no miner process running" }
foreach ($b in $MINER_DIRS) { if (Test-Path $b) { Say "STILL PRESENT $b" } else { Say "OK folder gone $b" } }
$os = Get-CimInstance Win32_OperatingSystem
Say ("free RAM now: {0} MB of {1} MB" -f [int]($os.FreePhysicalMemory/1024), [int]($os.TotalVisibleMemorySize/1024))
if (Test-Path 'C:\ProgramData\KMSAuto') {
    Say "NOTE: C:\ProgramData\KMSAuto exists (a pirated Windows/Office activation tool; such bundles are the usual carrier of this miner)."
    Say "      This script did NOT touch it. Removing it may un-activate a Windows/Office copy activated with it - your decision."
}
Say "NEXT: reboot, then run scripts\check_miner.bat; then Windows Security -> Virus & threat protection -> Scan options -> Microsoft Defender Offline scan."
Say "=== cleanup end ==="
