<#
SafeHouse parental block toggle — on-demand hosts-layer enforcement.

Toggles a named parental block (default: youtube) on or off in the live Windows
hosts file. The hostnames for each block live in `parental-blocks\<Name>.txt`
(one host per line, '#' comments allowed), resolved relative to this script so it
works both from the repo layout (windows\parental-toggle.ps1 +
windows\parental-blocks\youtube.txt) and from the flat staged layout
(C:\SafeHouse\windows\...).

  -Block : append a marked "# === Parental block: <DisplayName> ... ===" section
           (one `0.0.0.0 <host>` line per host) if it is not already present.
           Idempotent — running it twice does not duplicate the section.
  -Allow : remove that section (start marker through end marker) if present.
           Idempotent.
  (no switch) : print whether <Name> is currently blocked or allowed.

Marker convention (compatible with C:\Users\Public\ParentalControls\parental-undo.ps1,
which matches start `=== Parental block:` and end `=== end .* block ===`):
  start: # === Parental block: <DisplayName> (toggled via parental-toggle.ps1) ===
  end:   # === end <DisplayName> block ===

The hosts file has MIXED line endings (CRLF header + LF body). This script reads
the file as one raw string and only splices the target section in/out, writing the
result back as UTF-8 without BOM. Untouched lines keep their exact bytes — it never
re-encodes the body, so CRLF stays CRLF and LF stays LF.

Usage (run from a normal or elevated PowerShell — it self-elevates for -Block/-Allow):
  .\parental-toggle.ps1 -Name youtube -Block    # apply the YouTube block
  .\parental-toggle.ps1 -Name youtube -Allow    # remove the YouTube block
  .\parental-toggle.ps1 -Name youtube           # show status (default Name = youtube)
#>

[CmdletBinding()]
param(
  [string]$Name = 'youtube',
  [switch]$Block,
  [switch]$Allow,
  [string]$HostsPath
)

$ErrorActionPreference = 'Stop'

if ($Block -and $Allow) {
  Write-Error '-Block and -Allow are mutually exclusive. Pass only one (or neither for status).'
  return
}

# --- self-elevate (only when we actually modify the hosts file) -------------
if (($Block -or $Allow)) {
  $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
  if (-not $principal.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
    Write-Host 'Not elevated. Relaunching with admin rights (approve the UAC prompt)...'
    $a = @('-NoProfile','-ExecutionPolicy','Bypass','-NoExit','-File', ('"{0}"' -f $PSCommandPath))
    if ($Name)      { $a += @('-Name', ('"{0}"' -f $Name)) }
    if ($Block)     { $a += '-Block' }
    if ($Allow)     { $a += '-Allow' }
    if ($HostsPath) { $a += @('-HostsPath', ('"{0}"' -f $HostsPath)) }
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $a
    return
  }
}

# --- resolve paths (repo layout OR flat C:\SafeHouse\windows layout) ---------
$scriptDir = Split-Path -Parent $PSCommandPath
if (-not $HostsPath) { $HostsPath = Join-Path $env:SystemRoot 'System32\drivers\etc\hosts' }

function Find-BlockList([string]$name) {
  $candidates = @(
    (Join-Path $scriptDir (Join-Path 'parental-blocks' "$name.txt")),
    (Join-Path $scriptDir (Join-Path '..' (Join-Path 'parental-blocks' "$name.txt"))),
    (Join-Path $scriptDir (Join-Path '..' (Join-Path 'windows' (Join-Path 'parental-blocks' "$name.txt"))))
  )
  foreach ($c in $candidates) { if (Test-Path $c) { return (Resolve-Path $c).Path } }
  return $null
}

# --- display name (youtube -> YouTube; otherwise title-case) -----------------
$known = @{ youtube = 'YouTube' }
if ($known.ContainsKey($Name.ToLower())) {
  $DisplayName = $known[$Name.ToLower()]
} else {
  $DisplayName = (Get-Culture).TextInfo.ToTitleCase($Name.ToLower())
}

$startMarker = "# === Parental block: $DisplayName (toggled via parental-toggle.ps1) ==="
$endMarker   = "# === end $DisplayName block ==="

# Detect an existing section for this DisplayName (any start marker variant) and
# build the removal pattern (start marker through end marker), tolerant of an
# optional leading blank-line separator and CRLF/LF.
$dn = [regex]::Escape($DisplayName)
$detectPattern = "(?m)^# === Parental block: $dn\b"
$removePattern = "(?ms)(?:\r?\n)?^# === Parental block: $dn\b.*?^# === end $dn block ===[^\r\n]*(?:\r?\n)?"

# --- read the hosts file as one raw string (no newline translation) ----------
if (-not (Test-Path $HostsPath)) { Write-Error "Hosts file not found: $HostsPath"; return }
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$raw = [System.IO.File]::ReadAllText($HostsPath, $utf8NoBom)
$isBlocked = [regex]::IsMatch($raw, $detectPattern)

function Flush-Dns {
  try { & ipconfig /flushdns | Out-Null } catch {}
  Write-Host 'Flushed DNS cache. Reload the browser/app (a hard refresh) for the change to take effect.' -ForegroundColor Cyan
}

# --- status (no switch) ------------------------------------------------------
if (-not $Block -and -not $Allow) {
  if ($isBlocked) {
    Write-Host ("[$DisplayName] BLOCKED — the hosts section is present in $HostsPath.") -ForegroundColor Yellow
  } else {
    Write-Host ("[$DisplayName] ALLOWED — no hosts section present in $HostsPath.") -ForegroundColor Green
  }
  return
}

# --- block -------------------------------------------------------------------
if ($Block) {
  if ($isBlocked) {
    Write-Host ("[$DisplayName] already BLOCKED — nothing to do (idempotent).") -ForegroundColor Yellow
    return
  }
  $listPath = Find-BlockList $Name
  if (-not $listPath) { Write-Error "Block list not found: parental-blocks\$Name.txt (looked relative to $scriptDir)"; return }
  $hosts = @()
  foreach ($line in [System.IO.File]::ReadAllLines($listPath, $utf8NoBom)) {
    $h = ($line -replace '#.*$','').Trim()
    if ($h) { $hosts += $h }
  }
  if (-not $hosts) { Write-Error "No hostnames found in $listPath"; return }

  # Body uses LF. Build the section with a leading blank-line separator and a
  # trailing newline, matching the existing block convention, then append it.
  $nl = "`n"
  if ($raw.Length -gt 0 -and -not $raw.EndsWith("`n")) { $raw += $nl }
  $section = $nl + $startMarker + $nl
  foreach ($h in $hosts) { $section += "0.0.0.0 $h" + $nl }
  $section += $endMarker + $nl
  $new = $raw + $section

  [System.IO.File]::WriteAllText($HostsPath, $new, $utf8NoBom)
  Write-Host ("[$DisplayName] BLOCKED — added {0} host(s) to $HostsPath." -f $hosts.Count) -ForegroundColor Yellow
  Flush-Dns
  return
}

# --- allow -------------------------------------------------------------------
if ($Allow) {
  if (-not $isBlocked) {
    Write-Host ("[$DisplayName] already ALLOWED — no section to remove (idempotent).") -ForegroundColor Green
    return
  }
  $new = [regex]::Replace($raw, $removePattern, '')
  [System.IO.File]::WriteAllText($HostsPath, $new, $utf8NoBom)
  Write-Host ("[$DisplayName] ALLOWED — removed the hosts section from $HostsPath.") -ForegroundColor Green
  Flush-Dns
  return
}
