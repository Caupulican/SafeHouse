#!/usr/bin/env bash
# Sole WSL owner for starting the stable host-network DNS stack.
set -euo pipefail

PIHOLE_DIR="${PIHOLE_DIR:-$HOME/pihole}"
ADGUARD_DIR="${ADGUARD_DIR:-$HOME/adguard}"
for dir in "$PIHOLE_DIR" "$ADGUARD_DIR"; do
  test -f "$dir/docker-compose.yml" || {
    echo "ERROR: missing $dir/docker-compose.yml" >&2
    exit 1
  }
done

(cd "$PIHOLE_DIR" && docker compose up -d)
(cd "$ADGUARD_DIR" && docker compose up -d)

# Converge the LIVE restart policy to the declared one. `restart: unless-stopped` is read
# only when a container is CREATED, and compose does not count a policy change as drift
# worth recreating for -- so a container created before that line landed keeps `no`
# forever while every compose file in the repo claims otherwise.
#
# That is not cosmetic. On 2026-08-01 a WSL shutdown stopped both containers at 01:12,
# dockerd came back up without them because both still carried `no`, and the Windows relay
# failed open to the LAN router for the next fourteen hours. Name resolution kept working,
# so nothing looked broken -- there was simply no filtering at all, and the in-game ads in
# the Play Games VM came straight back.
#
# This must never fail the reconcile: the stack is already up by this point, and a
# non-zero exit here makes the caller conclude the stack is down and point Windows at the
# unfiltered router -- causing the very outage this line exists to prevent.
docker update --restart unless-stopped pihole adguardhome >/dev/null 2>&1 ||
  echo "WARNING: restart policy not set; the stack will not survive a WSL restart" >&2

python3 "$ADGUARD_DIR/bootstrap.py" \
  --base-url http://127.0.0.1:3000 \
  --upstream 127.0.0.1:5353 \
  --password-file "$ADGUARD_DIR/.admin-password" \
  --skip-filters

docker exec pihole sh -c 'nslookup github.com 127.0.0.1' >/dev/null
docker exec adguardhome sh -c 'nslookup github.com 127.0.0.1' >/dev/null
# Resolution alone is not filtering. The Windows relay deliberately falls back to the
# router during an outage, so a plain github.com probe stays green in the exact state the
# watchdog exists to repair. Require a known Pi-hole entry to reach a sinkhole address
# through AdGuard, proving the complete chain rather than only its transport.
docker exec adguardhome sh -c \
  "nslookup doubleclick.net 127.0.0.1 | grep -Eq 'Address:[[:space:]]+(0\\.0\\.0\\.0|::)$'"
printf 'SafeHouse DNS stack reconciled on WSL host networking\n'
