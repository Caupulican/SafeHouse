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

  2. DETECT - pure Windows, no WSL/Pi-hole. Poll the Windows DNS Client cache for
     live (non-blocked) resolutions of YouTube content domains (googlevideo.com,
     youtube.com, youtubei.googleapis.com, ytimg.com, youtu.be, yt3.ggpht.com).
     A "fresh" signal (a new cached content host, or one whose TTL bumped up =
     re-resolved) marks watching; a sliding WINDOW_SEC bridges the gaps between a
     video's sparse lookups and stops counting ~WINDOW_SEC after watching ends.

  3. ACCOUNT - if active and not already blocked, add sample_sec to seconds_used.

  4. ENFORCE - limit = limit_min*60 + bonus_sec (default limit 60 min). When
     seconds_used >= limit and not blocked, splice the YouTube hosts block in
     (shared list ..\parental-blocks\youtube.txt), set blocked_by_budget, flush DNS.

Resilient: a transient error in a tick is logged and the loop continues; a failed
DNS-cache query skips the sample (no phantom accounting). State + log live under
C:\ProgramData\SafeHouse (see common.ps1).

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
$HostsPath  = Get-YbHostsPath

# In-memory detection state (a persistent loop, so no need to persist these).
$script:PrevSig         = @{}   # entry-name -> remaining TTL last seen
$script:LastActiveEpoch = 0     # unix seconds of the last "fresh" YouTube signal

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

  # 2) DETECT.
  $hits = Get-YbYouTubeCacheHits
  if ($null -eq $hits) {
    Write-YbLog "[skip] DNS cache query failed - sample skipped (no accumulation)"
    return
  }

  # Fresh signal = a content host we did not see last tick, or whose TTL bumped up
  # (a re-resolution). A lingering cached entry whose TTL only decreases is NOT
  # fresh, so it cannot keep the counter alive long after watching has stopped.
  $curSig = @{}
  foreach ($h in $hits) { $curSig[$h.Entry] = $h.TTL }
  $fresh = $false
  foreach ($k in $curSig.Keys) {
    if (-not $script:PrevSig.ContainsKey($k)) { $fresh = $true; break }
    if ($curSig[$k] -gt $script:PrevSig[$k]) { $fresh = $true; break }
  }
  $script:PrevSig = $curSig
  if ($fresh) { $script:LastActiveEpoch = $now }

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
    Write-YbLog "[sample] active +${SampleSec}s used=$($st.seconds_used)s/${limit}s blocked=$($st.blocked_by_budget)"
  }
}

function Invoke-Main {
  Limit-YbLog
  Write-YbLog "[start] youtube-budget watcher (sample=${SampleSec}s window=${WindowSec}s limit=${DefLimit}m hosts=$HostsPath)"
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
