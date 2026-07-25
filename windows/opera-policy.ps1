<#
SafeHouse - lock down Opera GX so it cannot bypass the DNS filtering layer.

Opera GX is a per-user install in the child's profile, so it is NOT covered by the
machine policies already set for Edge and Chrome. It ships two independent bypasses:

  1. A built-in free VPN, which tunnels all browser traffic through Opera's proxies.
     DNS filtering (Pi-hole / AdGuard Home) and the hosts file never see that traffic,
     so every layer of this project is defeated while it is on.
  2. Its own DNS-over-HTTPS resolver, which bypasses the Windows resolver and therefore
     the hosts-file YouTube block.

Opera is Chromium-based and reads Chromium policies from
HKLM\SOFTWARE\Policies\Opera Software\Opera. Being in HKLM, these are not reachable
from the browser's own settings UI.

Run elevated. Re-runnable.
#>
[CmdletBinding()]
param([switch]$Undo)

$ErrorActionPreference = 'Stop'

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
  Write-Host 'Not elevated. Relaunching with admin rights (approve the UAC prompt)...'
  $a = @('-NoProfile','-ExecutionPolicy','Bypass','-NoExit','-File',('"{0}"' -f $PSCommandPath))
  if ($Undo) { $a += '-Undo' }
  Start-Process powershell.exe -Verb RunAs -ArgumentList $a
  return
}

$key = 'HKLM:\SOFTWARE\Policies\Opera Software\Opera'

if ($Undo) {
  if (Test-Path $key) { Remove-Item $key -Recurse -Force; Write-Host "Removed $key" }
  else { Write-Host 'Nothing to undo.' }
  return
}

if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }

# String policies.
$strings = @{
  # Kill Opera's own DoH resolver so name lookups go through the Windows resolver,
  # and therefore through AdGuard Home / Pi-hole and the hosts file.
  DnsOverHttpsMode = 'off'
  # Force direct connections. Opera's VPN is implemented as a browser proxy, so a
  # policy-pinned direct mode removes the mechanism it relies on.
  ProxyMode        = 'direct'
}
foreach ($n in $strings.Keys) {
  New-ItemProperty -Path $key -Name $n -Value $strings[$n] -PropertyType String -Force | Out-Null
  Write-Host ("  {0} = {1}" -f $n, $strings[$n])
}

# DWORD policies.
$dwords = @{
  # Use the OS resolver rather than Chromium's built-in async DNS client.
  BuiltInDnsClientEnabled = 0
  # Private browsing would not bypass DNS filtering, but it does hide history from a
  # parent, which is the other half of supervising an 8-year-old.
  IncognitoModeAvailability = 1
}
foreach ($n in $dwords.Keys) {
  New-ItemProperty -Path $key -Name $n -Value $dwords[$n] -PropertyType DWord -Force | Out-Null
  Write-Host ("  {0} = {1}" -f $n, $dwords[$n])
}

Write-Host ''
Write-Host "Applied to $key" -ForegroundColor Green
Write-Host @"

  Verify:   open Opera GX -> opera://policy  (policies must be listed as applied)
            open           -> opera://settings  (VPN toggle should be gone/greyed)
  Undo:     powershell -ExecutionPolicy Bypass -File "$PSCommandPath" -Undo

  NOTE: Opera must be fully closed and reopened for policies to load.
"@
