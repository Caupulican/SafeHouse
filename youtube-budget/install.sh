#!/usr/bin/env bash
# Install (ARM) the SafeHouse YouTube watch-time budget daemon.
#
#   *** RUNNING THIS ARMS LIVE ENFORCEMENT. ***
#   From here on, once a day's measured YouTube watching crosses the limit, the daemon
#   will splice a YouTube block into the LIVE Windows hosts file and flush DNS — i.e.
#   it will actually start blocking YouTube on this machine. Disarm with the Uninstall
#   command printed at the end.
#
# Prefers a systemd service (safehouse-youtube-budget.service, Restart=always) running
# the daemon loop; falls back to a cron @reboot drop-in + an immediate nohup launch when
# systemd is unavailable. Idempotent — re-running re-renders and reloads. Templates live
# in ../automation/ and are rendered with absolute paths. Needs root (writes under /etc
# and /var/lib, and edits the hosts file); self-elevates with sudo when not already root.
set -euo pipefail
YB_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$YB_DIR/.." && pwd)"
AUTOMATION="$REPO/automation"
DAEMON_SH="$YB_DIR/youtube-budget.sh"
CTL_SH="$YB_DIR/youtube-budget-ctl.sh"
NAME="safehouse-youtube-budget"
STATE_DIR="/var/lib/safehouse"

if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi

[ -f "$DAEMON_SH" ] || { echo "ERROR: $DAEMON_SH not found"; exit 1; }
chmod +x "$DAEMON_SH" "$CTL_SH" "$YB_DIR/lib.sh" 2>/dev/null || true

# State dir: root-owned, private (the daemon also mkdir -p's it, but pre-create 0700).
$SUDO mkdir -p "$STATE_DIR"
$SUDO chmod 700 "$STATE_DIR"

render() {
  sed -e "s#__DAEMON_SH__#$DAEMON_SH#g" \
      -e "s#__REPO__#$REPO#g" "$1"
}

have_systemd() { command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; }

arm_note() {
  cat <<EOF

[!] ARMED: live YouTube budget enforcement is now active.
    Daily limit / sampling are in $YB_DIR/config.env
    Parent CLI:  sudo $CTL_SH status | set-limit <m> | grant <m> | block | allow | reset
    Log:         $REPO/logs/youtube-budget.log
EOF
}

install_systemd() {
  local unit_dir=/etc/systemd/system
  echo "[*] Installing systemd service ($NAME.service) -> $unit_dir"
  render "$AUTOMATION/$NAME.service" | $SUDO tee "$unit_dir/$NAME.service" >/dev/null
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable --now "$NAME.service"
  cat <<EOF
[✓] Installed + started systemd service (Restart=always).
    Verify:    systemctl status $NAME.service && journalctl -u $NAME.service -n 50
    Stop:      sudo systemctl stop $NAME.service        # pause enforcement
    Logs:      $REPO/logs/youtube-budget.log  (also: journalctl -u $NAME.service)
    Uninstall: sudo systemctl disable --now $NAME.service && sudo rm $unit_dir/$NAME.service && sudo systemctl daemon-reload
EOF
  arm_note
}

install_cron() {
  local cron_file=/etc/cron.d/$NAME
  echo "[*] systemd not available — installing cron @reboot drop-in -> $cron_file"
  render "$AUTOMATION/$NAME.cron" | $SUDO tee "$cron_file" >/dev/null
  $SUDO chmod 0644 "$cron_file"
  # Start now so you don't have to reboot. Avoid a duplicate if it's already running.
  if ! pgrep -f "$DAEMON_SH" >/dev/null 2>&1; then
    $SUDO nohup "$DAEMON_SH" >>"$REPO/logs/youtube-budget.log" 2>&1 &
    echo "[*] Launched daemon via nohup (pid $!)."
  else
    echo "[*] Daemon already running — left as is."
  fi
  cat <<EOF
[✓] Installed cron @reboot launcher + started the daemon now.
    Verify:    cat $cron_file && pgrep -af youtube-budget.sh
    Stop:      sudo pkill -f $DAEMON_SH                 # pause enforcement
    Logs:      $REPO/logs/youtube-budget.log
    Uninstall: sudo rm $cron_file && sudo pkill -f $DAEMON_SH
EOF
  arm_note
}

if have_systemd; then
  install_systemd
else
  install_cron
fi
