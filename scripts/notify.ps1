<#
.SYNOPSIS
    Shows one Windows toast for the trading system's notifier (Phase 4, src\tradingsystem\core\notify.py).
    Not meant to be run by hand - send a notification with:
        .venv\Scripts\python.exe tools\notify.py --level info --title "Test" --text "Hello"

.DESCRIPTION
    Title and text arrive base64-encoded (UTF-8): Windows PowerShell 5.1 mangles native arguments that contain
    double quotes, newlines or non-ASCII characters. Both are XML-escaped into a ToastGeneric template and shown
    through the WinRT ToastNotificationManager under the Windows PowerShell AppUserModelID, which every Windows
    10/11 install registers - no module, shortcut or registry entry is needed. The toast appears as
    "Windows PowerShell" in the notification centre; Windows' own settings (Do not disturb, per-app notifications)
    apply. Details: docs\notifications.md

    Exit code 0 shown (or built, with -Check), 1 failed (the reason on stderr; the notifier logs it).

.PARAMETER TitleB64
    The title, UTF-8 then base64.
.PARAMETER TextB64
    The text, UTF-8 then base64.
.PARAMETER Level
    info | warn | critical. A critical toast stays on screen longer.
.PARAMETER Check
    Build the toast and the notifier but do not show it: print the toast XML instead (a self-test).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$TitleB64,
    [Parameter(Mandatory = $true)][string]$TextB64,
    [ValidateSet("info", "warn", "critical")][string]$Level = "info",
    [switch]$Check
)

$ErrorActionPreference = "Stop"
$AppId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'

function ConvertFrom-B64([string]$Value, [int]$Max) {
    $s = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($Value))
    # XML 1.0 forbids most control characters (tab, CR and LF stay)
    $s = [regex]::Replace($s, '[\x00-\x08\x0B\x0C\x0E-\x1F]', '')
    if ($s.Length -gt $Max) { $s = $s.Substring(0, $Max - 3) + '...' }
    return [System.Security.SecurityElement]::Escape($s)
}

try {
    $title = ConvertFrom-B64 $TitleB64 250
    $text = ConvertFrom-B64 $TextB64 1000
    $null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
    $null = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
    $duration = 'short'
    if ($Level -eq 'critical') { $duration = 'long' }
    $xml = '<toast duration="' + $duration + '"><visual><binding template="ToastGeneric">' +
           '<text>' + $title + '</text><text>' + $text + '</text></binding></visual></toast>'
    $doc = New-Object Windows.Data.Xml.Dom.XmlDocument
    $doc.LoadXml($xml)
    $toast = New-Object Windows.UI.Notifications.ToastNotification $doc
    $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($AppId)
    if ($Check) {
        # the self-test prints the toast XML (UTF-8) instead of showing it
        [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
        [Console]::Out.WriteLine($xml)
    } else {
        $notifier.Show($toast)
    }
    exit 0
} catch {
    [Console]::Error.WriteLine('toast failed: ' + $_.Exception.Message)
    exit 1
}
