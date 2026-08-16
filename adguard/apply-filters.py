#!/usr/bin/env python3
"""Reconcile a running AdGuard Home to the filter set in adguard/filters.json and the
user rules in adguard/user-rules.txt.

    python3 adguard/apply-filters.py                       # apply
    python3 adguard/apply-filters.py --dry-run             # show what would change
    python3 adguard/apply-filters.py --creds ~/adguard/.admin-password

Idempotent: adds what is missing, enables what is present but disabled, and leaves
everything else alone. It never REMOVES a list or a rule, because a parent may well have
added one by hand and silently deleting it would be worse than leaving an extra.

Why this exists rather than "click it in the UI": the whole point of this repo is that
the machine can be rebuilt from it. A filter set that lives only in the running container
is lost the moment the volume is recreated -- and its absence is invisible, because ads
returning looks exactly like ads that were never blocked.

Stdlib only.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path

from adguard_api import api

HERE = Path(__file__).resolve().parent
DEFAULT_SPEC = HERE / "filters.json"
DEFAULT_RULES = HERE / "user-rules.txt"
DEFAULT_CREDS = Path.home() / "adguard" / ".admin-password"


def wanted_rules(spec_path: Path) -> list[str]:
    """The payload lines of user-rules.txt: everything after the first blank line.

    The header above the blank line documents the file itself and must not end up in
    the running instance; the payload keeps its comment lines so the rules stay
    self-explaining in the AdGuard UI.
    """
    text = spec_path.read_text()
    _, _, payload = text.partition("\n\n")
    return [line for line in payload.splitlines() if line.strip()]


def reconcile_rules(base: str, auth: str, spec_path: Path, dry_run: bool) -> None:
    """Ensure every rule in user-rules.txt is present in AdGuard's user rules.

    Merge, never replace: `set_rules` overwrites the whole list, so the new list is
    the live one plus whatever the spec adds. Hand-added rules survive. A missing
    comment line alone does not trigger a write -- only absent RULES do, and then the
    spec's not-yet-present lines (comments included) are appended together.
    """
    status, body = api(base, auth, "/control/filtering/status")
    if status != 200:
        print(f"  !! cannot read user rules ({status}): {body[:160]}", file=sys.stderr)
        return
    live = json.loads(body).get("user_rules") or []
    have = set(live)
    spec = wanted_rules(spec_path)

    missing_rules = [r for r in spec if not r.startswith("!") and r not in have]
    if not missing_rules:
        print("user rules already reconciled - every wanted rule is present.")
        return

    for r in missing_rules:
        print(f"{'[dry-run] would add rule' if dry_run else 'adding rule'} {r}")
    if dry_run:
        return
    merged = live + [line for line in spec if line not in have]
    st, resp = api(base, auth, "/control/filtering/set_rules", {"rules": merged})
    if st != 200:
        print(f"  !! failed ({st}): {resp[:160]}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default="http://127.0.0.1:3000")
    p.add_argument("--username", default="admin")
    p.add_argument("--creds", type=Path, default=DEFAULT_CREDS,
                   help="file containing the admin password")
    p.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    p.add_argument("--rules", type=Path, default=DEFAULT_RULES,
                   help="user rules the instance must contain (default: user-rules.txt)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    if not args.creds.exists():
        print(f"!! password file not found: {args.creds}", file=sys.stderr)
        return 1
    password = args.creds.read_text().strip()
    auth = base64.b64encode(f"{args.username}:{password}".encode()).decode()
    base = args.base_url.rstrip("/")

    spec = json.loads(args.spec.read_text())
    wanted = spec["filters"]

    status, body = api(base, auth, "/control/filtering/status")
    if status != 200:
        print(f"!! cannot reach AdGuard Home at {base}: {status} {body[:200]}", file=sys.stderr)
        return 1
    current = {f.get("url"): f for f in json.loads(body).get("filters", [])}

    to_add = [f for f in wanted if f["url"] not in current]
    to_enable = [f for f in wanted
                 if f["url"] in current and not current[f["url"]].get("enabled")]

    if not to_add and not to_enable:
        print("Already reconciled - every wanted list is present and enabled.")
    for f in to_add:
        print(f"{'[dry-run] would add   ' if args.dry_run else 'adding   '} {f['name']}")
        if args.dry_run:
            continue
        st, resp = api(base, auth, "/control/filtering/add_url",
                       {"name": f["name"], "url": f["url"], "whitelist": False})
        if st != 200:
            print(f"  !! failed ({st}): {resp[:160]}", file=sys.stderr)
        time.sleep(1)

    for f in to_enable:
        print(f"{'[dry-run] would enable' if args.dry_run else 'enabling '} {f['name']}")
        if args.dry_run:
            continue
        st, resp = api(base, auth, "/control/filtering/set_url",
                       {"url": f["url"],
                        "data": {"name": f["name"], "url": f["url"], "enabled": True}})
        if st != 200:
            print(f"  !! failed ({st}): {resp[:160]}", file=sys.stderr)

    if not args.dry_run and (to_add or to_enable):
        print("refreshing lists...")
        api(base, auth, "/control/filtering/refresh", {"whitelist": False}, timeout=300)

    if args.rules.exists():
        reconcile_rules(base, auth, args.rules, args.dry_run)
    else:
        print(f"!! user rules spec not found, skipping: {args.rules}", file=sys.stderr)

    status, body = api(base, auth, "/control/filtering/status")
    if status == 200:
        total = 0
        print("\nactive filter lists:")
        for f in json.loads(body).get("filters", []):
            count = f.get("rules_count") or 0
            if f.get("enabled"):
                total += count
            print(f"  [{'x' if f.get('enabled') else ' '}] {f.get('name'):<52} {count:>9,}")
        print(f"\n  total active rules: {total:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
