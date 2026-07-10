# SafeHouse DNS persistence: after login, wake WSL + Pi-hole and force the Windows DNS
# Client through it. If Pi-hole cannot answer, fall back to the active IPv4 gateway.
# No machine-specific interface names, usernames, paths, or LAN addresses are stored here.
$ErrorActionPreference = 'Stop'
$nrptComment = 'SafeHouse: force Windows DNS Client through Pi-hole'
$logDir  = Join-Path $env:USERPROFILE '.pihole'
$logPath = Join-Path $logDir 'set-dns.log'

function Write-SafeHouseLog([string]$Message) {
  New-Item -ItemType Directory -Force -Path $logDir | Out-Null
  ('{0}  {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message) |
    Out-File -Append $logPath -Encoding ASCII
}

function Get-WslIp {
  $o = wsl.exe -e hostname -I 2>$null
  if ($o) {
    return (($o -split '\s+') | Where-Object { $_ -match '^\d+\.\d+\.\d+\.\d+$' } | Select-Object -First 1)
  }
  return $null
}

function Set-SafeHouseNrpt([string]$Server) {
  Get-DnsClientNrptRule -ErrorAction SilentlyContinue |
    Where-Object { $_.Comment -eq $nrptComment } |
    Remove-DnsClientNrptRule -Force -ErrorAction SilentlyContinue
  if ($Server) {
    Add-DnsClientNrptRule -Namespace '.' -NameServers $Server -Comment $nrptComment | Out-Null
  }
}

function Disable-BrowserSecureDns {
  # Keep host browsers on the Windows DNS Client, where the NRPT rule can enforce Pi-hole.
  foreach ($key in @(
    'HKLM:\SOFTWARE\Policies\Google\Chrome',
    'HKLM:\SOFTWARE\Policies\Microsoft\Edge'
  )) {
    New-Item -Path $key -Force | Out-Null
    New-ItemProperty -Path $key -Name 'DnsOverHttpsMode' -PropertyType String -Value 'off' -Force | Out-Null
  }
}

try {
  # Auto-detect the active default IPv4 route. The gateway doubles as the DNS failsafe.
  $route = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' |
    Where-Object { $_.NextHop -and $_.NextHop -ne '0.0.0.0' } |
    Sort-Object RouteMetric |
    Select-Object -First 1
  if (-not $route) { throw 'No active IPv4 default route found.' }

  $ifaceIndex = $route.InterfaceIndex
  $iface = (Get-NetAdapter -InterfaceIndex $ifaceIndex).Name
  $router = $route.NextHop

  # Wake WSL and read its current eth0 IP (retry while it boots).
  $ip = $null
  for ($t = 0; $t -lt 8 -and -not $ip; $t++) {
    $ip = Get-WslIp
    if (-not $ip) { Start-Sleep 3 }
  }

  $target = $router
  $mode = 'router-failsafe'
  if ($ip) {
    # Recreate Pi-hole if needed so port 53 is bound to the current WSL address. Docker
    # writes normal progress to stderr, so suppress native stderr without weakening the
    # error handling used by the Windows configuration commands below.
    $savedErrorPreference = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    wsl.exe -e bash -lc "cd ~/pihole && PIHOLE_BIND_IP=$ip docker compose up -d" 2>$null | Out-Null
    $composeExit = $LASTEXITCODE
    $ErrorActionPreference = $savedErrorPreference

    # Use Pi-hole only after it proves it can answer DNS.
    if ($composeExit -eq 0) {
      for ($i = 0; $i -lt 30; $i++) {
        try {
          Resolve-DnsName -Server $ip -Name example.com -DnsOnly -ErrorAction Stop | Out-Null
          $target = $ip
          $mode = 'pihole'
          break
        } catch {
          Start-Sleep 2
        }
      }
    }
  }

  Set-DnsClientServerAddress -InterfaceIndex $ifaceIndex -ServerAddresses $target
  Set-SafeHouseNrpt $target
  Disable-BrowserSecureDns
  Clear-DnsClientCache

  Write-SafeHouseLog ("mode={0} interface={1} wsl_ip={2} dns={3}" -f $mode, $iface, $ip, $target)
} catch {
  # Never leave a stale NRPT rule pointing at a dead WSL address.
  try { Set-SafeHouseNrpt $null } catch {}
  Write-SafeHouseLog ("ERROR: {0}" -f $_.Exception.Message)
  exit 1
}
