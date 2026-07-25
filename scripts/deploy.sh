#!/usr/bin/env bash
# SafeHouse: one command, any machine.
#
#   ./scripts/deploy.sh                 # Pi-hole only
#   ./scripts/deploy.sh --with-adguard  # AdGuard Home chained in front of Pi-hole
#
# Installs ansible if missing, then runs the playbook, which installs docker if missing
# and brings the whole stack up. Safe to re-run: everything downstream is idempotent.
#
# The Windows-side steps (adapter DNS, NRPT, firewall, YouTube budget) need UAC and are
# printed at the end for you to run from an elevated PowerShell.
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
extra_vars=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-adguard) extra_vars+=(-e safehouse_enable_adguard=true); shift ;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) extra_vars+=("$1"); shift ;;
  esac
done

if ! command -v ansible-playbook >/dev/null 2>&1; then
  echo "[*] ansible not found - installing (sudo required once)..."
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -qq && sudo apt-get install -y ansible
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y ansible
  elif command -v pacman >/dev/null 2>&1; then
    sudo pacman -Sy --noconfirm ansible
  else
    echo "!! No supported package manager found. Install ansible manually, then re-run." >&2
    exit 1
  fi
fi

cd "$repo_dir/ansible"
exec ansible-playbook -i inventory.ini playbook.yml "${extra_vars[@]}"
