#!/usr/bin/env bash
# SafeHouse auto-update: re-apply the repo blocklists and rebuild Pi-hole gravity so the
# adlist contents (ads + NSFW) re-download. This IS the "auto update" — there is no live
# state to mutate beyond what load-blocklists.sh already does, so it is safe to run on a
# schedule and safe to run repeatedly (idempotent, INSERT OR IGNORE under the hood).
# Logs with timestamps to logs/auto-update.log. Exits non-zero (gracefully) if Pi-hole/Docker
# is down. Usage: ./scripts/auto-update.sh   (see install-autoupdate.sh for the weekly schedule)
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
CONTAINER="${PIHOLE_CONTAINER:-pihole}"
LOG="$REPO/logs/auto-update.log"

mkdir -p "$REPO/logs"
log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG"; }

log "[*] auto-update start (repo=$REPO container=$CONTAINER)"

# Optional, guarded self-update: only when explicitly opted in AND the working tree is clean,
# so a scheduled pull can never clobber local edits awaiting review.
if [ "${SAFEHOUSE_GIT_PULL:-0}" = "1" ]; then
  if git -C "$REPO" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
     && [ -z "$(git -C "$REPO" status --porcelain)" ]; then
    log "[*] git pull (clean tree, SAFEHOUSE_GIT_PULL=1)..."
    if git -C "$REPO" pull --ff-only >>"$LOG" 2>&1; then
      log "[*] git pull done"
    else
      log "[!] git pull failed (continuing with the current checkout)"
    fi
  else
    log "[*] skip git pull (working tree dirty or not a git repo)"
  fi
else
  log "[*] skip git pull (set SAFEHOUSE_GIT_PULL=1 to enable)"
fi

# Re-apply repo blocklists + rebuild gravity (this re-downloads every adlist's contents).
if PIHOLE_CONTAINER="$CONTAINER" "$REPO/scripts/load-blocklists.sh" >>"$LOG" 2>&1; then
  log "[✓] auto-update done"
else
  rc=$?
  log "[!] load-blocklists.sh failed (rc=$rc) — is Docker/Pi-hole up? Full output above in $LOG"
  exit "$rc"
fi
