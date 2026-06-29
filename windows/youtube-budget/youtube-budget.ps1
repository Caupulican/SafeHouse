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

  2. DETECT - pure Windows, no WSL/Pi-hole. METER the inbound bytes to Google's
     serving IP ranges with a lightweight pktmon counters-only session (this survives
     DNS caching / QUIC connection-coalescing, which made cache-polling blind to
     modern YouTube), and GATE that on a recent real-time YouTube-family DNS lookup
     (Microsoft-Windows-DNS-Client ETW). A sample is "fresh" watching when the
     inbound throughput to Google exceeds MIN_THROUGHPUT_KBPS *and* a YouTube-family
     name resolved within DNS_GATE_MIN minutes. A sliding WINDOW_SEC keeps counting
     through brief dips between video segments and stops ~WINDOW_SEC after streaming
     ends.

  3. ACCOUNT - if active and not already blocked, add sample_sec to seconds_used.

  4. ENFORCE - limit = limit_min*60 + bonus_sec (default limit 60 min). When
     seconds_used >= limit and not blocked, splice the YouTube hosts block in
     (shared list ..\parental-blocks\youtube.txt), set blocked_by_budget, flush DNS.

Resilient: a transient error in a tick is logged and the loop continues; an
unreadable pktmon session is re-armed and the sample skipped (no phantom
accounting). State + log live under C:\ProgramData\SafeHouse (see common.ps1).

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
$DnsGateMin = [int]$cfg.dns_gate_min
$HostsPath  = Get-YbHostsPath

# In-memory detection state (a persistent loop, so no need to persist these).
$script:GoogRanges      = $null      # cached list of Google CIDRs we meter
$script:PrevBytes       = $null      # last cumulative inbound-bytes reading (for the delta)
$script:LastActiveEpoch = 0          # unix seconds of the last "fresh" watching sample
$script:LastDnsEpoch    = 0          # unix seconds of the last YouTube-family DNS lookup
$script:LastDnsCheck    = Get-Date   # high-water mark for the DNS-gate event query
$script:LastIdleLog     = 0          # rate-limit for the [idle] diagnostic line

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

  # 2) DETECT - meter inbound bytes to Google ranges, gated on YouTube DNS.
  $curBytes = Get-YbGoogInboundBytes
  if ($null -eq $curBytes) {
    # Session missing/unreadable -> (re)arm it and skip this sample (no accrual).
    if (-not $script:GoogRanges) { $script:GoogRanges = Get-YbGoogRanges }
    $armed = $false
    if ($script:GoogRanges) { $armed = Initialize-YbPktmon $script:GoogRanges }
    $script:PrevBytes = $null
    Write-YbLog "[skip] pktmon counters unreadable - re-armed=$armed, sample skipped (no accumulation)"
    return
  }

  # Inbound throughput = byte delta since last tick. A drop (counters reset because
  # the session was recreated) just rebaselines without billing.
  if ($null -eq $script:PrevBytes -or $curBytes -lt $script:PrevBytes) { $delta = [int64]0 }
  else { $delta = [int64]$curBytes - [int64]$script:PrevBytes }
  $script:PrevBytes = $curBytes
  $kbps = [int]([math]::Round(($delta * 8.0) / 1000.0 / $SampleSec))

  # DNS gate: did a YouTube-family name resolve since the last check? Update the
  # last-resolution timestamp; the gate then stays open for DNS_GATE_MIN minutes.
  $nowDt = Get-Date
  $since = $script:LastDnsCheck
  if (($nowDt - $since).TotalSeconds -gt 3600) { $since = $nowDt.AddSeconds(-($SampleSec + 5)) }
  if (($DnsGateMin -gt 0) -and (Test-YbYouTubeDnsSince $since)) { $script:LastDnsEpoch = $now }
  $script:LastDnsCheck = $nowDt
  $gateOpen = ($DnsGateMin -le 0) -or (($script:LastDnsEpoch -gt 0) -and (($now - $script:LastDnsEpoch) -lt ($DnsGateMin * 60)))
  $gateStr  = if ($DnsGateMin -le 0) { 'off' } elseif ($gateOpen) { 'open' } else { 'closed' }

  # Fresh watching sample = video-like throughput to Google AND the gate open.
  $fresh = ($kbps -ge $MinKbps) -and $gateOpen
  if ($fresh) { $script:LastActiveEpoch = $now }
  # window_sec smoothing: keep counting through brief dips, stop ~window_sec after end.
  $active = ($script:LastActiveEpoch -gt 0) -and (($now - $script:LastActiveEpoch) -lt $WindowSec)

  # 3) ACCOUNT - only bill while watching AND not already budget-blocked.
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
  if ($active) {
    Write-YbLog "[sample] active +${SampleSec}s used=$($st.seconds_used)s/${limit}s blocked=$($st.blocked_by_budget) kbps=$kbps gate=$gateStr"
  } elseif ($kbps -ge $MinKbps -and (($now - $script:LastIdleLog) -ge 60)) {
    # Google bytes are flowing but not counted (gate closed, or after window end) -
    # a rate-limited breadcrumb for tuning min_throughput_kbps / dns_gate_min.
    $script:LastIdleLog = $now
    Write-YbLog "[idle] google kbps=$kbps gate=$gateStr (not counted) used=$($st.seconds_used)s/${limit}s"
  }
}

function Invoke-Main {
  Limit-YbLog
  # Arm the byte meter + the real-time DNS gate before the loop.
  $script:GoogRanges = Get-YbGoogRanges
  $rangeCount = if ($script:GoogRanges) { @($script:GoogRanges).Count } else { 0 }
  $pktmon = $false
  if ($script:GoogRanges) { $pktmon = Initialize-YbPktmon $script:GoogRanges }
  $dnsCh = Enable-YbDnsChannel
  $script:LastDnsCheck = Get-Date
  if ($pktmon) { Start-Sleep -Seconds 1; $script:PrevBytes = Get-YbGoogInboundBytes }   # baseline so tick 1 has a valid delta
  Write-YbLog "[start] youtube-budget watcher (sample=${SampleSec}s window=${WindowSec}s limit=${DefLimit}m min_kbps=${MinKbps} dns_gate=${DnsGateMin}m ranges=$rangeCount pktmon=$pktmon dns_ch=$dnsCh hosts=$HostsPath)"
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
