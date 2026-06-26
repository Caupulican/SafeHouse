# SafeHouse

Infrastructure-as-Code for a network-level ad-blocking stack that kills ads inside
Google Play Games on PC (and everything else on this machine), using **Pi-hole** for DNS and the
**Windows Firewall** for the leaks DNS cannot catch.

This repo is the source of truth and backup for the whole setup. Rebuild it anywhere with one
command (Ansible or a plain bash bootstrap).

## Two layers of defense

Google Play Games runs Android games inside a `crosvm` VM. Ads are blocked at two layers, neither
of which touches the inside of the VM:

1. **DNS (Pi-hole).** Cheap and catches most ads. The Windows host DNS points at Pi-hole, the VM
   inherits it, and ad domains resolve to `0.0.0.0`. About 1.3M blocked domains plus regex rules
   for named mobile-game ad networks.
2. **Connection layer (Windows Firewall).** Some ad SDKs never ask Pi-hole: they resolve over
   their own encrypted DNS (DoH), over QUIC, or reuse a cached IP, so the DNS layer never sees
   them. crosvm re-originates every guest connection from the host `crosvm.exe` process, so a host
   firewall rule blocks those ad servers and the VM cannot bypass it. See
   [docs/FIREWALL.md](docs/FIREWALL.md).

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

### Option A: Ansible (recommended)
```bash
cd SafeHouse/ansible
sudo apt-get update && sudo apt-get install -y ansible   # one-time, if missing
ansible-playbook -i inventory.ini playbook.yml
```

### Option B: plain bash (no Ansible)
```bash
cd SafeHouse
./scripts/bootstrap.sh
```

Both deploy Pi-hole, auto-detect the WSL IP, load all blocklists, rebuild gravity, and stage the
Windows scripts to `C:\SafeHouse`. Then finish the admin steps (point DNS, arm the logon task, arm
the firewall): see [windows/README.md](windows/README.md).

## Layout

```
SafeHouse/
  pihole/         docker-compose.yml (parametrised) + .env.example
  blocklists/     adlists.txt, regex-denylist.txt (DNS layer)
                  ad-ip-ranges.txt, ad-watchlist.txt (firewall layer)
  tools/          adhunt.sh + parse_cap.py (silent SNI ad-server hunter)
  windows/        safehouse-adblock.ps1 (firewall), set-dns.ps1 + mktask.ps1 (DNS persistence)
  logs/           traffic.csv (gitignored runtime log)
  scripts/        bootstrap.sh, load-blocklists.sh, export-config.sh
  ansible/        playbook.yml + roles (prereqs, pihole, tools, windows_persistence)
  docs/           ARCHITECTURE.md, RUNBOOK.md, FIREWALL.md
```

## Day-to-day

- Ads came back? Re-run the blocker while ads show: `C:\SafeHouse\windows\safehouse-adblock.ps1`
- Hunt a CDN-fronted ad by hostname: `bash tools/adhunt.sh` (trigger ads during the capture)
- Add what you find to `blocklists/` and re-run the blocker or `./scripts/load-blocklists.sh`
- Back up live changes into the repo: `./scripts/export-config.sh` then commit
- Dashboard: http://localhost:8053/admin

See [docs/RUNBOOK.md](docs/RUNBOOK.md) for operations and troubleshooting.

## License

MIT. See [LICENSE](LICENSE).
