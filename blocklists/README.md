# Blocklists — the source of truth

Two plain-text files drive what Pi-hole blocks. Edit them, then run
`../scripts/load-blocklists.sh` to apply (or re-run the bootstrap / Ansible).

## `adlists.txt`
One blocklist URL per line (loaded into Pi-hole's gravity `adlist` table). Current set:
StevenBlack, HaGeZi Multi PRO, OISD Big, HaGeZi Pro++, GoodbyeAds, AdAway — chosen for strong
**mobile / in-app / game-ad** coverage. Lines starting with `#` are ignored.

## `regex-denylist.txt`
One Pi-hole **regex** per line (loaded as `domainlist` type 3 = regex deny). These target named
mobile-game ad networks & trackers by whole domain, e.g. `(^|\.)applovin\.com$`. They catch ad
subdomains the public lists miss (this is what we discovered with `adhunt`). Lines starting with
`#` are ignored.

## Adding more
1. Hunt the real ad server: `bash ../tools/adhunt.sh` (trigger ads in-game during capture).
2. Add the offending domain as a regex line here, e.g. `(^|\.)somenetwork\.com$`.
3. `../scripts/load-blocklists.sh` to apply.
4. Restart the game (clears the SDK's cached ad IPs) and re-verify.

## Backing up live changes
If you add rules via the Pi-hole dashboard, pull them back into these files with
`../scripts/export-config.sh`, then commit. That keeps the repo authoritative.
