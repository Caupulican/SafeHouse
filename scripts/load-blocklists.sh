#!/usr/bin/env bash
# Load adlists + regex denylist from blocklists/ into the running Pi-hole, then rebuild gravity.
# Idempotent (INSERT OR IGNORE). Usage: ./scripts/load-blocklists.sh
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
CONTAINER="${PIHOLE_CONTAINER:-pihole}"
GDB=/etc/pihole/gravity.db

sql() { docker exec "$CONTAINER" pihole-FTL sqlite3 "$GDB" "$1"; }

echo "[*] Loading adlists..."
while IFS= read -r url; do
  [ -z "$url" ] && continue
  case "$url" in \#*) continue ;; esac
  esc="${url//\'/\'\'}"
  sql "INSERT OR IGNORE INTO adlist (address,comment) VALUES ('$esc','SafeHouse');"
done < "$REPO/blocklists/adlists.txt"

echo "[*] Loading regex denylist..."
while IFS= read -r rx; do
  [ -z "$rx" ] && continue
  case "$rx" in \#*) continue ;; esac
  esc="${rx//\'/\'\'}"
  sql "INSERT OR IGNORE INTO domainlist (type,domain,enabled,comment) VALUES (3,'$esc',1,'SafeHouse');"
done < "$REPO/blocklists/regex-denylist.txt"

echo "[*] Rebuilding gravity (downloads lists, ~1-3 min)..."
docker exec "$CONTAINER" pihole -g

echo "[✓] adlists=$(sql 'select count(*) from adlist;') regex=$(sql 'select count(*) from domainlist where type=3;')"
