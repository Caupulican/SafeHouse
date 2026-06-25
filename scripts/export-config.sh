#!/usr/bin/env bash
# Pull the LIVE Pi-hole adlists + regex denylist back into the repo (source-of-truth backup).
# Run this after adding rules via the dashboard, then commit the changes.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
CONTAINER="${PIHOLE_CONTAINER:-pihole}"
GDB=/etc/pihole/gravity.db

docker exec "$CONTAINER" pihole-FTL sqlite3 "$GDB" "select address from adlist order by id;" \
  > "$REPO/blocklists/adlists.txt"
docker exec "$CONTAINER" pihole-FTL sqlite3 "$GDB" "select domain from domainlist where type=3 order by id;" \
  > "$REPO/blocklists/regex-denylist.txt"

echo "Exported $(wc -l < "$REPO/blocklists/adlists.txt") adlists, $(wc -l < "$REPO/blocklists/regex-denylist.txt") regex rules."
echo "Commit:  git -C \"$REPO\" add blocklists && git -C \"$REPO\" commit -m 'update blocklists'"
