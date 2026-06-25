# PiholeGPG persistence: after login, wake WSL + Pi-hole and point Windows DNS at it.
# Failsafe: if Pi-hole can't be reached, fall back to the router so the internet still works.
$ErrorActionPreference = 'SilentlyContinue'
$iface  = 'Ethernet'
$router = '192.168.1.1'   # <-- CHANGE to your router/gateway IP (failsafe DNS)

function Get-WslIp {
  $o = wsl.exe -e hostname -I 2>$null
  if ($o) {
    return (($o -split '\s+') | Where-Object { $_ -match '^\d+\.\d+\.\d+\.\d+$' } | Select-Object -First 1)
  }
  return $null
}

# 1) Wake WSL (systemd -> docker -> pihole) and read its current eth0 IP (retry while it boots)
$ip = $null
for ($t = 0; $t -lt 8 -and -not $ip; $t++) { $ip = Get-WslIp; if (-not $ip) { Start-Sleep 3 } }

if ($ip) {
  # 2) Ensure the Pi-hole container is up and bound to the *current* IP
  wsl.exe -e bash -lc "cd ~/pihole && PIHOLE_BIND_IP=$ip docker compose up -d" 2>$null | Out-Null
  # 3) Wait until Pi-hole actually answers DNS on that IP
  $ok = $false
  for ($i = 0; $i -lt 30; $i++) {
    try { Resolve-DnsName -Server $ip -Name example.com -ErrorAction Stop | Out-Null; $ok = $true; break }
    catch { Start-Sleep 2 }
  }
  if ($ok) { Set-DnsClientServerAddress -InterfaceAlias $iface -ServerAddresses $ip }
  else     { Set-DnsClientServerAddress -InterfaceAlias $iface -ServerAddresses $router }
} else {
  Set-DnsClientServerAddress -InterfaceAlias $iface -ServerAddresses $router
}
ipconfig /flushdns | Out-Null

# Log outcome for verification
$ts  = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
$cur = (Get-DnsClientServerAddress -InterfaceAlias $iface -AddressFamily IPv4).ServerAddresses -join ','
"$ts  wsl_ip=$ip  ethernet_dns=$cur" | Out-File -Append "$env:USERPROFILE\.pihole\set-dns.log" -Encoding ASCII
