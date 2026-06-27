#!/usr/bin/env bash
# SafeHouse — YouTube budget shared library.
# Sourced by youtube-budget.sh (daemon) and youtube-budget-ctl.sh (parent CLI).
# Provides: config loading, local-date helper, atomic JSON state read/write,
# byte-preserving hosts block/unblock (mirrors windows/parental-toggle.ps1),
# Pi-hole FTL detection, DNS flush, and logging. No side effects on source.
#
# Conventions reused from the repo:
#   * docker exec "$PIHOLE_CONTAINER" pihole-FTL sqlite3 ...  (load-blocklists.sh)
#   * hosts marker  "# === Parental block: <Name> ... ===" / "# === end <Name> block ==="
#   * mixed CRLF(header)/LF(body) hosts file is spliced, never re-encoded.

# --- locations (resolved relative to this file) ------------------------------
YB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$YB_DIR/.." && pwd)"
DETECT_DOMAINS_FILE="${DETECT_DOMAINS_FILE:-$YB_DIR/detect-domains.txt}"
LOG_FILE="${YB_LOG_FILE:-$REPO/logs/youtube-budget.log}"

# --- config ------------------------------------------------------------------
# Source config.env, then let any pre-set environment variable win (overridable).
yb_load_config() {
  local cfg="${YB_CONFIG:-$YB_DIR/config.env}"
  # Snapshot env overrides so they take precedence over the file.
  local _DAILY="${DAILY_LIMIT_MIN:-}" _SAMPLE="${SAMPLE_SEC:-}" _WINDOW="${WINDOW_SEC:-}"
  local _CONT="${PIHOLE_CONTAINER:-}" _HOSTS="${HOSTS_PATH:-}" _STATE="${STATE_FILE:-}"
  local _BNAME="${BLOCK_NAME:-}" _TZ="${TZ_RESET:-}"
  if [ -f "$cfg" ]; then
    # shellcheck disable=SC1090
    set -a; . "$cfg"; set +a
  fi
  [ -n "$_DAILY" ]  && DAILY_LIMIT_MIN="$_DAILY"
  [ -n "$_SAMPLE" ] && SAMPLE_SEC="$_SAMPLE"
  [ -n "$_WINDOW" ] && WINDOW_SEC="$_WINDOW"
  [ -n "$_CONT" ]   && PIHOLE_CONTAINER="$_CONT"
  [ -n "$_HOSTS" ]  && HOSTS_PATH="$_HOSTS"
  [ -n "$_STATE" ]  && STATE_FILE="$_STATE"
  [ -n "$_BNAME" ]  && BLOCK_NAME="$_BNAME"
  [ -n "$_TZ" ]     && TZ_RESET="$_TZ"
  # Hard defaults (in case config.env is absent).
  : "${DAILY_LIMIT_MIN:=60}" "${SAMPLE_SEC:=20}" "${WINDOW_SEC:=240}"
  : "${PIHOLE_CONTAINER:=pihole}"
  : "${HOSTS_PATH:=/mnt/c/Windows/System32/drivers/etc/hosts}"
  : "${STATE_FILE:=/var/lib/safehouse/youtube-budget.json}"
  : "${BLOCK_NAME:=YouTube}" "${TZ_RESET:=local}"
  # Fixed, container-internal FTL DB path (overridable for testing only).
  : "${FTL_DB:=/etc/pihole/pihole-FTL.db}"
  # Enforcement host list = same source of truth as the manual toggle.
  local lname; lname="$(printf '%s' "$BLOCK_NAME" | tr '[:upper:]' '[:lower:]')"
  BLOCKLIST_FILE="${BLOCKLIST_FILE:-$REPO/windows/parental-blocks/$lname.txt}"
}

# --- logging -----------------------------------------------------------------
yb_log() {
  mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE"
}

# --- local date for the daily reset -----------------------------------------
yb_today() {
  if [ "${TZ_RESET:-local}" = "local" ] || [ -z "${TZ_RESET:-}" ]; then
    date '+%F'
  else
    TZ="$TZ_RESET" date '+%F'
  fi
}

# --- state (atomic JSON via python3) ----------------------------------------
# yb_state_get FIELD  -> prints the value ("" if missing; bools as true/false).
yb_state_get() {
  YB_PATH="$STATE_FILE" YB_FIELD="$1" python3 - <<'PY'
import json, os, sys
try:
    with open(os.environ["YB_PATH"]) as f:
        d = json.load(f)
except Exception:
    d = {}
v = d.get(os.environ["YB_FIELD"], "")
if isinstance(v, bool):
    v = "true" if v else "false"
sys.stdout.write(str(v))
PY
}

# yb_state_exists -> rc 0 if the state file is present and valid JSON.
yb_state_exists() {
  YB_PATH="$STATE_FILE" python3 - <<'PY'
import json, os, sys
try:
    json.load(open(os.environ["YB_PATH"]))
except Exception:
    sys.exit(1)
PY
}

# yb_state_write DATE SECONDS_USED LIMIT_MIN BLOCKED_BY_BUDGET BONUS_SEC
# Writes the whole state atomically (temp + os.replace), 0600, root dir created.
yb_state_write() {
  YB_PATH="$STATE_FILE" YB_DATE="$1" YB_SU="$2" YB_LM="$3" YB_BB="$4" YB_BS="$5" python3 - <<'PY'
import json, os, tempfile
path = os.environ["YB_PATH"]
d = {
    "date": os.environ["YB_DATE"],
    "seconds_used": int(os.environ["YB_SU"]),
    "limit_min": int(os.environ["YB_LM"]),
    "blocked_by_budget": os.environ["YB_BB"] == "true",
    "bonus_sec": int(os.environ["YB_BS"]),
}
d_dir = os.path.dirname(path) or "."
os.makedirs(d_dir, exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=d_dir, prefix=".ybstate.")
try:
    with os.fdopen(fd, "w") as f:
        json.dump(d, f, indent=2)
        f.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
except Exception:
    try: os.unlink(tmp)
    except OSError: pass
    raise
PY
}

# --- hosts enforcement (byte-preserving splice) -----------------------------
# yb_hosts_is_blocked -> rc 0 if ANY "Parental block: <BLOCK_NAME>" section
# (manual toggle OR this auto block) is present in HOSTS_PATH.
yb_hosts_is_blocked() {
  YB_HOSTS="$HOSTS_PATH" YB_NAME="$BLOCK_NAME" python3 - <<'PY'
import os, re, sys
name = re.escape(os.environ["YB_NAME"])
try:
    raw = open(os.environ["YB_HOSTS"], "r", encoding="latin-1", newline="").read()
except FileNotFoundError:
    sys.exit(1)
sys.exit(0 if re.search(r"(?m)^# === Parental block: %s\b" % name, raw) else 1)
PY
}

# yb_hosts_block -> insert the auto YouTube section if no YouTube section exists.
# Prints BLOCKED / ALREADY / NOLIST. Byte-preserving (latin-1, no newline xlate);
# LF body + CRLF header untouched. Mirrors parental-toggle.ps1's -Block algorithm.
yb_hosts_block() {
  YB_HOSTS="$HOSTS_PATH" YB_NAME="$BLOCK_NAME" YB_LIST="$BLOCKLIST_FILE" python3 - <<'PY'
import os, re, sys, tempfile
hosts = os.environ["YB_HOSTS"]; name = os.environ["YB_NAME"]; listf = os.environ["YB_LIST"]
start = "# === Parental block: %s (auto: daily budget) ===" % name
end   = "# === end %s block ===" % name
raw = open(hosts, "r", encoding="latin-1", newline="").read()
if re.search(r"(?m)^# === Parental block: %s\b" % re.escape(name), raw):
    print("ALREADY"); sys.exit(0)
hostnames = []
try:
    for line in open(listf, "r", encoding="latin-1", newline="").read().splitlines():
        h = line.split("#", 1)[0].strip()
        if h:
            hostnames.append(h)
except FileNotFoundError:
    print("NOLIST"); sys.exit(2)
if not hostnames:
    print("NOLIST"); sys.exit(2)
nl = "\n"
if raw and not raw.endswith("\n"):
    raw += nl
section = nl + start + nl
for h in hostnames:
    section += "0.0.0.0 " + h + nl
section += end + nl
new = raw + section
d_dir = os.path.dirname(hosts) or "."
try:
    fd, tmp = tempfile.mkstemp(dir=d_dir, prefix=".ybhosts.")
    with os.fdopen(fd, "w", encoding="latin-1", newline="") as f:
        f.write(new)
    os.replace(tmp, hosts)
except OSError:
    # Some mounts (drvfs) can refuse cross-name rename; fall back to direct write.
    with open(hosts, "w", encoding="latin-1", newline="") as f:
        f.write(new)
print("BLOCKED:%d" % len(hostnames))
PY
}

# yb_hosts_unblock [auto|any] -> remove the YouTube section.
#   auto (default): remove only the "(auto: daily budget)" section the daemon owns.
#   any           : remove any "Parental block: <BLOCK_NAME>" section (parent override).
# Prints UNBLOCKED / NONE. Byte-preserving; mirrors parental-toggle.ps1's -Allow.
yb_hosts_unblock() {
  YB_HOSTS="$HOSTS_PATH" YB_NAME="$BLOCK_NAME" YB_MODE="${1:-auto}" python3 - <<'PY'
import os, re, sys, tempfile
hosts = os.environ["YB_HOSTS"]; name = os.environ["YB_NAME"]; mode = os.environ["YB_MODE"]
n = re.escape(name)
if mode == "auto":
    start_pat = r"# === Parental block: %s \(auto: daily budget\) ===" % n
else:
    start_pat = r"# === Parental block: %s\b[^\r\n]*" % n
remove = r"(?ms)(?:\r?\n)?^%s.*?^# === end %s block ===[^\r\n]*(?:\r?\n)?" % (start_pat, n)
raw = open(hosts, "r", encoding="latin-1", newline="").read()
new, count = re.subn(remove, "", raw, count=1)
if count == 0:
    print("NONE"); sys.exit(0)
d_dir = os.path.dirname(hosts) or "."
try:
    fd, tmp = tempfile.mkstemp(dir=d_dir, prefix=".ybhosts.")
    with os.fdopen(fd, "w", encoding="latin-1", newline="") as f:
        f.write(new)
    os.replace(tmp, hosts)
except OSError:
    with open(hosts, "w", encoding="latin-1", newline="") as f:
        f.write(new)
print("UNBLOCKED")
PY
}

# --- DNS flush (best-effort) --------------------------------------------------
yb_flush_dns() {
  local ipc=/mnt/c/Windows/System32/ipconfig.exe
  [ -x "$ipc" ] && "$ipc" /flushdns >/dev/null 2>&1 || true
}

# --- detection (Pi-hole FTL query log) --------------------------------------
# Build the "( domain=... OR domain LIKE '%.x' ... )" clause from detect-domains.txt.
yb_build_detect_where() {
  local conds="" line dom esc
  while IFS= read -r line; do
    dom="${line%%#*}"
    dom="$(printf '%s' "$dom" | tr -d '[:space:]')"
    [ -z "$dom" ] && continue
    esc="${dom//\'/\'\'}"
    [ -n "$conds" ] && conds="$conds OR "
    conds="${conds}domain='$esc' OR domain LIKE '%.$esc'"
  done < "$DETECT_DOMAINS_FILE"
  printf '%s' "$conds"
}

# The exact detection SQL (COUNT of allowed YouTube-content queries in the window).
yb_detect_sql() {
  local where; where="$(yb_build_detect_where)"
  printf "SELECT COUNT(*) FROM queries WHERE status IN (2,3) AND timestamp >= strftime('%%s','now') - %s AND ( %s );" \
    "$WINDOW_SEC" "$where"
}

# yb_detect -> echoes 1 (active) or 0 (idle) on success; rc!=0 if Pi-hole/Docker
# is unreachable or the query fails (caller should skip the sample, not accumulate).
yb_detect() {
  local sql count
  sql="$(yb_detect_sql)"
  count="$(docker exec "$PIHOLE_CONTAINER" pihole-FTL sqlite3 "$FTL_DB" "$sql" 2>/dev/null)" || return 1
  count="$(printf '%s' "$count" | tr -d '[:space:]')"
  [ -z "$count" ] && return 1
  case "$count" in *[!0-9]*) return 1;; esac
  if [ "$count" -gt 0 ]; then echo 1; else echo 0; fi
}
