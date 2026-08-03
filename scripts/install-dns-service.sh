#!/usr/bin/env bash
# Install the DNS stack watchdog (boot + every 5 minutes) that runs reconcile-dns.sh.
#
# This is the layer that makes the filtering chain survive a WSL restart. Docker's
# `restart: unless-stopped` is set on both containers and is NOT sufficient on its own:
# a dockerd restart SIGKILLed AdGuard Home and left it down with RestartCount=0 while
# Pi-hole recovered. Because the Windows relay fails open to the router, that outage is
# invisible -- DNS keeps working and nothing is filtered.
#
# Systemd timer only: unlike the weekly auto-update there is no cron fallback, because a
# 5-minute watchdog on a machine without systemd is not something this repo can promise.
# WSL2 here runs systemd (/etc/wsl.conf sets systemd=true).
#
# Idempotent — re-running just re-renders and reloads. Templates live in ../automation/.
# A normal `deploy.sh --with-adguard` invokes this after installing the reconciler under
# ~/.local/bin; direct repair runs therefore expect that deployment step to have run once.
# Needs root to write under /etc; self-elevates with sudo when not already root.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
AUTOMATION="$REPO/automation"
NAME="safehouse-dns"
# Who the job runs as: needs docker-group access and owns the compose projects + creds.
RUN_USER="${SAFEHOUSE_RUN_USER:-${SUDO_USER:-$(id -un)}}"
RUN_HOME="${SAFEHOUSE_RUN_HOME:-$(getent passwd "$RUN_USER" | cut -d: -f6)}"
# The deployment role installs this stable copy before the automation role runs.  Point
# systemd at it rather than at a checkout (often under /mnt/c in WSL), which may move,
# be unmounted, or be subject to DrvFs execution semantics after installation.
RECONCILE="${SAFEHOUSE_RECONCILE_SH:-$RUN_HOME/.local/bin/safehouse-reconcile-dns}"

if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi

[ -f "$RECONCILE" ] || { echo "ERROR: $RECONCILE not found"; exit 1; }
[ -n "$RUN_HOME" ] || { echo "ERROR: could not resolve the home directory of '$RUN_USER'"; exit 1; }
chmod +x "$RECONCILE" || true

if ! { command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; }; then
  echo "ERROR: systemd is not available; cannot install the DNS watchdog." >&2
  echo "       Enable it in /etc/wsl.conf ([boot] systemd=true), then 'wsl --shutdown'." >&2
  exit 1
fi

render() {
  sed -e "s#__RECONCILE_SH__#$RECONCILE#g" \
      -e "s#__REPO__#$REPO#g" \
      -e "s#__HOME__#$RUN_HOME#g" \
      -e "s#__USER__#$RUN_USER#g" "$1"
}

unit_dir=/etc/systemd/system
echo "[*] Installing systemd watchdog ($NAME.timer) for user '$RUN_USER' -> $unit_dir"
render "$AUTOMATION/$NAME.service" | $SUDO tee "$unit_dir/$NAME.service" >/dev/null
render "$AUTOMATION/$NAME.timer"   | $SUDO tee "$unit_dir/$NAME.timer"   >/dev/null
$SUDO systemctl daemon-reload
$SUDO systemctl enable --now "$NAME.timer"

cat <<EOF
[✓] Installed. The filtering chain is rebuilt 15s after boot, then every 5 minutes.
    Verify:    systemctl list-timers $NAME.timer
    Run now:   sudo systemctl start $NAME.service && journalctl -u $NAME.service -n 30
    Logs:      journalctl -u $NAME.service
    Cadence:   edit OnUnitActiveSec in $unit_dir/$NAME.timer, then 'sudo systemctl daemon-reload'
    Uninstall: sudo systemctl disable --now $NAME.timer && sudo rm $unit_dir/$NAME.service $unit_dir/$NAME.timer && sudo systemctl daemon-reload
EOF
