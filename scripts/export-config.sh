#!/usr/bin/env bash
# Pull the LIVE Pi-hole adlists + regex denylist back into the repo (source-of-truth backup).
# Run this after adding rules via the dashboard, then commit the changes.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
CONTAINER="${PIHOLE_CONTAINER:-pihole}"
GDB=/etc/pihole/gravity.db

docker exec "$CONTAINER" pihole-FTL sqlite3 "$GDB" "select address from adlist order by id;" \
  > "$REPO/blocklists/adlists.txt"

# Ad-network regex (everything type 3 that is NOT the parental layer) -> regex-denylist.txt
docker exec "$CONTAINER" pihole-FTL sqlite3 "$GDB" \
  "select domain from domainlist where type=3 and (comment is null or comment<>'SafeHouse-Parental') order by id;" \
  > "$REPO/blocklists/regex-denylist.txt"

# Parental layer (comment 'SafeHouse-Parental') -> parental-denylist.txt, keeping its public-safety
# header so adult content stays a referenced URL (in adlists.txt) and never a domain literal here.
PARENTAL="$REPO/blocklists/parental-denylist.txt"
cat > "$PARENTAL" <<'EOF'
# Parental content layer (DNS) — Pi-hole regex, one per line.
# Loaded by ../scripts/load-blocklists.sh as type 3 (regex deny), comment 'SafeHouse-Parental'.
# PUBLIC-REPO SAFE: product/service domains by NAME only. Adult / NSFW content is NOT listed here —
# it is covered network-wide via an external NSFW blocklist URL in adlists.txt (a link, never the
# domains). The lines below were exported live from Pi-hole; do not paste adult domains here.
EOF
docker exec "$CONTAINER" pihole-FTL sqlite3 "$GDB" \
  "select domain from domainlist where type=3 and comment='SafeHouse-Parental' order by id;" \
  >> "$PARENTAL"

echo "Exported $(wc -l < "$REPO/blocklists/adlists.txt") adlists, $(wc -l < "$REPO/blocklists/regex-denylist.txt") regex rules, $(docker exec "$CONTAINER" pihole-FTL sqlite3 "$GDB" "select count(*) from domainlist where type=3 and comment='SafeHouse-Parental';") parental rules."
echo "Commit:  git -C \"$REPO\" add blocklists && git -C \"$REPO\" commit -m 'update blocklists'"
