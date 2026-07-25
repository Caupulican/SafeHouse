# Blocklists

Everything the DNS and firewall layers load. Plain text, one entry per line, `#` for
comments.

| File | Layer | What it is |
|---|---|---|
| `adlists.txt` | Pi-hole (DNS) | Adlist URLs. Ads, tracking, malware |
| `regex-denylist.txt` | Pi-hole (DNS) | Regex patterns for named ad networks |
| `parental-denylist.txt` | Pi-hole (DNS) | Regex for ordinary services to restrict, by name |
| `ad-ip-ranges.txt` | Windows Firewall | IPs/CIDRs to block outbound. For ads that never do a DNS lookup |
| `ad-watchlist.txt` | Windows Firewall | Ad-network *identities* (ASN, owner, rDNS) used to classify new IPs |

Apply the DNS lists with `../scripts/load-blocklists.sh`; the firewall lists with
`../windows/safehouse-adblock.ps1`.

## No adult-content list ships here

Not the domains, and not a URL pointing at them. Three reasons:

1. It is not this project's place to curate or redistribute that material.
2. The appropriate list differs by household and jurisdiction.
3. A stale list committed here would give a false sense of coverage — the worst possible
   failure for a control like this.

`adlists.txt` documents where to add your own. AdGuard Home's parental category filter is
enabled by default and may be sufficient on its own; check it before stacking an
overlapping list, because overlapping lists make false positives much harder to diagnose.

## Why identities, not addresses

`ad-watchlist.txt` lists ad networks by **ASN, owner name and reverse-DNS pattern**, not
by IP. Ad servers rotate addresses constantly, so a hand-written IP list goes stale within
days. `safehouse-adblock.ps1` looks up each live connection's owner and matches it against
these identities, so a network stays blocked when it moves.

`ad-ip-ranges.txt` is the output of that process: hand-curated entries at the top, and an
`AUTO` section below the marker maintained by the script. Do not hand-edit below the
marker.

## Adding a network you have found

1. Note the ad server's owner or ASN (the traffic log records both).
2. Add an identity line to `ad-watchlist.txt` (`owner:`, `asn:` or `host:`).
3. Re-run `safehouse-adblock.ps1` while the ad is on screen, so it captures and classifies
   the live connection.
