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

python3 "$ADGUARD_DIR/bootstrap.py" \
  --base-url http://127.0.0.1:3000 \
  --upstream 127.0.0.1:5353 \
  --password-file "$ADGUARD_DIR/.admin-password" \
  --skip-filters

docker exec pihole sh -c 'nslookup github.com 127.0.0.1' >/dev/null
docker exec adguardhome sh -c 'nslookup github.com 127.0.0.1' >/dev/null
printf 'SafeHouse DNS stack reconciled on WSL host networking\n'
