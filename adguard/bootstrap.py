#!/usr/bin/env python3
"""Configure a fresh AdGuard Home end to end, with no wizard.

    python3 adguard/bootstrap.py --upstream 127.0.0.1:5353

AdGuard Home's first run puts up an interactive setup wizard, which is the one thing
standing between this stack and an unattended install. It also exposes that wizard as an
API, so this drives it directly: completes setup, points it at Pi-hole, turns on the
parental settings, and applies the filter set from filters.json.

Idempotent. On an already-configured instance the wizard step is skipped and the rest is
reconciled, so it is safe to re-run -- which matters, because the alternative is an
operator wondering whether it is safe and therefore never running it.

Stdlib only.
"""

from __future__ import annotations

import argparse
import base64
import secrets
import string
import subprocess
import sys
import time
from pathlib import Path

from adguard_api import api

HERE = Path(__file__).resolve().parent


def wait_for(base, attempts=60):
    for _ in range(attempts):
        status, _ = api(base, None, "/control/status", timeout=5)
        if status is not None:
            return True
        time.sleep(2)
    return False


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default="http://127.0.0.1:3000")
    p.add_argument("--username", default="admin")
    p.add_argument("--password-file", type=Path,
                   default=Path.home() / "adguard" / ".admin-password",
                   help="generated on first run if absent")
    p.add_argument("--upstream", required=True,
                   help="Pi-hole address, e.g. 10.0.0.5:5353")
    p.add_argument("--dns-port", type=int, default=53,
                   help="container-side DNS port (host mapping is compose's job)")
    p.add_argument("--web-port", type=int, default=3000)
    p.add_argument("--skip-filters", action="store_true")
    args = p.parse_args(argv)

    base = args.base_url.rstrip("/")
    if not wait_for(base):
        print(f"!! AdGuard Home never became reachable at {base}", file=sys.stderr)
        return 1

    # A generated password beats a known default: this dashboard can switch off every
    # filter in the stack, so shipping one password to every machine is not acceptable.
    if args.password_file.exists():
        password = args.password_file.read_text().strip()
    else:
        alphabet = string.ascii_letters + string.digits
        password = "".join(secrets.choice(alphabet) for _ in range(24))
        args.password_file.parent.mkdir(parents=True, exist_ok=True)
        args.password_file.write_text(password + "\n")
        args.password_file.chmod(0o600)
        print(f"[*] generated admin password -> {args.password_file}")

    auth = base64.b64encode(f"{args.username}:{password}".encode()).decode()

    # --- 1. wizard (only on a virgin instance) -------------------------------
    status, _ = api(base, auth, "/control/status")
    configured = status == 200
    if not configured:
        print("[*] completing first-run setup...")
        st, body = api(base, None, "/control/install/configure", {
            "web": {"ip": "0.0.0.0", "port": args.web_port},
            "dns": {"ip": "0.0.0.0", "port": args.dns_port},
            "username": args.username,
            "password": password,
        })
        if st != 200:
            print(f"!! setup failed ({st}): {body[:200]}", file=sys.stderr)
            return 1
        time.sleep(3)
    else:
        print("[*] already configured - skipping the wizard")

    # --- 2. upstream: everything not blocked here goes to Pi-hole ------------
    print(f"[*] upstream -> {args.upstream}")
    st, body = api(base, auth, "/control/dns_config", {
        "upstream_dns": [args.upstream],
        "bootstrap_dns": ["1.1.1.1", "1.0.0.1"],
        # Empty upstream_mode = use the first working upstream rather than
        # load-balancing, so every query deterministically traverses Pi-hole and its
        # blocklists instead of sometimes going straight out.
        "upstream_mode": "",
        "protection_enabled": True,
    })
    if st != 200:
        print(f"!! could not set upstream ({st}): {body[:200]}", file=sys.stderr)

    # --- 3. parental settings ------------------------------------------------
    print("[*] parental control + SafeSearch + Safe Browsing")
    api(base, auth, "/control/parental/enable", {})
    api(base, auth, "/control/safesearch/settings", {
        "enabled": True, "bing": True, "duckduckgo": True, "ecosia": True,
        "google": True, "pixabay": True, "yandex": True, "youtube": True,
    }, method="PUT")
    # Malware/phishing, as a category lookup rather than a downloaded list. Enabled
    # 2026-08-19; it was off on the live instance and nothing here would have turned it
    # back on, so a rebuild was silently dropping it. Deliberately chosen INSTEAD of
    # HaGeZi's Threat Intelligence Feeds, which covers similar ground in two million
    # rules - see the reason parked next to that list in filters.json.
    api(base, auth, "/control/safebrowsing/enable", {})

    # --- 4. filter lists -----------------------------------------------------
    if args.skip_filters:
        print("[*] --skip-filters")
    else:
        print("[*] applying filter lists...")
        rc = subprocess.call([
            sys.executable, str(HERE / "apply-filters.py"),
            "--base-url", base, "--username", args.username,
            "--creds", str(args.password_file),
        ])
        if rc != 0:
            print("!! filter application reported a problem", file=sys.stderr)

    print()
    print(f"AdGuard Home ready: {base}")
    print(f"  user     : {args.username}")
    print(f"  password : {args.password_file}")
    print(f"  upstream : {args.upstream} (Pi-hole)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
