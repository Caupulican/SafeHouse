#!/usr/bin/env bash
# SafeHouse one-shot setup (no Ansible required).
# Deploys Pi-hole to $HOME/pihole, loads blocklists, rebuilds gravity, stages Windows scripts.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PIHOLE_DIR="${PIHOLE_DIR:-$HOME/pihole}"
CONTAINER="${PIHOLE_CONTAINER:-pihole}"

command -v docker >/dev/null || { echo "ERROR: docker not found. Install: https://get.docker.com"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "ERROR: 'docker compose' plugin missing."; exit 1; }

echo "[*] Deploying to $PIHOLE_DIR"
mkdir -p "$PIHOLE_DIR"
cp "$REPO/pihole/docker-compose.yml" "$PIHOLE_DIR/docker-compose.yml"
cp "$REPO/tools/adhunt.sh" "$REPO/tools/parse_cap.py" "$PIHOLE_DIR/"
chmod +x "$PIHOLE_DIR/adhunt.sh"

IP="$(hostname -I | cut -d' ' -f1)"
echo "[*] WSL eth0 IP = $IP"
sed "s/^PIHOLE_BIND_IP=.*/PIHOLE_BIND_IP=$IP/" "$REPO/pihole/.env.example" > "$PIHOLE_DIR/.env"

echo "[*] Starting Pi-hole..."
( cd "$PIHOLE_DIR" && docker compose up -d )

echo "[*] Waiting for Pi-hole to answer DNS..."
for _ in $(seq 1 40); do
  docker exec "$CONTAINER" dig +short github.com @127.0.0.1 >/dev/null 2>&1 && break
  sleep 2
done

PIHOLE_CONTAINER="$CONTAINER" "$REPO/scripts/load-blocklists.sh"

echo "[*] Staging Windows scripts..."
mkdir -p /mnt/c/Temp 2>/dev/null || true
cp "$REPO/windows/mktask.ps1" /mnt/c/Temp/mktask.ps1 2>/dev/null || true

# Deploy the connection-layer ad blocker toolkit to a Windows-native working folder.
mkdir -p /mnt/c/SafeHouse/windows /mnt/c/SafeHouse/blocklists /mnt/c/SafeHouse/logs 2>/dev/null || true
cp "$REPO/windows/safehouse-adblock.ps1" /mnt/c/SafeHouse/windows/ 2>/dev/null || true
cp "$REPO/blocklists/ad-ip-ranges.txt" "$REPO/blocklists/ad-watchlist.txt" /mnt/c/SafeHouse/blocklists/ 2>/dev/null || true
echo "    safehouse-adblock.ps1 + ad-ip-ranges.txt + ad-watchlist.txt -> C:\\SafeHouse\\"
WINPROFILE="$(wslpath "$(cmd.exe /c 'echo %USERPROFILE%' 2>/dev/null | tr -d '\r')" 2>/dev/null || true)"
if [ -n "${WINPROFILE:-}" ] && [ -d "$WINPROFILE" ]; then
  mkdir -p "$WINPROFILE/.pihole"
  cp "$REPO/windows/set-dns.ps1" "$WINPROFILE/.pihole/set-dns.ps1"
  echo "    set-dns.ps1 -> $WINPROFILE/.pihole/"
fi

cat <<EOF

[✓] Pi-hole is up:  http://localhost:8053/admin   (DNS on ${IP}:53)

NEXT: one-time admin steps (see windows/README.md):
  1) Point Windows DNS at Pi-hole (elevated PowerShell):
       Set-DnsClientServerAddress -InterfaceAlias 'Ethernet' -ServerAddresses ${IP}
  2) Arm the reboot-proof logon task (elevated PowerShell):
       powershell -ExecutionPolicy Bypass -File C:\\Temp\\mktask.ps1
  3) Arm the connection-layer ad blocker (elevated PowerShell, with ads showing):
       powershell -ExecutionPolicy Bypass -File C:\\SafeHouse\\windows\\safehouse-adblock.ps1
  4) RESTART Google Play Games so the crosvm VM picks up Pi-hole.
EOF
