<#
SafeHouse - YouTube watch-time budget: shared helpers (Windows-native).

Dot-sourced by youtube-budget.ps1 (the watcher) and youtube-budget-ctl.ps1 (the
parent CLI). Provides: paths/config, local-date helper, atomic JSON state I/O, the
byte-preserving hosts block/unblock splice (mirrors windows\parental-toggle.ps1),
connection-flow watch detection (RESOLVED-IP PINNING: harvest the IPs that
YouTube-family hostnames resolve to from the DNS-Client ETW channel, pin them, and
meter inbound bytes to EXACTLY those IPs with pktmon), and DNS flush. No side effects
on import.

State + config + log live under C:\ProgramData\SafeHouse (created with a locked-down
ACL by install-task.ps1: SYSTEM + Administrators full, Users read-only). For testing
without elevation, set $env:YB_ROOT (state/config/log root) and/or $env:YB_HOSTS
(hosts file) to point at temp copies - every helper honours those overrides.

Marker convention (shared with windows\parental-toggle.ps1 and compatible with
an external undo script matching the same markers):
  start: # === Parental block: YouTube (auto: daily budget) ===
  end:   # === end YouTube block ===
#>

# --- locations ---------------------------------------------------------------
$script:YbRoot       = if ($env:YB_ROOT) { $env:YB_ROOT } else { 'C:\ProgramData\SafeHouse' }
$script:YbStatePath  = Join-Path $script:YbRoot 'youtube-budget.json'
$script:YbConfigPath = Join-Path $script:YbRoot 'config.json'
$script:YbLogPath    = Join-Path $script:YbRoot 'youtube-budget.log'
$script:YbPinsPath   = Join-Path $script:YbRoot 'yt-pinned-ips.json'  # resolved YouTube IPs we meter
$script:YbGoogPath   = Join-Path $script:YbRoot 'goog-ranges.json'    # cached Google prefixes (GGC diagnostics only)
$script:YbScriptDir  = Split-Path -Parent $PSCommandPath

# --- connection-flow metering: tools + constants -----------------------------
$script:YbPktMon       = Join-Path $env:SystemRoot 'System32\PktMon.exe'   # byte meter (counts UDP/QUIC too)
$script:YbGoogUrl      = 'https://www.gstatic.com/ipranges/goog.json'      # Google's published prefixes (for GGC diagnostics)
$script:YbGoogMaxAgeD  = 7            # refresh the cached prefixes if older than this many days
$script:YbPktFilterTag = 'SafeHouseYB'  # name stamped on every pktmon filter we own
$script:YbMaxFilters   = 32           # pktmon allows up to 32 active filters at once = our pinned-IP cap
$script:YbDnsChannel   = 'Microsoft-Windows-DNS-Client/Operational'        # source of resolved IPs
# YouTube-family DNS names whose answers we pin. Matched as a case-insensitive
# substring of the query name (googlevideo|youtube|ytimg|ggpht|yt3; i.ytimg c ytimg).
$script:YbYtNameRegex  = 'googlevideo|youtube|ytimg|ggpht|yt3'

# --- hosts marker + enforcement constants ------------------------------------
$script:YbBlockName   = 'YouTube'
$script:YbStartMarker = '# === Parental block: YouTube (auto: daily budget) ==='
$script:YbEndMarker   = '# === end YouTube block ==='
$script:YbFirewallRule = 'SafeHouse: YouTube budget block'  # owns only the budget's endpoint block
$script:YbUtf8NoBom   = New-Object System.Text.UTF8Encoding($false)

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
# Returns @{ limit_min; sample_sec; window_sec; min_throughput_kbps; pin_idle_expire_min }
# from ProgramData\config.json, then a script-local config.json, then hard defaults.
#   min_throughput_kbps  - sustained inbound kbps to the PINNED IPs that counts as
#                          video-like watching (default 64; tune up to ignore bursts).
#   pin_idle_expire_min  - drop a pinned IP after this many minutes with zero bytes
#                          flowing to it (default 30). A pin persists by ACTIVITY, not
#                          by DNS recency, so a coalesced QUIC session that stops
#                          re-resolving stays metered for as long as it streams.
function Get-YbConfig {
  $cfg = @{ limit_min = 180; sample_sec = 20; window_sec = 240; min_throughput_kbps = 64; pin_idle_expire_min = 30 }
  $paths = @($script:YbConfigPath, (Join-Path $script:YbScriptDir 'config.json'))
  foreach ($p in $paths) {
    if ($p -and (Test-Path $p)) {
      try {
        $o = Get-Content -Raw -LiteralPath $p | ConvertFrom-Json
        if ($null -ne $o.limit_min)           { $cfg.limit_min           = [int]$o.limit_min }
        if ($null -ne $o.sample_sec)          { $cfg.sample_sec          = [int]$o.sample_sec }
        if ($null -ne $o.window_sec)          { $cfg.window_sec          = [int]$o.window_sec }
        if ($null -ne $o.min_throughput_kbps) { $cfg.min_throughput_kbps = [int]$o.min_throughput_kbps }
        if ($null -ne $o.pin_idle_expire_min) { $cfg.pin_idle_expire_min = [int]$o.pin_idle_expire_min }
        break
      } catch { }
    }
  }
  if ($cfg.limit_min  -lt 0) { $cfg.limit_min  = 0 }
  if ($cfg.sample_sec -lt 1) { $cfg.sample_sec = 20 }
  if ($cfg.window_sec -lt $cfg.sample_sec) { $cfg.window_sec = $cfg.sample_sec }
  if ($cfg.min_throughput_kbps -lt 0) { $cfg.min_throughput_kbps = 0 }
  if ($cfg.pin_idle_expire_min -lt 1) { $cfg.pin_idle_expire_min = 1 }
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
    # Parent forced the block on via `ctl block`. Distinct from blocked_by_budget so
    # the watcher's declarative reconcile does not lift a deliberate manual block when
    # the day's usage happens to sit under the limit. Absent in pre-existing state
    # files -> $false, which is the correct default.
    manual_block      = [bool]$o.manual_block
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
    manual_block      = [bool]$State.manual_block
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

# --- endpoint firewall enforcement -------------------------------------------
# Hosts/DNS blocking prevents new lookups but cannot terminate an already-open QUIC
# stream. While the budget block is active, also block the exact YouTube endpoints
# previously pinned by the detector. The rule is SafeHouse-owned and removed on reset.
function Test-YbFirewallBlock {
  try { return @((Get-NetFirewallRule -DisplayName $script:YbFirewallRule -ErrorAction SilentlyContinue)).Count -gt 0 }
  catch { return $false }
}

function Remove-YbFirewallBlock {
  try {
    $rules = @(Get-NetFirewallRule -DisplayName $script:YbFirewallRule -ErrorAction SilentlyContinue)
    if ($rules.Count -eq 0) { return 'NONE' }
    $rules | Remove-NetFirewallRule | Out-Null
    return 'UNBLOCKED'
  } catch { return 'ERROR' }
}

# Returns BLOCKED:<n> | NOIPS | ERROR. $Pins is the persisted resolved-IP pin set.
function Add-YbFirewallBlock($Pins) {
  $ips = @()
  foreach ($p in @($Pins)) {
    $candidate = [string]$p.ip
    $parsed = [System.Net.IPAddress]::Any
    if ([System.Net.IPAddress]::TryParse($candidate, [ref]$parsed)) { $ips += $parsed.ToString() }
  }
  $ips = @($ips | Select-Object -Unique)
  if ($ips.Count -eq 0) { return 'NOIPS' }
  try {
    $null = Remove-YbFirewallBlock
    foreach ($direction in @('Outbound', 'Inbound')) {
      New-NetFirewallRule -DisplayName $script:YbFirewallRule -Direction $direction -Action Block `
        -RemoteAddress $ips -Protocol Any -Profile Any -Enabled True | Out-Null
    }
    return ('BLOCKED:{0}' -f $ips.Count)
  } catch { return 'ERROR' }
}

# --- DNS flush (best-effort) -------------------------------------------------
function Invoke-YbFlushDns {
  try { & ipconfig /flushdns | Out-Null } catch { }
}

# --- detection: resolved-IP pinning + pktmon byte metering -------------------
# Modern YouTube is invisible both to DNS-cache polling AND to metering "Google's
# published ranges": Edge coalesces QUIC connections onto warm IPs (so video streams
# with almost no fresh googlevideo.com lookups), and much of the video is served from
# Google Global Cache (GGC) nodes embedded in the ISP whose IPs are NOT in goog.json
# at all. We instead PIN the exact IPs that YouTube-family hostnames resolve to (read
# live from the DNS-Client ETW channel) and meter inbound bytes to EXACTLY those IPs
# with pktmon (which counts UDP/QUIC too). Because only YouTube-resolved IPs are ever
# metered, Gmail/Drive/Search never count; and a pin survives QUIC coalescing because
# it is kept alive by ACTIVITY (bytes flowing), not by DNS recency.

# --- pinned-IP state (persisted): [ { ip; first_seen; last_active } ] ---------
# first_seen / last_active are unix seconds. last_active drives expiry + the 32-cap
# eviction order.
function Read-YbPins {
  if (-not (Test-Path $script:YbPinsPath)) { return @() }
  try { $o = Get-Content -Raw -LiteralPath $script:YbPinsPath | ConvertFrom-Json } catch { return @() }
  if ($null -eq $o) { return @() }
  $pins = @()
  foreach ($p in @($o)) {
    if ([string]::IsNullOrWhiteSpace([string]$p.ip)) { continue }
    $pins += @{ ip = [string]$p.ip; first_seen = [int]$p.first_seen; last_active = [int]$p.last_active }
  }
  # Emit elements to the pipeline (callers wrap with @()); do NOT use ,$pins - that
  # returns the whole array as a single item and @() would then nest it.
  return $pins
}

# Write-YbPins @( @{ip;first_seen;last_active} ) -> atomic temp-write + replace.
# Serialised element-by-element so a 0- or 1-element set still emits a JSON array.
function Write-YbPins($Pins) {
  $dir = Split-Path -Parent $script:YbPinsPath
  if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
  $items = @(foreach ($p in @($Pins)) {
    ([ordered]@{ ip = [string]$p.ip; first_seen = [int]$p.first_seen; last_active = [int]$p.last_active } | ConvertTo-Json -Compress)
  })
  $json = '[' + ($items -join ',') + ']'
  $tmp  = Join-Path $dir ('.ybpins.' + [System.IO.Path]::GetRandomFileName())
  [System.IO.File]::WriteAllText($tmp, ($json + "`r`n"), $script:YbUtf8NoBom)
  Move-Item -LiteralPath $tmp -Destination $script:YbPinsPath -Force
}

# Harvest the answer IPs of YouTube-family resolutions from the DNS-Client channel
# since $Since. Event 3008 ("query completed") property [0] is the QueryName and
# property [4] is the ';'-separated QueryResults (a CNAME chain + the answer IPs; IPv4
# answers may appear mapped as ::ffff:x.x.x.x, alongside raw IPv6). Non-IP tokens
# (CNAMEs) are dropped. Best-effort: returns @() if the channel is idle/unreadable.
function Get-YbResolvedYtIps([datetime]$Since) {
  try {
    $ev = Get-WinEvent -FilterHashtable @{ LogName = $script:YbDnsChannel; Id = 3008; StartTime = $Since } -ErrorAction Stop
  } catch {
    return @()   # "no events found" (idle) or channel unavailable
  }
  $ips = New-Object System.Collections.Generic.List[string]
  foreach ($e in $ev) {
    $qn = $null
    try { $qn = [string]$e.Properties[0].Value } catch { }
    if ([string]::IsNullOrWhiteSpace($qn)) { continue }
    if ($qn -notmatch $script:YbYtNameRegex) { continue }   # -match is case-insensitive
    $res = $null
    try { $res = [string]$e.Properties[4].Value } catch { }
    if ([string]::IsNullOrWhiteSpace($res)) { continue }
    foreach ($tok in ($res -split ';')) {
      $t = $tok.Trim()
      if (-not $t) { continue }
      if ($t -match '^::ffff:(\d{1,3}(?:\.\d{1,3}){3})$') { $t = $matches[1] }   # unmap IPv4
      $parsed = [System.Net.IPAddress]::Any
      if ([System.Net.IPAddress]::TryParse($t, [ref]$parsed)) {
        $s = $parsed.ToString()
        if (-not $ips.Contains($s)) { [void]$ips.Add($s) }
      }
    }
  }
  return $ips.ToArray()
}

# (Re)create the lightweight pktmon counters-only session whose filters match the
# given IPs (pinned /32 & /128). Idempotent: tears down any prior session/filters
# first. Counters-only (start -c -o) = no packet logging to disk, just per-component
# byte counters, so overhead stays minimal. Returns $true on success.
function Initialize-YbPktmon($Filters) {
  $ErrorActionPreference = 'SilentlyContinue'   # pktmon writes benign stderr; don't let EAP=Stop trip
  if (-not (Test-Path $script:YbPktMon)) { return $false }
  if (-not $Filters -or @($Filters).Count -eq 0) { return $false }
  try {
    & $script:YbPktMon stop 2>&1 | Out-Null
    & $script:YbPktMon filter remove 2>&1 | Out-Null
    $i = 0
    foreach ($f in $Filters) {
      $i++
      if ($i -gt $script:YbMaxFilters) { break }
      & $script:YbPktMon filter add ('{0}{1}' -f $script:YbPktFilterTag, $i) -i $f 2>&1 | Out-Null
    }
    & $script:YbPktMon start -c -o 2>&1 | Out-Null
    return $true
  } catch { return $false }
}

# Tear down our pktmon session + filters (called when the pinned set empties, and at
# watcher startup to clear any stale session).
function Stop-YbPktmon {
  $ErrorActionPreference = 'SilentlyContinue'
  try { & $script:YbPktMon stop 2>&1 | Out-Null; & $script:YbPktMon filter remove 2>&1 | Out-Null } catch { }
}

# Read the pktmon counters and return the maximum cumulative INBOUND bytes across all
# monitored components. pktmon counts the same packet at every layer of the network
# stack, so SUMMING components multi-counts; the MAX ~= the physical NIC's view of
# matched inbound bytes - one monotonic number since the session started. Returns $null
# if the session isn't running / counters can't be parsed, so the caller skips the
# sample (no phantom accounting).
function Get-YbInboundBytes {
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

# Ensure the DNS-Client/Operational ETW channel is enabled (off by default). It is the
# real-time source of resolved IPs - it logs every resolution as it happens, so it
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

# --- GGC diagnostics (not on the metering hot path) --------------------------
# Load Google's published prefixes as @{ bytes; bits } entries, ONLY so [pin] log
# lines can flag which pinned IPs are GGC nodes OUTSIDE goog.json - i.e. exactly the
# traffic the old range meter structurally missed. Fetches/refreshes the goog.json
# cache (weekly; reused on failure); returns @() if unavailable.
function Get-YbGoogNets {
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
  if (-not (Test-Path $script:YbGoogPath)) { return @() }
  try { $obj = Get-Content -Raw -LiteralPath $script:YbGoogPath | ConvertFrom-Json } catch { return @() }
  if ($null -eq $obj -or $null -eq $obj.prefixes) { return @() }
  $nets = @()
  foreach ($p in $obj.prefixes) {
    $cidr = if ($p.ipv4Prefix) { [string]$p.ipv4Prefix } elseif ($p.ipv6Prefix) { [string]$p.ipv6Prefix } else { $null }
    if (-not $cidr) { continue }
    $parts = $cidr -split '/'
    if ($parts.Count -ne 2) { continue }
    $ip = [System.Net.IPAddress]::Any
    if (-not [System.Net.IPAddress]::TryParse($parts[0], [ref]$ip)) { continue }
    $nets += @{ bytes = $ip.GetAddressBytes(); bits = [int]$parts[1] }
  }
  return $nets
}

# True if $IpStr falls inside any published prefix in $Nets (family-aware bit-prefix
# compare on the raw address bytes; works uniformly for IPv4 and IPv6).
function Test-YbIpInGoog([string]$IpStr, $Nets) {
  $ip = [System.Net.IPAddress]::Any
  if (-not [System.Net.IPAddress]::TryParse($IpStr, [ref]$ip)) { return $false }
  $ab = $ip.GetAddressBytes()
  foreach ($n in @($Nets)) {
    $nb = $n.bytes
    if ($nb.Length -ne $ab.Length) { continue }   # different address family
    $bits = [int]$n.bits; $ok = $true; $i = 0
    while ($bits -gt 0) {
      if ($bits -ge 8) {
        if ($ab[$i] -ne $nb[$i]) { $ok = $false; break }
        $bits -= 8; $i++
      } else {
        $mask = [byte](((0xFF -shl (8 - $bits)) -band 0xFF))
        if (($ab[$i] -band $mask) -ne ($nb[$i] -band $mask)) { $ok = $false }
        break
      }
    }
    if ($ok) { return $true }
  }
  return $false
}
