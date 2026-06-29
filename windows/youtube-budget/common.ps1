<#
SafeHouse - YouTube watch-time budget: shared helpers (Windows-native).

Dot-sourced by youtube-budget.ps1 (the watcher) and youtube-budget-ctl.ps1 (the
parent CLI). Provides: paths/config, local-date helper, atomic JSON state I/O, the
byte-preserving hosts block/unblock splice (mirrors windows\parental-toggle.ps1),
connection-flow watch detection (pktmon byte metering to Google's IP ranges, gated
by a real-time DNS-Client ETW signal), and DNS flush. No side effects on import.

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
$script:YbGoogPath   = Join-Path $script:YbRoot 'goog-ranges.json'   # cached Google IP prefixes
$script:YbScriptDir  = Split-Path -Parent $PSCommandPath

# --- connection-flow metering: tools + constants -----------------------------
$script:YbPktMon       = Join-Path $env:SystemRoot 'System32\PktMon.exe'   # byte meter (counts UDP/QUIC too)
$script:YbGoogUrl      = 'https://www.gstatic.com/ipranges/goog.json'      # Google's published serving prefixes
$script:YbGoogMaxAgeD  = 7            # refresh the cached prefixes if older than this many days
$script:YbPktFilterTag = 'SafeHouseYB'  # name stamped on every pktmon filter we own
$script:YbMaxFilters   = 32           # pktmon allows up to 32 active filters at once
$script:YbDnsChannel   = 'Microsoft-Windows-DNS-Client/Operational'        # real-time DNS gate signal

# --- hosts marker + enforcement constants ------------------------------------
$script:YbBlockName   = 'YouTube'
$script:YbStartMarker = '# === Parental block: YouTube (auto: daily budget) ==='
$script:YbEndMarker   = '# === end YouTube block ==='
$script:YbUtf8NoBom   = New-Object System.Text.UTF8Encoding($false)

# YouTube-family DNS names used as the GATE: a recent real-time resolution of any
# of these is what separates "YouTube bytes" from generic Google bulk traffic
# (Drive sync, Edge update, Play-games VM) that also rides Google's IP ranges.
# Matched as DNS suffixes (E or *.E), anchored so nsfwyoutube.com etc. never match.
$script:YbContentDomains = @(
  'googlevideo.com',          # the video-stream CDN - the strongest watch signal
  'youtube.com',              # site + API surface
  'youtubei.googleapis.com',  # InnerTube API (playback / next / player calls)
  'ytimg.com',                # thumbnails / static (i.ytimg.com, s.ytimg.com, ...)
  'youtu.be',                 # short links
  'ggpht.com'                 # channel avatars / images (yt3.ggpht.com, ...)
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
# Returns @{ limit_min; sample_sec; window_sec; min_throughput_kbps; dns_gate_min }
# from ProgramData\config.json, then a script-local config.json, then hard defaults.
#   min_throughput_kbps - sustained inbound kbps to Google ranges that counts as
#                         video-like watching (default 64; tune up to ignore bursts).
#   dns_gate_min        - only count Google bytes when a YouTube-family DNS lookup
#                         happened within this many minutes (default 20; 0 disables
#                         the gate = throughput-only).
function Get-YbConfig {
  $cfg = @{ limit_min = 60; sample_sec = 20; window_sec = 240; min_throughput_kbps = 64; dns_gate_min = 20 }
  $paths = @($script:YbConfigPath, (Join-Path $script:YbScriptDir 'config.json'))
  foreach ($p in $paths) {
    if ($p -and (Test-Path $p)) {
      try {
        $o = Get-Content -Raw -LiteralPath $p | ConvertFrom-Json
        if ($null -ne $o.limit_min)           { $cfg.limit_min           = [int]$o.limit_min }
        if ($null -ne $o.sample_sec)          { $cfg.sample_sec          = [int]$o.sample_sec }
        if ($null -ne $o.window_sec)          { $cfg.window_sec          = [int]$o.window_sec }
        if ($null -ne $o.min_throughput_kbps) { $cfg.min_throughput_kbps = [int]$o.min_throughput_kbps }
        if ($null -ne $o.dns_gate_min)        { $cfg.dns_gate_min        = [int]$o.dns_gate_min }
        break
      } catch { }
    }
  }
  if ($cfg.limit_min  -lt 0) { $cfg.limit_min  = 0 }
  if ($cfg.sample_sec -lt 1) { $cfg.sample_sec = 20 }
  if ($cfg.window_sec -lt $cfg.sample_sec) { $cfg.window_sec = $cfg.sample_sec }
  if ($cfg.min_throughput_kbps -lt 0) { $cfg.min_throughput_kbps = 0 }
  if ($cfg.dns_gate_min -lt 0) { $cfg.dns_gate_min = 0 }
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

# --- detection: connection-flow metering (pktmon) + DNS gate -----------------
# Modern YouTube is invisible to DNS-cache polling: Edge reuses warm IPs and
# coalesces QUIC connections, so video streams over already-open Google connections
# with almost no fresh googlevideo.com lookups. We instead METER the actual inbound
# bytes to Google's serving IP ranges (which survive caching/coalescing) and GATE
# that on a recent real-time YouTube-family DNS resolution so generic Google bulk
# traffic (Drive, Edge-update, the Play-games VM) does not falsely count.

# Build the Google IP prefixes to meter (<= YbMaxFilters CIDRs). pktmon allows only
# 32 active filters but Google publishes ~110 prefixes, so we take ALL IPv6 prefixes
# (full coverage of the dual-stack path Google/YouTube actually uses on this box)
# plus the largest IPv4 blocks to fill the remaining slots (largest = most Google
# address space per slot). Fetches Google's published list to a cache, refreshing
# when older than YbGoogMaxAgeD days; on a fetch failure it reuses the cache.
# Returns an array of CIDR strings, or $null if neither network nor cache is usable.
function Get-YbGoogRanges {
  $needFetch = $true
  if (Test-Path $script:YbGoogPath) {
    $ageDays = ((Get-Date) - (Get-Item -LiteralPath $script:YbGoogPath).LastWriteTime).TotalDays
    if ($ageDays -lt $script:YbGoogMaxAgeD) { $needFetch = $false }
  }
  if ($needFetch) {
    try {
      $resp = Invoke-WebRequest -Uri $script:YbGoogUrl -UseBasicParsing -TimeoutSec 20
      $text = [string]$resp.Content
      $null = ($text | ConvertFrom-Json)   # validate before trusting/caching it
      [System.IO.File]::WriteAllText($script:YbGoogPath, $text, $script:YbUtf8NoBom)
    } catch { }                            # fall back to the cached copy below
  }
  if (-not (Test-Path $script:YbGoogPath)) { return $null }
  try { $obj = Get-Content -Raw -LiteralPath $script:YbGoogPath | ConvertFrom-Json } catch { return $null }
  if ($null -eq $obj -or $null -eq $obj.prefixes) { return $null }
  $v6 = @($obj.prefixes | Where-Object { $_.ipv6Prefix } | ForEach-Object { [string]$_.ipv6Prefix })
  $v4 = @($obj.prefixes | Where-Object { $_.ipv4Prefix } | ForEach-Object { [string]$_.ipv4Prefix })
  $v4 = @($v4 | Sort-Object { [int](($_ -split '/')[1]) })   # ascending mask = largest blocks first
  $ranges = @($v6)
  $slotsForV4 = $script:YbMaxFilters - $ranges.Count
  if ($slotsForV4 -gt 0) { $ranges += @($v4 | Select-Object -First $slotsForV4) }
  if ($ranges.Count -gt $script:YbMaxFilters) { $ranges = @($ranges[0..($script:YbMaxFilters - 1)]) }
  if (-not $ranges -or $ranges.Count -eq 0) { return $null }
  return ,$ranges
}

# (Re)create the lightweight pktmon counters-only session whose filters match the
# given Google ranges. Idempotent: tears down any prior session/filters first.
# Counters-only (start -c -o) = no packet logging to disk, just per-component byte
# counters, so overhead stays minimal. Returns $true on success.
function Initialize-YbPktmon($Ranges) {
  $ErrorActionPreference = 'SilentlyContinue'   # pktmon writes benign stderr; don't let EAP=Stop trip
  if (-not (Test-Path $script:YbPktMon)) { return $false }
  if (-not $Ranges -or @($Ranges).Count -eq 0) { return $false }
  try {
    & $script:YbPktMon stop 2>&1 | Out-Null
    & $script:YbPktMon filter remove 2>&1 | Out-Null
    $i = 0
    foreach ($r in $Ranges) {
      $i++
      if ($i -gt $script:YbMaxFilters) { break }
      & $script:YbPktMon filter add ('{0}{1}' -f $script:YbPktFilterTag, $i) -i $r 2>&1 | Out-Null
    }
    & $script:YbPktMon start -c -o 2>&1 | Out-Null
    return $true
  } catch { return $false }
}

# Tear down our pktmon session + filters (called on demand; the watcher otherwise
# leaves the counters-only session running).
function Stop-YbPktmon {
  $ErrorActionPreference = 'SilentlyContinue'
  try { & $script:YbPktMon stop 2>&1 | Out-Null; & $script:YbPktMon filter remove 2>&1 | Out-Null } catch { }
}

# Read the pktmon counters and return the maximum cumulative INBOUND bytes across
# all monitored components. pktmon counts the same packet at every layer of the
# network stack, so SUMMING components multi-counts; the MAX ~= the physical NIC's
# view of matched inbound bytes - one monotonic number since the session started.
# Returns $null if the session isn't running / counters can't be parsed, so the
# caller skips the sample (no phantom accounting).
function Get-YbGoogInboundBytes {
  $ErrorActionPreference = 'SilentlyContinue'
  if (-not (Test-Path $script:YbPktMon)) { return $null }
  try { $raw = (& $script:YbPktMon counters --json 2>$null | Out-String) } catch { return $null }
  if ([string]::IsNullOrWhiteSpace($raw)) { return $null }
  $s = $raw.IndexOf('['); $e = $raw.LastIndexOf(']')
  if ($s -lt 0 -or $e -le $s) { return $null }   # not JSON => session not running
  try { $o = $raw.Substring($s, $e - $s + 1) | ConvertFrom-Json } catch { return $null }
  $max = [int64]0
  foreach ($g in $o) {
    foreach ($c in $g.Components) {
      foreach ($cn in $c.Counters) {
        $b = [int64]$cn.Inbound.Bytes
        if ($b -gt $max) { $max = $b }
      }
    }
  }
  return $max
}

# Ensure the DNS-Client/Operational ETW channel is enabled (off by default). It is
# the real-time DNS gate signal - it logs every resolution as it happens, so it
# catches the sparse, short-TTL YouTube lookups that never linger in the DNS cache.
# Idempotent, best-effort; the watcher (SYSTEM) calls this once at startup.
function Enable-YbDnsChannel {
  $ErrorActionPreference = 'SilentlyContinue'
  try {
    $log = Get-WinEvent -ListLog $script:YbDnsChannel -ErrorAction Stop
    if (-not $log.IsEnabled) { $log.IsEnabled = $true; $log.SaveChanges() }
    return $true
  } catch {
    try { & "$env:SystemRoot\System32\wevtutil.exe" sl $script:YbDnsChannel /e:true 2>&1 | Out-Null; return $true }
    catch { return $false }
  }
}

# DNS gate: $true if any YouTube-family name was resolved at/after $Since. Reads the
# DNS-Client/Operational channel (events 3006 query / 3008 query-completed) and the
# QueryName *property* (Properties[0]) so it is locale-independent. Best-effort:
# returns $false if the channel is empty or unreadable.
function Test-YbYouTubeDnsSince([datetime]$Since) {
  $suffixPattern = '(^|\.)(' +
    (($script:YbContentDomains | ForEach-Object { [regex]::Escape($_) }) -join '|') + ')$'
  try {
    $ev = Get-WinEvent -FilterHashtable @{ LogName = $script:YbDnsChannel; Id = 3006, 3008; StartTime = $Since } -ErrorAction Stop
  } catch {
    return $false   # "no events found" (normal/idle) or channel unavailable
  }
  foreach ($e in $ev) {
    $qn = $null
    try { $qn = [string]$e.Properties[0].Value } catch { }
    if ([string]::IsNullOrWhiteSpace($qn)) { continue }
    if ($qn.ToLower() -match $suffixPattern) { return $true }
  }
  return $false
}
