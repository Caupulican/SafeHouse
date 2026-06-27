# Blocklists: the source of truth

These plain-text files drive both layers of blocking. The first two feed **Pi-hole** (DNS layer);
the last two feed the **Windows Firewall** (connection layer, for ads that bypass DNS).

## `adlists.txt`
One blocklist URL per line (loaded into Pi-hole's gravity `adlist` table). Current set:
StevenBlack, HaGeZi Multi PRO, OISD Big, HaGeZi Pro++, GoodbyeAds, AdAway, chosen for strong
**mobile / in-app / game-ad** coverage. It also references the **HaGeZi NSFW** list, which is the
**parental adult/NSFW coverage** for the whole network — see the parental layer below. Lines
starting with `#` are ignored.

## `regex-denylist.txt`
One Pi-hole **regex** per line (loaded as `domainlist` type 3 = regex deny, comment `SafeHouse`).
These target named mobile-game ad networks & trackers by whole domain, e.g. `(^|\.)applovin\.com$`.
They catch ad subdomains the public lists miss (this is what we discovered with `adhunt`). Lines
starting with `#` are ignored.

## `parental-denylist.txt` (parental content layer)
One Pi-hole **regex** per line, loaded as `domainlist` type 3 = regex deny with the **distinct**
comment `SafeHouse-Parental` so it stays separate from the ad regex above. This is the **parental
content layer**: ordinary product/service domains blocked by name, e.g. the **YouTube** family
(`(^|\.)youtube\.com$`, `googlevideo\.com`, `ytimg\.com`, …). It is **public-repo safe** — names
only. Adult / NSFW content is **deliberately not listed here**; it is covered network-wide by the
HaGeZi NSFW **URL** in `adlists.txt` (a link, never the domains). Extend it with more domain
families (e.g. social media) as commented in the file, then `../scripts/load-blocklists.sh`.

## `ad-watchlist.txt` (firewall layer)
Stable ad-network identities for the connection-layer blocker (`../windows/safehouse-adblock.ps1`).
Lines are `asn:<number>`, `owner:<substring>`, or `host:<substring>`. Because it matches on owner
and hostname rather than raw IP, it keeps catching ad servers after they rotate addresses. Shared
CDNs (Amazon, Fastly, Akamai, Google, Cloudflare) are intentionally left out.

## `ad-ip-ranges.txt` (firewall layer)
The IP/CIDR ruleset the firewall actually blocks. The top section is hand-curated; the `AUTO`
section below the marker is maintained by `safehouse-adblock.ps1` as it classifies live traffic.
Re-run that script (elevated) to refresh it. See [../docs/FIREWALL.md](../docs/FIREWALL.md).

## Adding more (DNS layer)
1. Hunt the real ad server: `bash ../tools/adhunt.sh` (trigger ads in-game during capture).
2. Add the offending domain as a regex line here, e.g. `(^|\.)somenetwork\.com$`.
3. `../scripts/load-blocklists.sh` to apply.
4. Restart the game (clears the SDK's cached ad IPs) and re-verify.

## Backing up live changes
If you add rules via the Pi-hole dashboard, pull them back into these files with
`../scripts/export-config.sh`, then commit. That keeps the repo authoritative. The export splits
type-3 regex by comment: `SafeHouse-Parental` rules round-trip to `parental-denylist.txt` (header
preserved), everything else to `regex-denylist.txt`.
