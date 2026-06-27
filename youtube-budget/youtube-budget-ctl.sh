#!/usr/bin/env bash
# SafeHouse — YouTube budget parent CLI.
#
# Operate the budget by hand, consistently with the daemon (same STATE_FILE + same
# byte-preserving hosts block). State + hosts live root-side, so run these with the
# privileges the daemon has (typically `sudo` on the WSL host).
#
#   youtube-budget-ctl.sh status              show used / remaining / limit / blocked
#   youtube-budget-ctl.sh set-limit <minutes> change today's + the default daily limit
#   youtube-budget-ctl.sh grant <minutes>     add bonus minutes for today (unblocks if it frees budget)
#   youtube-budget-ctl.sh block               force the YouTube block ON now
#   youtube-budget-ctl.sh allow               force OFF now (override until the threshold is crossed again)
#   youtube-budget-ctl.sh reset               zero today's usage + bonus and unblock
set -euo pipefail

# shellcheck source=lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
yb_load_config

usage() {
  cat <<'EOF'
SafeHouse — YouTube budget parent CLI

  youtube-budget-ctl.sh status              show used / remaining / limit / blocked
  youtube-budget-ctl.sh set-limit <minutes> change today's + the default daily limit
  youtube-budget-ctl.sh grant <minutes>     add bonus minutes for today (unblocks if it frees budget)
  youtube-budget-ctl.sh block               force the YouTube block ON now
  youtube-budget-ctl.sh allow               force OFF now (override until the threshold is crossed again)
  youtube-budget-ctl.sh reset               zero today's usage + bonus and unblock

State + hosts edits are root-side; run with sudo on the WSL host.
EOF
  exit "${1:-0}"
}

# --- load state into locals (init defaults, honour day rollover) -------------
load_state() {
  TODAY="$(yb_today)"
  if yb_state_exists; then
    S_DATE="$(yb_state_get date)"
    S_USED="$(yb_state_get seconds_used)"
    S_LIMIT="$(yb_state_get limit_min)"
    S_BLOCKED="$(yb_state_get blocked_by_budget)"
    S_BONUS="$(yb_state_get bonus_sec)"
  else
    S_DATE="$TODAY"; S_USED=0; S_LIMIT="$DAILY_LIMIT_MIN"; S_BLOCKED=false; S_BONUS=0
  fi
  [ -z "$S_USED" ]    && S_USED=0
  [ -z "$S_LIMIT" ]   && S_LIMIT="$DAILY_LIMIT_MIN"
  [ -z "$S_BLOCKED" ] && S_BLOCKED=false
  [ -z "$S_BONUS" ]   && S_BONUS=0
  # If the stored day is stale, present/operate on a fresh day (matches the daemon).
  if [ "$S_DATE" != "$TODAY" ]; then
    S_DATE="$TODAY"; S_USED=0; S_BONUS=0; S_BLOCKED=false
  fi
}

save_state() { yb_state_write "$S_DATE" "$S_USED" "$S_LIMIT" "$S_BLOCKED" "$S_BONUS"; }

is_int() { case "$1" in ''|*[!0-9]*) return 1;; *) return 0;; esac; }

fmt_hms() { # seconds -> "Hh Mm Ss" (compact)
  local s="$1" h m
  h=$(( s / 3600 )); m=$(( (s % 3600) / 60 )); s=$(( s % 60 ))
  if [ "$h" -gt 0 ]; then printf '%dh %02dm %02ds' "$h" "$m" "$s"
  elif [ "$m" -gt 0 ]; then printf '%dm %02ds' "$m" "$s"
  else printf '%ds' "$s"; fi
}

eff_limit() { echo $(( S_LIMIT * 60 + S_BONUS )); }

# Update DAILY_LIMIT_MIN in config.env in place (creates/replaces the line).
config_set_limit() {
  local cfg="${YB_CONFIG:-$YB_DIR/config.env}" min="$1"
  [ -f "$cfg" ] || return 0
  if grep -q '^DAILY_LIMIT_MIN=' "$cfg"; then
    sed -i "s/^DAILY_LIMIT_MIN=.*/DAILY_LIMIT_MIN=$min/" "$cfg"
  else
    printf 'DAILY_LIMIT_MIN=%s\n' "$min" >> "$cfg"
  fi
}

cmd_status() {
  load_state
  local limit rem used="$S_USED"
  limit="$(eff_limit)"
  rem=$(( limit - used )); [ "$rem" -lt 0 ] && rem=0
  local blk_human="ALLOWED"; [ "$S_BLOCKED" = "true" ] && blk_human="BLOCKED (budget)"
  # Reflect what the hosts file actually says, too.
  local hosts_state="allowed"; if yb_hosts_is_blocked; then hosts_state="blocked"; fi
  echo   "YouTube watch-time budget — $TODAY"
  echo   "  Used today : $(fmt_hms "$used")"
  echo   "  Limit      : $(fmt_hms "$limit")  (${S_LIMIT}m daily + $(fmt_hms "$S_BONUS") bonus)"
  echo   "  Remaining  : $(fmt_hms "$rem")"
  echo   "  Budget     : $blk_human"
  printf '  Hosts file : YouTube section %s in %s\n' "$hosts_state" "$HOSTS_PATH"
}

cmd_set_limit() {
  is_int "${1:-}" || { echo "set-limit needs a whole number of minutes" >&2; exit 2; }
  load_state
  S_LIMIT="$1"
  config_set_limit "$1"
  # If a higher limit frees today's budget, lift the auto block.
  if [ "$S_BLOCKED" = "true" ] && [ "$S_USED" -lt "$(eff_limit)" ]; then
    yb_hosts_unblock auto >/dev/null || true; yb_flush_dns; S_BLOCKED=false
    echo "Limit raised — budget freed, YouTube unblocked."
  fi
  save_state
  echo "Daily limit set to ${S_LIMIT}m (today + config.env)."
}

cmd_grant() {
  is_int "${1:-}" || { echo "grant needs a whole number of minutes" >&2; exit 2; }
  load_state
  S_BONUS=$(( S_BONUS + $1 * 60 ))
  if [ "$S_BLOCKED" = "true" ] && [ "$S_USED" -lt "$(eff_limit)" ]; then
    yb_hosts_unblock auto >/dev/null || true; yb_flush_dns; S_BLOCKED=false
    echo "Granted $1m — budget freed, YouTube unblocked."
  else
    echo "Granted $1m of bonus for today."
  fi
  save_state
}

cmd_block() {
  load_state
  local res; res="$(yb_hosts_block)" || true
  case "$res" in
    BLOCKED*|ALREADY) S_BLOCKED=true; yb_flush_dns; echo "YouTube BLOCKED now ($res).";;
    NOLIST) echo "Cannot block — host list missing: $BLOCKLIST_FILE" >&2; exit 1;;
    *) echo "Block failed (out='$res')" >&2; exit 1;;
  esac
  save_state
}

cmd_allow() {
  load_state
  yb_hosts_unblock any >/dev/null || true
  yb_flush_dns
  # Give a one-sample headroom so a no-activity tick won't instantly re-arm; the
  # daemon re-blocks only when fresh watching crosses the limit again.
  local limit; limit="$(eff_limit)"
  if [ "$S_USED" -ge "$limit" ]; then
    S_BONUS=$(( S_BONUS + (S_USED - limit) + SAMPLE_SEC ))
  fi
  S_BLOCKED=false
  save_state
  echo "YouTube ALLOWED now (override). Daemon re-blocks once today's watching crosses the limit again."
}

cmd_reset() {
  load_state
  S_USED=0; S_BONUS=0
  yb_hosts_unblock any >/dev/null || true
  yb_flush_dns
  S_BLOCKED=false
  save_state
  echo "Reset — today's usage zeroed and YouTube unblocked (limit ${S_LIMIT}m)."
}

main() {
  local sub="${1:-status}"; shift || true
  case "$sub" in
    status)            cmd_status "$@";;
    set-limit)         cmd_set_limit "$@";;
    grant)             cmd_grant "$@";;
    block)             cmd_block "$@";;
    allow)             cmd_allow "$@";;
    reset)             cmd_reset "$@";;
    -h|--help|help)    usage 0;;
    *) echo "unknown subcommand: $sub" >&2; usage 2;;
  esac
}

main "$@"
