# SafeHouse

Infrastructure-as-Code for a network-level ad-blocking stack that kills ads inside
Google Play Games on PC (and everything else on this machine), using **Pi-hole** for DNS and the
**Windows Firewall** for the leaks DNS cannot catch.

This repo is the source of truth and backup for the whole setup. Rebuild it anywhere with one
command (Ansible or a plain bash bootstrap).

## Two layers of defense

Google Play Games runs Android games inside a `crosvm` VM. Ads are blocked at two layers, neither
of which touches the inside of the VM:

1. **DNS (Pi-hole).** Cheap and catches most ads. The Windows adapter points at Pi-hole and a
   global NRPT rule forces the Windows DNS Client through it even when the router advertises an
   IPv6 resolver; Chrome/Edge secure DNS is disabled by policy. The VM inherits the adapter DNS,
   and ad domains resolve to `0.0.0.0`. About 1.3M blocked domains plus regex rules for named
   mobile-game ad networks. It also carries a **parental content layer**: a regex
   denylist (`blocklists/parental-denylist.txt`, e.g. the YouTube family) plus **adult/NSFW**
   coverage pulled in via a referenced blocklist URL in `blocklists/adlists.txt` (a link, never
   domain literals in this public repo). The blocklists **auto-update weekly** (see below).
2. **Connection layer (Windows Firewall).** Some ad SDKs never ask Pi-hole: they resolve over
   their own encrypted DNS (DoH), over QUIC, or reuse a cached IP, so the DNS layer never sees
   them. crosvm re-originates every guest connection from the host `crosvm.exe` process, so a host
   firewall rule blocks those ad servers and the VM cannot bypass it. See
   [docs/FIREWALL.md](docs/FIREWALL.md).

Optionally, **AdGuard Home** chains in front of Pi-hole on `:53` (Pi-hole moves to `:5353`) to add
the two things Pi-hole lacks: **per-client** policy and **time-of-day schedules**, plus a
maintained one-click YouTube ruleset. The Windows adapter DNS and NRPT rule stay pointed at the
same WSL IP, so nothing on the Windows side changes. See [docs/ADGUARD.md](docs/ADGUARD.md).

> Any filtering resolver only filters the queries that reach it. Two bypasses on this machine
> made the DNS layer decorative and are both now closed: **Avast Real Site (Secure DNS)**, which
> resolved through its own DoH (Avast has since been removed), and **Opera GX**, whose built-in
> VPN tunnels all browser traffic past every filter here — pinned off by
> [`windows/opera-policy.ps1`](windows/opera-policy.ps1). See
> [docs/ADGUARD.md](docs/ADGUARD.md#hard-prerequisite-close-the-doh-bypasses).

> Key gotcha baked into this design: the crosvm VM reads host DNS only at launch and otherwise
> uses its own encrypted DNS. After DNS is (re)pointed or firewall rules change, **Google Play
> Games must be restarted** for the VM to pick it up. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## The deterministic ad blocker

[`windows/safehouse-adblock.ps1`](windows/safehouse-adblock.ps1) is one self-elevating script that
does the whole loop and is safe to re-run any time:

1. Captures the VM's current outbound IPs from the host.
2. Classifies each by network owner (ASN) and reverse DNS against `blocklists/ad-watchlist.txt`,
   a list of stable ad-network identities that keep working when the servers rotate IPs.
3. Logs every observation to `logs/traffic.csv`.
4. Ingests the confirmed ad IPs into `blocklists/ad-ip-ranges.txt`.
5. Rebuilds the `SafeHouse-AdBlock` firewall group from that ruleset (TCP and UDP, reboot-safe).

```powershell
powershell -ExecutionPolicy Bypass -File C:\SafeHouse\windows\safehouse-adblock.ps1
```

## Quick start

### One command, any machine
```bash
cd SafeHouse
./scripts/deploy.sh                 # Pi-hole only
./scripts/deploy.sh --with-adguard  # AdGuard Home chained in front of Pi-hole
```

Installs Ansible if missing, which installs Docker if missing, brings the stack up,
auto-detects the WSL IP, generates a random Pi-hole admin password on first run
(`~/pihole/.admin-password`), loads all blocklists, rebuilds gravity, installs the weekly
auto-update and stages the Windows scripts to `C:\SafeHouse`. Safe to re-run.

### Option B: plain bash (no Ansible)
```bash
cd SafeHouse
./scripts/bootstrap.sh
```

Then finish the admin steps, which need UAC and cannot be automated from WSL (enforce
DNS/NRPT, arm the logon task, arm the firewall): see [windows/README.md](windows/README.md).

## Layout

```
SafeHouse/
  pihole/         docker-compose.yml (parametrised) + .env.example
  blocklists/     adlists.txt (incl. NSFW URL), regex-denylist.txt,
                  parental-denylist.txt (parental DNS layer)
                  ad-ip-ranges.txt, ad-watchlist.txt (firewall layer)
  tools/          adhunt.sh + parse_cap.py (silent SNI ad-server hunter)
  windows/        safehouse-adblock.ps1 (firewall), set-dns.ps1 + mktask.ps1 (DNS persistence),
                  parental-toggle.ps1 + parental-blocks/ (on-demand hosts-layer blocks),
                  youtube-budget/ (Windows-native daily YouTube watch-time budget + auto-block)
  adguard/        docker-compose.yml + .env.example (optional, chained in front of Pi-hole)
  automation/     safehouse-autoupdate.{service,timer,cron} unit templates
  logs/           traffic.csv, auto-update.log (gitignored runtime logs)
  scripts/        deploy.sh (one-command install), bootstrap.sh, load-blocklists.sh,
                  export-config.sh, auto-update.sh, install-autoupdate.sh
  ansible/        playbook.yml + roles (prereqs, pihole, adguard, tools, automation,
                  windows_persistence)
  python/         Python port of the YouTube budget watcher (in progress, see
                  docs/PYTHON-PORT.md) - not yet deployed
  docs/           ARCHITECTURE.md, RUNBOOK.md, FIREWALL.md, ADGUARD.md, PYTHON-PORT.md
```

## Auto-update

The blocklists refresh on a **weekly schedule** (default Sunday 04:00 local). `scripts/auto-update.sh`
re-applies the repo lists and rebuilds gravity so every adlist (ads + NSFW) re-downloads;
`scripts/install-autoupdate.sh` installs it as a **systemd timer** (cron fallback) and the Ansible
`automation` role wires it in during provisioning. Run it by hand any time with
`./scripts/auto-update.sh` (log in `logs/auto-update.log`). See
[docs/RUNBOOK.md](docs/RUNBOOK.md) for verify / change-cadence / uninstall.

## Day-to-day

- Ads came back? Re-run the blocker while ads show: `C:\SafeHouse\windows\safehouse-adblock.ps1`
- Hunt a CDN-fronted ad by hostname: `bash tools/adhunt.sh` (trigger ads during the capture)
- Add what you find to `blocklists/` and re-run the blocker or `./scripts/load-blocklists.sh`
- Back up live changes into the repo: `./scripts/export-config.sh` then commit
- Limit YouTube watch time: arm the Windows-native budget with
  `C:\SafeHouse\windows\youtube-budget\install-task.ps1` (elevated) and drive it with
  `…\youtube-budget-ctl.ps1 status` (measures via the Windows DNS cache, auto-blocks at the
  daily limit, browser/app-agnostic, no WSL dependency — see
  [windows/youtube-budget/README.md](windows/youtube-budget/README.md))
- Dashboard: http://localhost:8053/admin

See [docs/RUNBOOK.md](docs/RUNBOOK.md) for operations and troubleshooting.

## License

MIT. See [LICENSE](LICENSE).
