# Read-only check: is the miner (H12) gone? Safe to run any time, no administrator needed for most checks.
$BAD = @('C:\ProgramData\WindowsTask', 'C:\ProgramData\ReaItekHD')
function IsBad($p) { if (-not $p) { return $false }; foreach ($b in $BAD) { if ($p -like "$b*") { return $true } }; return $false }
$bad = 0
Write-Host "== processes from the miner folders"
$p = Get-CimInstance Win32_Process | Where-Object { IsBad $_.ExecutablePath -or $_.CommandLine -match 'stratum\+tcp' }
if ($p) { $p | ForEach-Object { Write-Host "  !! $($_.ProcessId) $($_.ExecutablePath)  $($_.CommandLine)" -ForegroundColor Red; $bad++ } } else { Write-Host "  ok none" -ForegroundColor Green }
Write-Host "== folders"
foreach ($b in $BAD) { if (Test-Path $b) { Write-Host "  !! still present: $b" -ForegroundColor Red; $bad++ } else { Write-Host "  ok gone: $b" -ForegroundColor Green } }
Write-Host "== autostart registry entries"
$hit = $false
foreach ($k in 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run', 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run', 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run', 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce', 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce') {
    if (-not (Test-Path $k)) { continue }
    foreach ($prop in (Get-ItemProperty $k).PSObject.Properties) { if ($prop.Name -notlike 'PS*' -and (IsBad ([string]$prop.Value))) { Write-Host "  !! $k\$($prop.Name) = $($prop.Value)" -ForegroundColor Red; $hit = $true; $bad++ } }
}
if (-not $hit) { Write-Host "  ok none" -ForegroundColor Green }
Write-Host "== scheduled tasks / services / drivers"
$hit = $false
foreach ($t in (Get-ScheduledTask -ErrorAction SilentlyContinue)) { foreach ($a in $t.Actions) { if ((IsBad $a.Execute) -or ($a.Arguments -match 'WindowsTask|ReaItekHD')) { Write-Host "  !! task $($t.TaskPath)$($t.TaskName)" -ForegroundColor Red; $hit = $true; $bad++ } } }
foreach ($s in Get-CimInstance Win32_Service) { if ((IsBad $s.PathName) -or $s.PathName -match 'WinRing0') { Write-Host "  !! service $($s.Name) $($s.PathName)" -ForegroundColor Red; $hit = $true; $bad++ } }
foreach ($d in Get-CimInstance Win32_SystemDriver) { if ((IsBad $d.PathName) -or $d.PathName -match 'WinRing0') { Write-Host "  !! driver $($d.Name) $($d.PathName)" -ForegroundColor Red; $hit = $true; $bad++ } }
if (-not $hit) { Write-Host "  ok none" -ForegroundColor Green }
Write-Host "== Defender"
try { $m = Get-MpComputerStatus; Write-Host ("  real-time protection: {0}; last full scan: {1}; signatures: {2}" -f $m.RealTimeProtectionEnabled, $m.FullScanEndTime, $m.AntivirusSignatureLastUpdated) } catch { Write-Host "  (status not readable)" }
$os = Get-CimInstance Win32_OperatingSystem
Write-Host ("== free RAM: {0} MB of {1} MB" -f [int]($os.FreePhysicalMemory/1024), [int]($os.TotalVisibleMemorySize/1024))
if (Test-Path 'C:\ProgramData\KMSAuto') { Write-Host "== note: C:\ProgramData\KMSAuto still exists (see cleanup_miner.ps1 note)" -ForegroundColor Yellow }
Write-Host ""
if ($bad -eq 0) { Write-Host "RESULT: CLEAN (as far as these checks go) - finish with the Defender Offline scan." -ForegroundColor Green } else { Write-Host "RESULT: $bad item(s) left - run cleanup_miner.bat as administrator again after a reboot." -ForegroundColor Red }
