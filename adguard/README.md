# AdGuard Home

The front half of the DNS chain. AdGuard Home owns `:53` and forwards whatever it does
not block to Pi-hole on `:5353`.

## Why AdGuard *and* Pi-hole

They are strong at different things, and chaining costs less than choosing:

| | Pi-hole | AdGuard Home |
|---|---|---|
| Bulk domain blocklists | excellent | good |
| Per-client policy | weak | native |
| Time-of-day schedules | no | yes |
| Forced SafeSearch | no | yes |
| Blocked-services presets | no | yes, maintained |
| REST API for enforcement | limited | full |

That last row matters most here. The YouTube budget can toggle a *wildcard* rule at the
resolver — `*.googlevideo.com` — instead of chasing rotating IP addresses through a
32-filter cap. A hosts file cannot express that wildcard at all.

## Files

| File | Purpose |
|---|---|
| `docker-compose.yml` | The container. Binds DNS to the WSL IP to dodge the WSL DNS-tunnel listener on `10.255.255.254:53` |
| `.env.example` | Copy to `.env`. Bind IP, ports, timezone |
| `filters.json` | **The filter set, with a written reason for each list.** Source of truth |
| `bootstrap.py` | Configures a fresh instance end to end: wizard, upstream, parental settings, filters |
| `apply-filters.py` | Reconciles a running instance to `filters.json` |

## Setup

Handled automatically by `Install-SafeHouse.ps1` or `scripts/deploy.sh --with-adguard`.
By hand:

```bash
mkdir -p ~/adguard && cp adguard/docker-compose.yml ~/adguard/
cp adguard/.env.example ~/adguard/.env
cd ~/adguard && docker compose up -d

python3 adguard/bootstrap.py --upstream 127.0.0.1:5353
```

`bootstrap.py` drives the first-run wizard through its API, which is the one thing that
would otherwise make this stack impossible to install unattended. It generates an admin
password into `~/adguard/.admin-password` rather than shipping a default — this dashboard
can switch off every filter in the stack.

Both scripts are idempotent. Re-running is safe, which matters: a script an operator is
unsure about re-running is a script that never gets re-run.

## The filter set

`filters.json` is the source of truth and explains each choice. The short version:

- **AWAvenue Ads Rule** — the important one. Targets advertising SDKs *inside Android
  apps*, which is exactly what the games embed. Generic web lists miss these.
- **AdAway** — Android hosts list. Note it ships with AdGuard *added but disabled*, which
  is easy to miss and was the state found on a real install.
- **HaGeZi Pro** — broad ads/tracking. "Pro", deliberately not Pro++ or Ultimate: those
  over-block, and a game that will not start is a worse outcome than an ad.
- **Encrypted DNS/VPN/Proxy Bypass** — blocks known DoH and VPN endpoints, closing the
  hole that makes every other DNS control decorative.
- **Threat Intelligence Feeds** — malware and phishing. In-game ads are a common delivery
  route for both.

Verify with the query log, which names the rule that matched, or:

```bash
dig @127.0.0.1 a.applovin.com +short     # -> 0.0.0.0
dig @127.0.0.1 play.googleapis.com +short # -> a real address (Play Games must still work)
```

## Verifying the chain

```bash
dig @127.0.0.1 doubleclick.net +short    # -> 0.0.0.0, blocked somewhere in the chain
docker exec pihole tail -20 /var/log/pihole/pihole.log   # the query should appear here
```

If it does not appear in Pi-hole's log, AdGuard is answering alone and the upstream is
not configured.
