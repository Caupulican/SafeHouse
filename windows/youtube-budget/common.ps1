<#
SafeHouse - YouTube watch-time budget: shared helpers (Windows-native).

Dot-sourced by youtube-budget.ps1 (the watcher) and youtube-budget-ctl.ps1 (the
parent CLI). Provides: paths/config, local-date helper, atomic JSON state I/O, the
byte-preserving hosts block/unblock splice (mirrors windows\parental-toggle.ps1),
DNS-cache YouTube detection, and DNS flush. No side effects on import.

State + config + log live under C:\ProgramData\SafeHouse (created with a locked-down
ACL by install-task.ps1: SYSTEM + Administrators full, Users read-only). For testing
without elevation, set $env:YB_ROOT (state/config/log root) and/or $env:YB_HOSTS
(hosts file) to point at temp copies - every helper honours those overrides.

Marker convention (shared with windows\parental-toggle.ps1 and compatible with
C:\Users\Public\ParentalControls\parental-undo.ps1):
  start: # === Parental block: YouTube (auto: daily budget) ===
  end:   # === end YouTube block ===
#>

# --- locations ---------------------------------------------------------------
$script:YbRoot       = if ($env:YB_ROOT) { $env:YB_ROOT } else { 'C:\ProgramData\SafeHouse' }
$script:YbStatePath  = Join-Path $script:YbRoot 'youtube-budget.json'
$script:YbConfigPath = Join-Path $script:YbRoot 'config.json'
$script:YbLogPath    = Join-Path $script:YbRoot 'youtube-budget.log'
$script:YbScriptDir  = Split-Path -Parent $PSCommandPath

# --- hosts marker + enforcement constants ------------------------------------
$script:YbBlockName   = 'YouTube'
$script:YbStartMarker = '# === Parental block: YouTube (auto: daily budget) ==='
$script:YbEndMarker   = '# === end YouTube block ==='
$script:YbUtf8NoBom   = New-Object System.Text.UTF8Encoding($false)

# YouTube *content* hosts whose live (non-blocked) resolution means "watching".
# Matched as DNS suffixes (E or *.E), anchored so nsfwyoutube.com etc. never match.
$script:YbContentDomains = @(
  'googlevideo.com',          # the video-stream CDN - the strongest "is watching" signal
  'youtube.com',              # site + API surface
  'youtubei.googleapis.com',  # InnerTube API (playback / next / player calls)
  'ytimg.com',                # thumbnails / static (i.ytimg.com, s.ytimg.com, ...)
  'youtu.be',                 # short links
  'yt3.ggpht.com'             # channel avatars / images
)

# --- hosts path (override with $env:YB_HOSTS for testing on a copy) ----------
function Get-YbHostsPath {
  if ($env:YB_HOSTS) { return $env:YB_HOSTS }
  return (Join-Path $env:SystemRoot 'System32\drivers\etc\hosts')
}

# --- enforcement host list (repo layout OR flat C:\SafeHouse layout) ---------
function Find-YbBlockList {
  $candidates = @(
    (Join-Path $script:YbScriptDir 'parental-blocks\youtube.txt'),
    (Join-Path $script:YbScriptDir '..\parental-blocks\youtube.txt'),
    (Join-Path $script:YbScriptDir '..\windows\parental-blocks\youtube.txt')
  )
  foreach ($c in $candidates) { if (Test-Path $c) { return (Resolve-Path $c).Path } }
  return $null
}

# --- logging (best-effort; never throws) -------------------------------------
function Write-YbLog([string]$Message) {
  try {
    $dir = Split-Path -Parent $script:YbLogPath
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    $line = ('{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message)
    Add-Content -LiteralPath $script:YbLogPath -Value $line -Encoding UTF8
  } catch { }
}

# Trim the log if it grows past ~2 MB (one .1 archive). Call once at startup.
function Limit-YbLog([int]$MaxBytes = 2097152) {
  try {
    if (Test-Path $script:YbLogPath) {
      $len = (Get-Item -LiteralPath $script:YbLogPath).Length
      if ($len -gt $MaxBytes) {
        Move-Item -LiteralPath $script:YbLogPath -Destination ($script:YbLogPath + '.1') -Force
      }
    }
  } catch { }
}

# --- local date for the daily reset ------------------------------------------
function Get-YbToday { (Get-Date).ToString('yyyy-MM-dd') }

# --- config ------------------------------------------------------------------
# Returns @{ limit_min; sample_sec; window_sec } from ProgramData\config.json,
# then a script-local config.json, then hard defaults.
function Get-YbConfig {
  $cfg = @{ limit_min = 60; sample_sec = 20; window_sec = 240 }
  $paths = @($script:YbConfigPath, (Join-Path $script:YbScriptDir 'config.json'))
  foreach ($p in $paths) {
    if ($p -and (Test-Path $p)) {
      try {
        $o = Get-Content -Raw -LiteralPath $p | ConvertFrom-Json
        if ($null -ne $o.limit_min)  { $cfg.limit_min  = [int]$o.limit_min }
        if ($null -ne $o.sample_sec) { $cfg.sample_sec = [int]$o.sample_sec }
        if ($null -ne $o.window_sec) { $cfg.window_sec = [int]$o.window_sec }
        break
      } catch { }
    }
  }
  if ($cfg.limit_min  -lt 0) { $cfg.limit_min  = 0 }
  if ($cfg.sample_sec -lt 1) { $cfg.sample_sec = 20 }
  if ($cfg.window_sec -lt $cfg.sample_sec) { $cfg.window_sec = $cfg.sample_sec }
  return $cfg
}

# --- state (atomic JSON) -----------------------------------------------------
# Read-YbState -> hashtable, or $null if absent/corrupt.
function Read-YbState {
  if (-not (Test-Path $script:YbStatePath)) { return $null }
  try { $o = Get-Content -Raw -LiteralPath $script:YbStatePath | ConvertFrom-Json } catch { return $null }
  if ($null -eq $o) { return $null }
  return @{
    date              = [string]$o.date
    seconds_used      = [int]$o.seconds_used
    limit_min         = [int]$o.limit_min
    bonus_sec         = [int]$o.bonus_sec
    blocked_by_budget = [bool]$o.blocked_by_budget
  }
}

# Write-YbState $hashtable  -> atomic temp-write + replace.
function Write-YbState($State) {
  $dir = Split-Path -Parent $script:YbStatePath
  if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
  $obj = [ordered]@{
    date              = [string]$State.date
    seconds_used      = [int]$State.seconds_used
    limit_min         = [int]$State.limit_min
    bonus_sec         = [int]$State.bonus_sec
    blocked_by_budget = [bool]$State.blocked_by_budget
  }
  $json = ($obj | ConvertTo-Json)
  $tmp  = Join-Path $dir ('.ybstate.' + [System.IO.Path]::GetRandomFileName())
  [System.IO.File]::WriteAllText($tmp, ($json + "`r`n"), $script:YbUtf8NoBom)
  # Atomic-enough replace via a same-volume rename. (.NET File.Replace is unreliable
  # on some Windows builds; Move-Item -Force is the robust idiom and overwrites.)
  Move-Item -LiteralPath $tmp -Destination $script:YbStatePath -Force
}

# --- hosts enforcement (byte-preserving splice; mirrors parental-toggle.ps1) --
# True if ANY "# === Parental block: YouTube ... ===" section (manual or auto)
# is present in the hosts file.
function Test-YbHostsBlocked([string]$HostsPath) {
  if (-not (Test-Path $HostsPath)) { return $false }
  $raw = [System.IO.File]::ReadAllText($HostsPath, $script:YbUtf8NoBom)
  return [regex]::IsMatch($raw, '(?m)^# === Parental block: YouTube\b')
}

# Insert the auto YouTube section if no YouTube section exists yet.
# Returns 'BLOCKED:<n>' | 'ALREADY' | 'NOLIST'. Byte-preserving: only this section
# is spliced; the file's mixed CRLF header / LF body is otherwise untouched.
function Add-YbHostsBlock([string]$HostsPath) {
  if (-not (Test-Path $HostsPath)) { return 'NOLIST' }
  $raw = [System.IO.File]::ReadAllText($HostsPath, $script:YbUtf8NoBom)
  if ([regex]::IsMatch($raw, '(?m)^# === Parental block: YouTube\b')) { return 'ALREADY' }
  $listPath = Find-YbBlockList
  if (-not $listPath) { return 'NOLIST' }
  $hosts = @()
  foreach ($line in [System.IO.File]::ReadAllLines($listPath, $script:YbUtf8NoBom)) {
    $h = ($line -replace '#.*$', '').Trim()
    if ($h) { $hosts += $h }
  }
  if (-not $hosts) { return 'NOLIST' }
  $nl = "`n"
  if ($raw.Length -gt 0 -and -not $raw.EndsWith("`n")) { $raw += $nl }
  $section = $nl + $script:YbStartMarker + $nl
  foreach ($h in $hosts) { $section += "0.0.0.0 $h" + $nl }
  $section += $script:YbEndMarker + $nl
  [System.IO.File]::WriteAllText($HostsPath, ($raw + $section), $script:YbUtf8NoBom)
  return ('BLOCKED:{0}' -f $hosts.Count)
}

# Remove the YouTube section. Scope 'auto' (default) removes only the
# "(auto: daily budget)" section this watcher owns; scope 'any' removes any
# "Parental block: YouTube ..." section (a parent override). Returns
# 'UNBLOCKED' | 'NONE'. Byte-preserving (mirrors parental-toggle.ps1 -Allow).
function Remove-YbHostsBlock([string]$HostsPath, [string]$Scope = 'auto') {
  if (-not (Test-Path $HostsPath)) { return 'NONE' }
  $raw = [System.IO.File]::ReadAllText($HostsPath, $script:YbUtf8NoBom)
  if ($Scope -eq 'auto') {
    $startPat = '# === Parental block: YouTube \(auto: daily budget\) ==='
  } else {
    $startPat = '# === Parental block: YouTube\b[^\r\n]*'
  }
  $removePat = "(?ms)(?:\r?\n)?^$startPat.*?^# === end YouTube block ===[^\r\n]*(?:\r?\n)?"
  $new = [regex]::Replace($raw, $removePat, '')
  if ($new -eq $raw) { return 'NONE' }
  [System.IO.File]::WriteAllText($HostsPath, $new, $script:YbUtf8NoBom)
  return 'UNBLOCKED'
}

# --- DNS flush (best-effort) -------------------------------------------------
function Invoke-YbFlushDns {
  try { & ipconfig /flushdns | Out-Null } catch { }
}

# --- detection (Windows DNS Client cache) ------------------------------------
# Returns the list of currently-cached YouTube *content* entries that represent a
# live (non-blocked) resolution: @( @{ Entry=<name>; TTL=<sec> }, ... ).
#   * $null  => the DNS-cache query itself failed (caller skips the sample).
#   * @()    => idle (no YouTube content resolved within its TTL).
# Browser DoH is forced OFF by machine policy, so YouTube name lookups go through
# the Windows resolver and land here regardless of which browser/app is used.
function Get-YbYouTubeCacheHits {
  $suffixPattern = '(^|\.)(' +
    (($script:YbContentDomains | ForEach-Object { [regex]::Escape($_) }) -join '|') + ')$'
  $hits = @()
  $any  = $false
  foreach ($dom in $script:YbContentDomains) {
    try {
      $rows = Get-DnsClientCache -Entry ('*' + $dom) -ErrorAction Stop
    } catch {
      return $null   # cmdlet/service failure -> signal "skip this sample"
    }
    $any = $true
    foreach ($e in $rows) {
      $name = [string]$e.Entry
      if ([string]::IsNullOrWhiteSpace($name)) { continue }
      $name = $name.ToLower()
      if ($name -notmatch $suffixPattern) { continue }       # anchored: excludes nsfwyoutube.com
      if ([int]$e.Status -ne 0) { continue }                 # only successful resolutions
      if ([int]$e.TimeToLive -le 0) { continue }             # still within TTL
      $data = [string]$e.Data
      if ($data -eq '0.0.0.0' -or $data -eq '::' -or [string]::IsNullOrWhiteSpace($data)) { continue } # blocked/hosts entry
      $hits += [pscustomobject]@{ Entry = $name; TTL = [int]$e.TimeToLive }
    }
  }
  if (-not $any) { return $null }
  return ,$hits
}
