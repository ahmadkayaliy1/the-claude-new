<#
.SYNOPSIS
    Thin launcher of one operator session for Task Scheduler (docs\operator_sessions.md):
        powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File tools\operator\run_session.ps1 -Kind daily
.DESCRIPTION
    Everything happens in tools\operator\run_session.py (Python): PowerShell 5.1 drops empty arguments, pipes stdin
    as ASCII and decodes native output with the OEM code page, so this script only finds the project's venv (two
    levels up from this file - never a hard-coded path), sets UTF-8 for Python and passes the kind on.
    Exit code = run_session.py's: 0 finished or dry run, 1 failed, 2 not run (disabled, busy, usage gauge).
.PARAMETER Kind
    daily | weekly | diagnose
.PARAMETER DryRun
    Build the review pack and print the exact command; no Claude call.
#>
[CmdletBinding()]
param(
    [ValidateSet("daily", "weekly", "diagnose")]
    [string]$Kind = "daily",
    [switch]$DryRun
)
$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "Python venv not found: $py"
    exit 1
}
$script = Join-Path $PSScriptRoot "run_session.py"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
$argv = @($script, "--kind", $Kind)
if ($DryRun) { $argv += "--dry-run" }
Push-Location $root
try {
    & $py @argv
    $rc = $LASTEXITCODE
} finally { Pop-Location }
exit $rc
