<#
SafeHouse ad blocker - deterministic and self-arming.

One script that does the whole loop, re-runnable any time:

  1. Elevates itself (UAC) if it is not already admin.
  2. Captures the Google Play Games crosvm VM's current outbound IPs. crosvm uses userspace
     networking, so every guest connection is re-originated from the host crosvm.exe process and
     shows up in the host TCP table. That means ad servers the VM reached over DoH, QUIC, or a
     cached IP are all visible here, even though Pi-hole never saw the DNS query.
  3. Classifies each remote IP by its network owner (ASN, via Team Cymru over DNS) and reverse
     DNS, against ad-watchlist.txt. The watchlist holds stable ad-network identities (ASN numbers,
     owner-name and hostname substrings), so it keeps working when the ad servers rotate IPs.
  4. Logs every observation to logs\traffic.csv so you can analyze and refresh any time.
  5. Ingests the confirmed ad IPs into ad-ip-ranges.txt (the persistent ruleset, kept in the repo).
  6. Rebuilds the Windows Firewall block group from that ruleset. Outbound, TCP and UDP (so QUIC
     on UDP 443 is covered too), and the rules persist across reboots.

The whole thing is deterministic: same inputs (watchlist + current connections + ruleset file)
produce the same firewall state. Run it again whenever ads come back to fold in the new servers.

Usage (run from a normal or elevated PowerShell - it self-elevates):
  .\safehouse-adblock.ps1                  capture + classify + log + ingest + (re)build firewall
  .\safehouse-adblock.ps1 -RebuildOnly     skip capture, just rebuild the firewall from the file
  .\safehouse-adblock.ps1 -IngestFile c:\path\ips.txt   also ingest IPs/CIDRs from a file, then build
#>

[CmdletBinding()]
param(
  [string]$ListPath,
  [string]$WatchlistPath,
  [string]$LogPath,
  [string]$Group       = 'SafeHouse-AdBlock',
  [string]$ProcessName = 'crosvm',
  [string]$IngestFile,
  [switch]$RebuildOnly
)

$ErrorActionPreference = 'Stop'

# --- 1. self-elevate --------------------------------------------------------
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
  Write-Host 'Not elevated. Relaunching with admin rights (approve the UAC prompt)...'
  $a = @('-NoProfile','-ExecutionPolicy','Bypass','-NoExit','-File', ('"{0}"' -f $PSCommandPath))
  if ($RebuildOnly) { $a += '-RebuildOnly' }
  if ($IngestFile)  { $a += @('-IngestFile', ('"{0}"' -f $IngestFile)) }
  Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $a
  return
}

# --- resolve file locations (works from the repo layout or a flat folder) ---
$scriptDir = Split-Path -Parent $PSCommandPath
function Find-Repo-File([string]$name, [string]$repoSub) {
  foreach ($c in @((Join-Path $scriptDir $name), (Join-Path $scriptDir (Join-Path '..' $repoSub) | Join-Path -ChildPath $name))) {
    if (Test-Path $c) { return (Resolve-Path $c).Path }
  }
  # default to a sibling path even if missing, so we can create it
  return (Join-Path $scriptDir $name)
}
if (-not $ListPath)      { $ListPath      = Find-Repo-File 'ad-ip-ranges.txt' 'blocklists' }
if (-not $WatchlistPath) { $WatchlistPath = Find-Repo-File 'ad-watchlist.txt' 'blocklists' }
if (-not $LogPath) {
  $repoLogs = Join-Path $scriptDir (Join-Path '..' 'logs')
  $logDir   = if (Test-Path $repoLogs) { (Resolve-Path $repoLogs).Path } else { Join-Path $scriptDir 'logs' }
  if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
  $LogPath = Join-Path $logDir 'traffic.csv'
}

Write-Host "SafeHouse ad blocker"
Write-Host "  ruleset:   $ListPath"
Write-Host "  watchlist: $WatchlistPath"
Write-Host "  log:       $LogPath"
Write-Host ""

# --- load the ad watchlist (stable identities) ------------------------------
$wlAsns = @(); $wlOwners = @(); $wlHosts = @()
if (Test-Path $WatchlistPath) {
  foreach ($line in Get-Content $WatchlistPath) {
    $l = ($line -replace '#.*$','').Trim()
    if (-not $l) { continue }
    if ($l -match '^(?i)asn:\s*(\d+)')   { $wlAsns   += $Matches[1]; continue }
    if ($l -match '^(?i)owner:\s*(.+)$') { $wlOwners += $Matches[1].Trim().ToLower(); continue }
    if ($l -match '^(?i)host:\s*(.+)$')  { $wlHosts  += $Matches[1].Trim().ToLower(); continue }
    $wlOwners += $l.ToLower()
  }
}

# --- enrichment helpers -----------------------------------------------------
function Get-Owner([string]$ip) {
  $o = $ip.Split('.'); $rev = "$($o[3]).$($o[2]).$($o[1]).$($o[0])"
  $asn = ''; $cc = ''; $owner = ''
  try {
    $r = Resolve-DnsName -Type TXT -Name "$rev.origin.asn.cymru.com" -ErrorAction Stop | Select-Object -First 1
    $p = (($r.Strings -join '') -split '\|')
    $asn = $p[0].Trim().Split(' ')[0]
    if ($p.Count -ge 3) { $cc = $p[2].Trim() }
    if ($asn) {
      $r2 = Resolve-DnsName -Type TXT -Name "AS$asn.asn.cymru.com" -ErrorAction Stop | Select-Object -First 1
      $owner = ((($r2.Strings -join '') -split '\|')[-1]).Trim()
    }
  } catch {}
  [pscustomobject]@{ ASN = $asn; CC = $cc; Owner = $owner }
}
function Get-Ptr([string]$ip) {
  try { return (Resolve-DnsName -Type PTR -Name $ip -ErrorAction Stop | Select-Object -First 1).NameHost } catch { return '' }
}
function Test-IsAd($asn, $owner, $ptr) {
  if ($asn -and ($wlAsns -contains $asn)) { return $true }
  $ol = "$owner".ToLower(); foreach ($w in $wlOwners) { if ($w -and $ol.Contains($w)) { return $true } }
  $pl = "$ptr".ToLower();   foreach ($w in $wlHosts)  { if ($w -and $pl.Contains($w)) { return $true } }
  return $false
}

# --- 2 + 3 + 4. capture, classify, log --------------------------------------
$newAdRanges = New-Object System.Collections.Generic.List[string]
if (-not $RebuildOnly) {
  $pids = (Get-Process -Name $ProcessName -ErrorAction SilentlyContinue).Id
  if (-not $pids) {
    Write-Warning "No '$ProcessName' process found. Open Google Play Games and re-run to capture live ad servers. Rebuilding the firewall from the existing ruleset for now."
  } else {
    $conns = Get-NetTCPConnection -State Established -OwningProcess $pids -ErrorAction SilentlyContinue
    $ips = $conns | Select-Object -ExpandProperty RemoteAddress -Unique |
      Where-Object { $_ -match '^\d+\.\d+\.\d+\.\d+$' -and
                     $_ -notmatch '^(127\.|10\.|169\.254|172\.1[6-9]\.|172\.2[0-9]\.|172\.3[0-1]\.|192\.168\.)' }
    $stamp = (Get-Date).ToString('s')
    Write-Host ("Captured {0} external IP(s) from {1}:" -f ($ips | Measure-Object).Count, $ProcessName)
    foreach ($ip in $ips) {
      $info = Get-Owner $ip
      $ptr  = Get-Ptr $ip
      $isAd = Test-IsAd $info.ASN $info.Owner $ptr
      $verdict = if ($isAd) { 'AD' } else { 'ok' }
      [pscustomobject]@{ ts = $stamp; ip = $ip; asn = $info.ASN; cc = $info.CC; owner = $info.Owner; ptr = $ptr; verdict = $verdict } |
        Export-Csv -Path $LogPath -Append -NoTypeInformation
      if ($isAd) {
        $o = $ip.Split('.'); $cidr = "$($o[0]).$($o[1]).$($o[2]).0/24"
        $note = if ($info.Owner) { $info.Owner } else { $ptr }
        $newAdRanges.Add("$cidr    $note (auto)")
        Write-Host ("  AD  {0,-16} {1}" -f $ip, $note) -ForegroundColor Yellow
      } else {
        Write-Host ("  ok  {0,-16} {1}" -f $ip, $info.Owner) -ForegroundColor DarkGray
      }
    }
  }
}

# also ingest IPs/CIDRs from an external file, if asked (e.g. SNI hunter output)
if ($IngestFile -and (Test-Path $IngestFile)) {
  foreach ($line in Get-Content $IngestFile) {
    $t = ($line -replace '#.*$','').Trim()
    if ($t -match '^\d+\.\d+\.\d+\.\d+(/\d+)?') { $newAdRanges.Add((($t -split '\s+')[0] + '    ingested (auto)')) }
  }
}

# --- 5. ingest confirmed ad ranges into ad-ip-ranges.txt ---------------------
$beginMark = '# >>> AUTO (safehouse-adblock.ps1) - managed, do not hand-edit below >>>'
$endMark   = '# <<< AUTO end <<<'
$raw = if (Test-Path $ListPath) { Get-Content $ListPath -Raw } else { '' }
$manual = $raw; $autoEntries = @()
$mi = $raw.IndexOf($beginMark)
if ($mi -ge 0) {
  $manual = $raw.Substring(0, $mi).TrimEnd()
  ($raw.Substring($mi) -split "`n") | ForEach-Object {
    $t = ($_ -replace '#.*$','').Trim()
    if ($t -match '^\d+\.\d+\.\d+\.\d+(/\d+)?') {
      $cidr = ($t -split '\s+')[0]
      $note = ($_ -replace '^[^#]*','').Trim()  # keep trailing note if present
      $autoEntries += ('{0}    {1}' -f $cidr, ($_.Substring($cidr.Length).Trim()))
    }
  }
}
# manual CIDRs (to avoid duplicating them in the auto section)
$manualCidrs = @()
($manual -split "`n") | ForEach-Object {
  $t = ($_ -replace '#.*$','').Trim()
  if ($t -match '^\d+\.\d+\.\d+\.\d+(/\d+)?') { $manualCidrs += ($t -split '\s+')[0] }
}
# union of existing auto + new, deduped by CIDR, excluding anything already in the manual section
$seen = @{}; $finalAuto = New-Object System.Collections.Generic.List[string]
foreach ($e in (@($autoEntries) + @($newAdRanges))) {
  $cidr = ($e -split '\s+')[0]
  if (-not $cidr) { continue }
  if ($manualCidrs -contains $cidr) { continue }
  if ($seen.ContainsKey($cidr)) { continue }
  $seen[$cidr] = $true
  $finalAuto.Add($e)
}
$out = $manual.TrimEnd() + "`r`n`r`n" + $beginMark + "`r`n"
foreach ($e in $finalAuto) { $out += $e + "`r`n" }
$out += $endMark + "`r`n"
Set-Content -Path $ListPath -Value $out -Encoding ASCII

# --- 6. (re)build the firewall from the full ruleset ------------------------
$allRanges = @()
foreach ($line in (Get-Content $ListPath)) {
  $t = ($line -replace '#.*$','').Trim()
  if ($t -match '^\d+\.\d+\.\d+\.\d+(/\d+)?') { $allRanges += ($t -split '\s+')[0] }
}
$allRanges = $allRanges | Select-Object -Unique
if (-not $allRanges) { Write-Warning 'No ranges to block. Nothing to do.'; return }

Get-NetFirewallRule -Group $Group -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName 'SafeHouse Ad/Tracker Block (out)' -Group $Group `
  -Direction Outbound -Action Block -Profile Any -RemoteAddress $allRanges -Protocol Any | Out-Null

Write-Host ""
Write-Host ("Firewall group '{0}' now blocks {1} range(s)." -f $Group, $allRanges.Count) -ForegroundColor Green
if ($newAdRanges.Count -gt 0) { Write-Host ("Added {0} new range(s) this run (see the AUTO section of {1})." -f $newAdRanges.Count, $ListPath) }
Write-Host ""
Write-Host "Next: fully quit Google Play Games (tray icon -> Quit) and reopen it so the games drop"
Write-Host "cached ad IPs and reconnect into the blocked ranges."
Write-Host ""
Write-Host "Analyze later:  Import-Csv '$LogPath' | Group-Object owner | Sort-Object Count -Descending"
Write-Host "Remove all:     Get-NetFirewallRule -Group '$Group' | Remove-NetFirewallRule"
