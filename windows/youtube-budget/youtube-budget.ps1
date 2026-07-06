<#
SafeHouse - YouTube daily watch-time budget: the WATCHER (Windows-native).

A persistent loop (registered by install-task.ps1 as the SYSTEM Scheduled Task
"SafeHouse-YouTubeBudget", trigger AtStartup, Restart=always). Every sample (~20s)
it does, in order:

  1. DATE-BASED IDEMPOTENT RESET - read state; compute today's LOCAL date. If the
     stored date != today (any number of days elapsed, including the very first
     tick after boot), zero seconds_used + bonus_sec, clear blocked_by_budget,
     set date=today, and REMOVE any stale auto YouTube block from the hosts file.
     Purely a date comparison, so it is correct whether the PC ran through midnight,
     was off at midnight, or was off for several days. Running it twice in one day
     is a no-op.

  2. DETECT - RESOLVED-IP PINNING (pure Windows, no WSL/Pi-hole). Harvest the IPs that
     YouTube-family hostnames resolve to from the Microsoft-Windows-DNS-Client ETW
     channel, PIN them, and METER inbound bytes to EXACTLY those IPs with a lightweight
     pktmon counters-only session. This catches the stream wherever it flows - Google's
     own IPs (v4 AND v6) and the Google Global Cache (GGC) nodes embedded in the ISP
     whose IPs are not in goog.json at all - which range-based metering structurally
     missed. Because only YouTube-resolved IPs are pinned, Gmail/Drive/Search never
     count (no DNS gate needed). A pin is kept alive by ACTIVITY (bytes flowing), not
     DNS recency, so a coalesced QUIC session that stops re-resolving stays metered;
     an idle pin expires after pin_idle_expire_min. A sample is "watching" when inbound
     throughput to the pinned IPs exceeds min_throughput_kbps; a sliding window_sec
     keeps counting through brief dips between segments and stops ~window_sec after
     streaming ends.

  3. ACCOUNT - if active and not already blocked, add sample_sec to seconds_used.

  4. ENFORCE - limit = limit_min*60 + bonus_sec (default limit 60 min). When
     seconds_used >= limit and not blocked, splice the YouTube hosts block in
     (shared list ..\parental-blocks\youtube.txt), set blocked_by_budget, flush DNS.

Resilient: a transient error in a tick is logged and the loop continues; an
unreadable pktmon session is re-armed and the sample skipped (no phantom accounting).
State + pins + log live under C:\ProgramData\SafeHouse (see common.ps1).

Run modes:
  .\youtube-budget.ps1            # the supervised loop (what the task runs)
  .\youtube-budget.ps1 -Once      # run a single tick and exit (for testing/verify)
#>

[CmdletBinding()]
param(
  [switch]$Once
)

$ErrorActionPreference = 'Stop'
. (Join-Path (Split-Path -Parent $PSCommandPath) 'common.ps1')

$cfg        = Get-YbConfig
$SampleSec  = [int]$cfg.sample_sec
$WindowSec  = [int]$cfg.window_sec
$DefLimit   = [int]$cfg.limit_min
$MinKbps    = [int]$cfg.min_throughput_kbps
$PinIdleSec = [int]$cfg.pin_idle_expire_min * 60
$HostsPath  = Get-YbHostsPath

# In-memory detection state (a persistent loop, so no need to persist these).
$script:PrevBytes    = $null      # last cumulative inbound-bytes reading (for the delta)
$script:LastActive   = 0          # unix seconds of the last "fresh" (video-like) sample
$script:MeteredIps   = @()        # IPs currently loaded as pktmon filters (sorted)
$script:LastDnsCheck = Get-Date   # high-water mark for the DNS harvest query
$script:GoogNets     = @()        # published prefixes, for GGC-vs-goog diagnostics only
$script:LastIdleLog  = 0          # rate-limit for the [idle] diagnostic line

function Get-NowEpoch { [int][DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }

function Invoke-Tick {
  $today = Get-YbToday
  $now   = Get-NowEpoch

  # Load or initialise state.
  $st = Read-YbState
  if ($null -eq $st) {
    $st = @{ date = $today; seconds_used = 0; limit_min = $DefLimit; bonus_sec = 0; blocked_by_budget = $false }
    Write-YbState $st
    Write-YbLog "[init] created state $($script:YbStatePath) (limit=$($st.limit_min)m)"
  }

  # 1) DATE-BASED IDEMPOTENT RESET (survives shutdowns / multi-day-off).
  if ($st.date -ne $today) {
    $res = Remove-YbHostsBlock $HostsPath 'auto'
    if ($res -eq 'UNBLOCKED') { Invoke-YbFlushDns }
    Write-YbLog "[reset] new day $today (was $($st.date)) - usage zeroed; auto block: $res"
    $st.date = $today
    $st.seconds_used = 0
    $st.bonus_sec = 0
    $st.blocked_by_budget = $false
    Write-YbState $st
  }

  # 2) DETECT - resolved-IP pinning + pktmon byte metering.

  # 2a) Meter bytes to the set armed during the just-elapsed interval (last tick's
  #     MeteredIps). Read BEFORE re-arming so the delta reflects that stable set.
  $curBytes = $null
  if (@($script:MeteredIps).Count -gt 0) {
    $curBytes = Get-YbInboundBytes
    if ($null -eq $curBytes) {
      # Session missing/unreadable -> force a clean re-arm below (no accrual this tick).
      $script:MeteredIps = @()
      $script:PrevBytes  = $null
      Write-YbLog '[skip] pktmon counters unreadable - re-arming pinned filters, sample skipped'
    }
  }
  if ($null -eq $curBytes) {
    $kbps = 0
  } else {
    if ($null -eq $script:PrevBytes -or $curBytes -lt $script:PrevBytes) { $delta = [int64]0 }
    else { $delta = [int64]$curBytes - [int64]$script:PrevBytes }
    $script:PrevBytes = $curBytes
    $kbps = [int]([math]::Round(($delta * 8.0) / 1000.0 / $SampleSec))
  }
  $videoLike = ($kbps -ge $MinKbps)

  # 2b) Harvest freshly-resolved YouTube-family IPs from the DNS ETW channel.
  $nowDt = Get-Date
  $since = $script:LastDnsCheck
  if (($nowDt - $since).TotalSeconds -gt 3600) { $since = $nowDt.AddSeconds(-($SampleSec + 5)) }
  $resolved = Get-YbResolvedYtIps $since
  $script:LastDnsCheck = $nowDt

  # 2c) Update the pin set: add/refresh resolved IPs; keep the actively-streaming set
  #     alive; expire idle pins; cap to the 32 most-recently-active.
  $pins = @(Read-YbPins)
  $byIp = @{}
  foreach ($p in $pins) { $byIp[$p.ip] = $p }
  $newPins = @()
  foreach ($ip in $resolved) {
    if ($byIp.ContainsKey($ip)) { $byIp[$ip].last_active = $now }
    else {
      $pin = @{ ip = $ip; first_seen = $now; last_active = $now }
      $byIp[$ip] = $pin; $pins += $pin; $newPins += $ip
    }
  }
  # Keep-alive: if the metered set is actively streaming, refresh every loaded pin so a
  # coalesced session (which stops re-resolving) does not expire mid-stream.
  if ($videoLike) {
    foreach ($ip in $script:MeteredIps) { if ($byIp.ContainsKey($ip)) { $byIp[$ip].last_active = $now } }
  }
  # Expire idle pins, then cap to the 32 most-recently-active (evict the oldest).
  $pins = @($pins | Where-Object { ($now - [int]$_.last_active) -lt $PinIdleSec })
  if ($pins.Count -gt $script:YbMaxFilters) {
    $pins = @($pins | Sort-Object -Descending { [int]$_.last_active } | Select-Object -First $script:YbMaxFilters)
  }
  Write-YbPins $pins

  # Log new pins (Google/GGC IPs, not PII); flag GGC/off-goog = what ranges missed.
  foreach ($ip in $newPins) {
    if (-not ($pins | Where-Object { $_.ip -eq $ip })) { continue }   # dropped by cap/expiry same tick
    $tag = if (Test-YbIpInGoog $ip $script:GoogNets) { 'goog' } else { 'GGC/off-goog' }
    Write-YbLog "[pin] +$ip ($tag) pins=$($pins.Count)"
  }

  # 2d) (Re)arm pktmon iff the metered IP set changed since it was last armed. Re-arming
  #     resets the counters, so we rebaseline (bill 0 that tick); window_sec bridges it.
  $desired = @($pins | ForEach-Object { [string]$_.ip } | Sort-Object)
  if ((@($desired) -join ',') -ne (@($script:MeteredIps) -join ',')) {
    if ($desired.Count -gt 0) {
      if (-not (Initialize-YbPktmon $desired)) { Write-YbLog "[warn] pktmon re-arm failed for $($desired.Count) pinned IPs" }
    } else {
      Stop-YbPktmon
    }
    $script:MeteredIps = $desired
    $script:PrevBytes  = $null
  }

  # 3) ACCOUNT - fresh watching = video-like throughput to the pinned IPs (the pin is
  #    the disambiguator; no DNS gate). window_sec smoothing keeps counting through
  #    brief dips and stops ~window_sec after streaming ends. Idle (no bytes) = +0s.
  if ($videoLike) { $script:LastActive = $now }
  $active = ($script:LastActive -gt 0) -and (($now - $script:LastActive) -lt $WindowSec)
  if ($active -and -not $st.blocked_by_budget) {
    $st.seconds_used = [int]$st.seconds_used + $SampleSec
  }

  # 4) ENFORCE.
  $limit = [int]$st.limit_min * 60 + [int]$st.bonus_sec
  if (($st.seconds_used -ge $limit) -and -not $st.blocked_by_budget) {
    $res = Add-YbHostsBlock $HostsPath
    switch -Wildcard ($res) {
      'BLOCKED*' { $st.blocked_by_budget = $true; Invoke-YbFlushDns; Write-YbLog "[enforce] budget reached ($($st.seconds_used)s >= ${limit}s) - YouTube BLOCKED ($res)" }
      'ALREADY'  { $st.blocked_by_budget = $true; Write-YbLog "[enforce] budget reached - YouTube already blocked (ALREADY)" }
      'NOLIST'   { Write-YbLog "[error] enforcement host list missing (parental-blocks\youtube.txt) - cannot block" }
      default    { Write-YbLog "[error] hosts block failed (out='$res')" }
    }
  }

  Write-YbState $st
  $pc = $pins.Count
  if ($active) {
    Write-YbLog "[sample] active +${SampleSec}s used=$($st.seconds_used)s/${limit}s blocked=$($st.blocked_by_budget) kbps=$kbps pins=$pc"
  } elseif ($kbps -ge $MinKbps -and (($now - $script:LastIdleLog) -ge 60)) {
    # Bytes to pinned IPs but not counted (window elapsed) - a rate-limited breadcrumb.
    $script:LastIdleLog = $now
    Write-YbLog "[idle] kbps=$kbps pins=$pc (not counted; window elapsed) used=$($st.seconds_used)s/${limit}s"
  }
}

function Invoke-Main {
  Limit-YbLog
  # Clear any stale pktmon session, arm the DNS source, load the GGC diagnostic set.
  Stop-YbPktmon
  $script:MeteredIps = @()
  $script:PrevBytes  = $null
  $dnsCh   = Enable-YbDnsChannel
  $script:GoogNets = Get-YbGoogNets
  $script:LastDnsCheck = Get-Date
  $pinCount = @(Read-YbPins).Count
  Write-YbLog "[start] youtube-budget watcher (pinned-IP metering; sample=${SampleSec}s window=${WindowSec}s limit=${DefLimit}m min_kbps=${MinKbps} pin_idle_expire=$([int]$cfg.pin_idle_expire_min)m goog_nets=$(@($script:GoogNets).Count) pins=$pinCount dns_ch=$dnsCh hosts=$HostsPath)"
  while ($true) {
    try { Invoke-Tick } catch { Write-YbLog "[warn] tick failed (continuing): $_" }
    Start-Sleep -Seconds $SampleSec
  }
}

if ($Once) {
  try { Invoke-Tick } catch { Write-YbLog "[warn] tick failed: $_"; throw }
} else {
  Invoke-Main
}
