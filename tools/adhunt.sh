#!/usr/bin/env bash
# One-command ad-server hunt for Google Play Games (crosvm) on Windows.
# Captures the VM's :443 + :53 traffic with pktmon, exports pcapng, extracts TLS SNI + DNS,
# and flags ad/tracker hosts. Trigger ads in-game during the capture window.
# Usage: bash ~/pihole/adhunt.sh [seconds]   (default 45)
DUR="${1:-45}"
PS="/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
HERE="$(cd "$(dirname "$0")" && pwd)"

"$PS" -NoProfile -Command "
\$s = @'
New-Item -ItemType Directory -Force C:\Temp | Out-Null
Remove-Item C:\Temp\adhunt_done.txt,C:\Temp\adhunt.etl,C:\Temp\adhunt.pcapng -ErrorAction SilentlyContinue
pktmon filter remove | Out-Null
pktmon filter add h443 -p 443 | Out-Null
pktmon filter add h53 -p 53 | Out-Null
pktmon start --capture --pkt-size 0 --file-name C:\Temp\adhunt.etl | Out-Null
Start-Sleep -Seconds $DUR
pktmon stop | Out-Null
pktmon etl2pcap C:\Temp\adhunt.etl --out C:\Temp\adhunt.pcapng 2>&1 | Out-Null
pktmon filter remove | Out-Null
\"DONE\" | Out-File C:\Temp\adhunt_done.txt -Encoding ASCII
'@
\$p = Join-Path \$env:TEMP 'adhunt.ps1'
Set-Content -Path \$p -Value \$s -Encoding ASCII
Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File', \$p
" 2>&1 | tr -d '\r'

echo ">>> ${DUR}s capture running, APPROVE UAC, then TRIGGER ADS in the game <<<"
for i in $(seq 1 $((DUR/2 + 30))); do
  [ -f /mnt/c/Temp/adhunt_done.txt ] && { echo "capture complete"; break; }
  sleep 2
done
if [ -f /mnt/c/Temp/adhunt.pcapng ]; then
  python3 "$HERE/parse_cap.py" /mnt/c/Temp/adhunt.pcapng
else
  echo "no pcapng produced (UAC not approved, or no traffic)"
fi
