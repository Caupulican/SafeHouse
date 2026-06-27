#!/usr/bin/env bash
# Install the WEEKLY auto-update schedule (default: Sunday 04:00 local) that runs auto-update.sh.
# Prefers a systemd timer (safehouse-autoupdate.service + .timer); falls back to a cron drop-in
# (/etc/cron.d/safehouse-autoupdate) when systemd is unavailable. Idempotent — re-running just
# re-renders and reloads. Templates live in ../automation/ and are rendered with absolute paths.
# Needs root to write under /etc; self-elevates with sudo when not already root.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
AUTOMATION="$REPO/automation"
AUTOUPDATE="$REPO/scripts/auto-update.sh"
NAME="safehouse-autoupdate"
# Who the scheduled job should run as (needs docker-group access + repo ownership).
RUN_USER="${SAFEHOUSE_RUN_USER:-${SUDO_USER:-$(id -un)}}"

if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi

[ -f "$AUTOUPDATE" ] || { echo "ERROR: $AUTOUPDATE not found"; exit 1; }
chmod +x "$AUTOUPDATE" || true

render() {
  sed -e "s#__AUTOUPDATE_SH__#$AUTOUPDATE#g" \
      -e "s#__REPO__#$REPO#g" \
      -e "s#__USER__#$RUN_USER#g" "$1"
}

have_systemd() { command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; }

install_systemd() {
  local unit_dir=/etc/systemd/system
  echo "[*] Installing systemd timer ($NAME.timer) for user '$RUN_USER' -> $unit_dir"
  render "$AUTOMATION/$NAME.service" | $SUDO tee "$unit_dir/$NAME.service" >/dev/null
  render "$AUTOMATION/$NAME.timer"   | $SUDO tee "$unit_dir/$NAME.timer"   >/dev/null
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable --now "$NAME.timer"
  cat <<EOF
[✓] Installed systemd timer. Cadence: WEEKLY, Sunday 04:00 local.
    Verify:    systemctl status $NAME.timer && systemctl list-timers $NAME.timer
    Run now:   sudo systemctl start $NAME.service && journalctl -u $NAME.service -n 50
    Logs:      $REPO/logs/auto-update.log  (also: journalctl -u $NAME.service)
    Cadence:   edit OnCalendar in $unit_dir/$NAME.timer, then 'sudo systemctl daemon-reload'
    Uninstall: sudo systemctl disable --now $NAME.timer && sudo rm $unit_dir/$NAME.service $unit_dir/$NAME.timer && sudo systemctl daemon-reload
EOF
}

install_cron() {
  local cron_file=/etc/cron.d/$NAME
  echo "[*] systemd not available — installing cron drop-in for user '$RUN_USER' -> $cron_file"
  render "$AUTOMATION/$NAME.cron" | $SUDO tee "$cron_file" >/dev/null
  $SUDO chmod 0644 "$cron_file"
  cat <<EOF
[✓] Installed cron job. Cadence: WEEKLY, Sunday 04:00 local.
    Verify:    cat $cron_file
    Run now:   $AUTOUPDATE
    Logs:      $REPO/logs/auto-update.log
    Cadence:   edit the schedule fields in $cron_file
    Uninstall: sudo rm $cron_file
EOF
}

if have_systemd; then
  install_systemd
else
  install_cron
fi
