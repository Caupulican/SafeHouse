<#
SafeHouse - YouTube watch-time budget: parent CLI (Windows-native).

Operate the budget by hand, consistently with the watcher (same ProgramData state +
the same byte-preserving hosts splice). State and the hosts file are writable only by
SYSTEM/Administrators, so every mutating verb self-elevates via UAC (status is
read-only and needs no elevation - a standard user can read it).

  youtube-budget-ctl.ps1 status            used / remaining / limit / blocked today
  youtube-budget-ctl.ps1 set-limit <min>   change today's + the default daily limit
  youtube-budget-ctl.ps1 grant <min>       add bonus minutes for today (unblocks if it frees budget)
  youtube-budget-ctl.ps1 block             force the YouTube block ON now
  youtube-budget-ctl.ps1 allow             force OFF now (override until the limit is crossed again)
  youtube-budget-ctl.ps1 reset             zero today's usage + bonus and unblock
#>

[CmdletBinding()]
param(
  [Parameter(Position = 0)][ValidateSet('status', 'set-limit', 'grant', 'block', 'allow', 'reset', 'help')]
  [string]$Command = 'status',
  [Parameter(Position = 1)][int]$Value
)

$ErrorActionPreference = 'Stop'
. (Join-Path (Split-Path -Parent $PSCommandPath) 'common.ps1')

function Test-Admin {
  $p = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
  return $p.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
}

# Mutating verbs touch SYSTEM/Admin-only state + the hosts file -> self-elevate.
if ($Command -ne 'status' -and $Command -ne 'help' -and -not (Test-Admin)) {
  Write-Host 'Not elevated. Relaunching with admin rights (approve the UAC prompt)...'
  $a = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-NoExit', '-File', ('"{0}"' -f $PSCommandPath), $Command)
  if ($PSBoundParameters.ContainsKey('Value')) { $a += [string]$Value }
  Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $a
  return
}

$cfg       = Get-YbConfig
$HostsPath = Get-YbHostsPath

function Format-Hms([int]$s) {
  $h = [math]::Floor($s / 3600); $m = [math]::Floor(($s % 3600) / 60); $sec = $s % 60
  if ($h -gt 0)    { return ('{0}h {1:00}m {2:00}s' -f $h, $m, $sec) }
  elseif ($m -gt 0){ return ('{0}m {1:00}s' -f $m, $sec) }
  else             { return ('{0}s' -f $sec) }
}

# Load state, applying the same local-day rollover the watcher uses (so the CLI
# always presents/operates on TODAY even if the watcher has not ticked yet).
function Get-State {
  $today = Get-YbToday
  $st = Read-YbState
  if ($null -eq $st) {
    $st = @{ date = $today; seconds_used = 0; limit_min = [int]$cfg.limit_min; bonus_sec = 0; blocked_by_budget = $false }
  }
  if ($st.date -ne $today) {
    $st.date = $today; $st.seconds_used = 0; $st.bonus_sec = 0; $st.blocked_by_budget = $false
  }
  return $st
}

function Get-EffLimit($st) { [int]$st.limit_min * 60 + [int]$st.bonus_sec }

function Set-ConfigLimit([int]$min) {
  $obj = [ordered]@{
    limit_min           = $min
    sample_sec          = [int]$cfg.sample_sec
    window_sec          = [int]$cfg.window_sec
    min_throughput_kbps = [int]$cfg.min_throughput_kbps
    dns_gate_min        = [int]$cfg.dns_gate_min
  }
  $dir = Split-Path -Parent $script:YbConfigPath
  if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
  Set-Content -LiteralPath $script:YbConfigPath -Value ($obj | ConvertTo-Json) -Encoding UTF8
}

switch ($Command) {

  'status' {
    $st = Get-State
    $limit = Get-EffLimit $st
    $rem = $limit - [int]$st.seconds_used; if ($rem -lt 0) { $rem = 0 }
    $budget = if ($st.blocked_by_budget) { 'BLOCKED (budget)' } else { 'ALLOWED' }
    $hostsState = if (Test-YbHostsBlocked $HostsPath) { 'blocked' } else { 'allowed' }
    Write-Host ("YouTube watch-time budget - {0}" -f $st.date)
    Write-Host ("  Used today : {0}" -f (Format-Hms ([int]$st.seconds_used)))
    Write-Host ("  Limit      : {0}  ({1}m daily + {2} bonus)" -f (Format-Hms $limit), $st.limit_min, (Format-Hms ([int]$st.bonus_sec)))
    Write-Host ("  Remaining  : {0}" -f (Format-Hms $rem))
    Write-Host ("  Budget     : {0}" -f $budget)
    Write-Host ("  Hosts file : YouTube section {0} in {1}" -f $hostsState, $HostsPath)
  }

  'set-limit' {
    if (-not $PSBoundParameters.ContainsKey('Value')) { Write-Error 'set-limit needs a whole number of minutes'; return }
    $st = Get-State
    $st.limit_min = $Value
    Set-ConfigLimit $Value
    if ($st.blocked_by_budget -and ([int]$st.seconds_used -lt (Get-EffLimit $st))) {
      Remove-YbHostsBlock $HostsPath 'auto' | Out-Null; Invoke-YbFlushDns; $st.blocked_by_budget = $false
      Write-Host 'Limit raised - budget freed, YouTube unblocked.'
    }
    Write-YbState $st
    Write-Host ("Daily limit set to {0}m (today + config.json)." -f $st.limit_min)
  }

  'grant' {
    if (-not $PSBoundParameters.ContainsKey('Value')) { Write-Error 'grant needs a whole number of minutes'; return }
    $st = Get-State
    $st.bonus_sec = [int]$st.bonus_sec + $Value * 60
    if ($st.blocked_by_budget -and ([int]$st.seconds_used -lt (Get-EffLimit $st))) {
      Remove-YbHostsBlock $HostsPath 'auto' | Out-Null; Invoke-YbFlushDns; $st.blocked_by_budget = $false
      Write-Host ("Granted {0}m - budget freed, YouTube unblocked." -f $Value)
    } else {
      Write-Host ("Granted {0}m of bonus for today." -f $Value)
    }
    Write-YbState $st
  }

  'block' {
    $st = Get-State
    $res = Add-YbHostsBlock $HostsPath
    switch -Wildcard ($res) {
      'BLOCKED*' { $st.blocked_by_budget = $true; Invoke-YbFlushDns; Write-Host ("YouTube BLOCKED now ($res).") }
      'ALREADY'  { $st.blocked_by_budget = $true; Write-Host 'YouTube already BLOCKED.' }
      'NOLIST'   { Write-Error 'Cannot block - host list missing (parental-blocks\youtube.txt).'; return }
      default    { Write-Error "Block failed (out='$res')"; return }
    }
    Write-YbState $st
  }

  'allow' {
    $st = Get-State
    Remove-YbHostsBlock $HostsPath 'any' | Out-Null
    Invoke-YbFlushDns
    # Give one-sample headroom so a no-activity tick won't instantly re-arm; the
    # watcher re-blocks only when fresh watching crosses the limit again.
    $limit = Get-EffLimit $st
    if ([int]$st.seconds_used -ge $limit) {
      $st.bonus_sec = [int]$st.bonus_sec + ([int]$st.seconds_used - $limit) + [int]$cfg.sample_sec
    }
    $st.blocked_by_budget = $false
    Write-YbState $st
    Write-Host 'YouTube ALLOWED now (override). Watcher re-blocks once today''s watching crosses the limit again.'
  }

  'reset' {
    $st = Get-State
    $st.seconds_used = 0; $st.bonus_sec = 0
    Remove-YbHostsBlock $HostsPath 'any' | Out-Null
    Invoke-YbFlushDns
    $st.blocked_by_budget = $false
    Write-YbState $st
    Write-Host ("Reset - today's usage zeroed and YouTube unblocked (limit {0}m)." -f $st.limit_min)
  }

  'help' {
    Write-Host @'
SafeHouse - YouTube budget parent CLI

  status            used / remaining / limit / blocked today
  set-limit <min>   change today's + the default daily limit
  grant <min>       add bonus minutes for today (unblocks if it frees budget)
  block             force the YouTube block ON now
  allow             force OFF now (override until the limit is crossed again)
  reset             zero today's usage + bonus and unblock

State + hosts edits self-elevate via UAC (status is read-only).
'@
  }
}
