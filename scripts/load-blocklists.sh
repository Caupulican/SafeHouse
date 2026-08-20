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

echo "[*] Loading parental denylist..."
while IFS= read -r rx; do
  [ -z "$rx" ] && continue
  case "$rx" in \#*) continue ;; esac
  esc="${rx//\'/\'\'}"
  sql "INSERT OR IGNORE INTO domainlist (type,domain,enabled,comment) VALUES (3,'$esc',1,'SafeHouse-Parental');"
done < "$REPO/blocklists/parental-denylist.txt"

# Type 0 = exact allow, which beats gravity. Loaded last only for readability; order does
# not matter to Pi-hole. Without this an allowlist lives solely in the container and dies
# with the volume - and a re-blocked false positive shows up as a game that stopped
# signing in, weeks later, with nothing pointing back at the rebuild.
echo "[*] Loading allowlist..."
while IFS= read -r dom; do
  [ -z "$dom" ] && continue
  case "$dom" in \#*) continue ;; esac
  esc="${dom//\'/\'\'}"
  sql "INSERT OR IGNORE INTO domainlist (type,domain,enabled,comment) VALUES (0,'$esc',1,'SafeHouse-Allow');"
done < "$REPO/blocklists/allowlist.txt"

echo "[*] Rebuilding gravity (downloads lists, ~1-3 min)..."
docker exec "$CONTAINER" pihole -g

echo "[✓] adlists=$(sql 'select count(*) from adlist;') regex=$(sql 'select count(*) from domainlist where type=3;') parental=$(sql "select count(*) from domainlist where type=3 and comment='SafeHouse-Parental';") allowed=$(sql "select count(*) from domainlist where type=0 and comment='SafeHouse-Allow';")"
