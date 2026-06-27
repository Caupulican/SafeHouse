#!/usr/bin/env bash
# SafeHouse — YouTube daily watch-time budget DAEMON.
#
# Runs on the Pi-hole/WSL (Debian) side. Every SAMPLE_SEC it:
#   1. DETECT  — asks Pi-hole's FTL query log whether any *allowed* YouTube-content
#                query happened in the last WINDOW_SEC (AdAway-style: we key off the
#                very hostnames the manual block would 0.0.0.0). Browser/app-agnostic.
#   2. ACCOUNT — bills SAMPLE_SEC against today's budget in STATE_FILE (atomic JSON),
#                resetting at the local-day boundary.
#   3. ENFORCE — once seconds_used >= limit_min*60 + bonus_sec, splices the YouTube
#                hosts block into the live Windows hosts file (byte-preserving) and
#                flushes Windows DNS. Removes it again on the daily reset.
#
# It is tamper-resistant by placement: the kid uses Windows and has no WSL account,
# so detection, accounting and enforcement all live where they cannot reach.
#
# This is the loop that the systemd service / nohup fallback supervises (see
# install.sh). Run it directly to foreground-test:  ./youtube-budget.sh
# Resilient: if Pi-hole/Docker is down it logs and SKIPS the sample (never crashes,
# never accumulates phantom time).
set -euo pipefail

# shellcheck source=lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
yb_load_config

# --- read or initialise today's state ---------------------------------------
yb_init_state_if_missing() {
  if ! yb_state_exists; then
    yb_state_write "$(yb_today)" 0 "$DAILY_LIMIT_MIN" false 0
    yb_log "[init] created state $STATE_FILE (limit=${DAILY_LIMIT_MIN}m)"
  fi
}

tick() {
  local today date seconds_used limit_min blocked bonus active limit
  today="$(yb_today)"

  yb_init_state_if_missing
  date="$(yb_state_get date)"
  seconds_used="$(yb_state_get seconds_used)"
  limit_min="$(yb_state_get limit_min)"
  blocked="$(yb_state_get blocked_by_budget)"
  bonus="$(yb_state_get bonus_sec)"
  [ -z "$seconds_used" ] && seconds_used=0
  [ -z "$limit_min" ]    && limit_min="$DAILY_LIMIT_MIN"
  [ -z "$blocked" ]      && blocked=false
  [ -z "$bonus" ]        && bonus=0

  # 1) Daily reset (local-date rollover): zero usage + bonus, lift the auto block.
  if [ "$date" != "$today" ]; then
    if [ "$blocked" = "true" ]; then
      yb_hosts_unblock auto >/dev/null || true
      yb_flush_dns
      yb_log "[reset] new day $today — removed auto YouTube block"
    fi
    date="$today"; seconds_used=0; bonus=0; blocked=false
    yb_state_write "$date" "$seconds_used" "$limit_min" "$blocked" "$bonus"
    yb_log "[reset] new day $today — usage zeroed (limit=${limit_min}m)"
  fi

  # 2) Detect (resilient: skip the sample if the infra is down).
  if ! active="$(yb_detect)"; then
    yb_log "[skip] Pi-hole/Docker unreachable — sample skipped (no accumulation)"
    return 0
  fi

  # 3) Account — only bill when watching AND not already budget-blocked.
  if [ "$active" = "1" ] && [ "$blocked" != "true" ]; then
    seconds_used=$(( seconds_used + SAMPLE_SEC ))
  fi

  # 4) Enforce.
  limit=$(( limit_min * 60 + bonus ))
  if [ "$seconds_used" -ge "$limit" ] && [ "$blocked" != "true" ]; then
    local res
    res="$(yb_hosts_block)" || true
    case "$res" in
      BLOCKED*|ALREADY)
        blocked=true
        yb_flush_dns
        yb_log "[enforce] budget reached (${seconds_used}s >= ${limit}s) — YouTube BLOCKED ($res)"
        ;;
      NOLIST)
        yb_log "[error] enforcement host list missing ($BLOCKLIST_FILE) — cannot block"
        ;;
      *)
        yb_log "[error] hosts block failed (out='$res')"
        ;;
    esac
  fi

  yb_state_write "$date" "$seconds_used" "$limit_min" "$blocked" "$bonus"

  if [ "$active" = "1" ]; then
    yb_log "[sample] active +${SAMPLE_SEC}s used=${seconds_used}s/${limit}s blocked=${blocked}"
  fi
}

main() {
  yb_log "[start] youtube-budget daemon (sample=${SAMPLE_SEC}s window=${WINDOW_SEC}s limit=${DAILY_LIMIT_MIN}m container=${PIHOLE_CONTAINER})"
  while true; do
    # Never let one bad sample kill the daemon.
    tick || yb_log "[warn] tick failed (continuing)"
    sleep "$SAMPLE_SEC"
  done
}

main "$@"
