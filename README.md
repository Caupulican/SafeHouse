# 🏠 SafeHouse

Infrastructure-as-Code for a **network-level ad-blocking stack** that kills ads inside
**Google Play Games on PC** (and everything else on this machine) using **Pi-hole** in
Docker on WSL2 — plus a silent **TLS-SNI ad-server hunter** and **reboot-proof** auto-setup.

This repo is the **source of truth + backup** for the whole setup. Rebuild it anywhere with
one command (Ansible *or* a plain bash bootstrap).

---

## What it does

- Runs **Pi-hole** in Docker inside WSL2, bound to the (dynamic) WSL eth0 IP.
- Points the Windows host DNS at Pi-hole, so the **crosvm Android VM** (Google Play Games)
  resolves through it and ad domains die at the network layer — **nothing installed in the VM**.
- Ships **~1.3M blocked domains** (StevenBlack, HaGeZi Pro/Pro++, OISD Big, GoodbyeAds, AdAway)
  + **70 regex rules** for named mobile-game ad networks (AppLovin, Vungle, ironSource,
  Mintegral, Pangle, Chartboost, InMobi, Moloco, Smaato, Tapjoy, …).
- Includes `adhunt` — a **silent packet-capture tool** (pktmon → pcapng → Python SNI parser)
  that reveals the *real* ad servers a game contacts (even IP-direct / DoH-resolved) so you can
  block exactly what leaks.
- Includes a **logon task** that re-points DNS automatically after every reboot (WSL IPs change).

> ⚠️ **Key gotcha baked into this design:** the crosvm VM reads host DNS **only at launch** and
> otherwise uses its own encrypted DNS. So after DNS is (re)pointed, **Google Play Games must be
> restarted** for the VM to pick up Pi-hole. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Quick start

### Option A — Ansible (recommended)
```bash
cd SafeHouse/ansible
# one-time: install ansible if missing
sudo apt-get update && sudo apt-get install -y ansible
ansible-playbook -i inventory.ini playbook.yml
```

### Option B — plain bash (no Ansible)
```bash
cd SafeHouse
./scripts/bootstrap.sh
```

Both: deploy Pi-hole, auto-detect the WSL IP, load all blocklists, rebuild gravity, and stage the
Windows scripts. Then finish the **two admin steps** (point DNS + arm the logon task):
see [windows/README.md](windows/README.md).

---

## Layout

```
SafeHouse/
├── pihole/              docker-compose.yml (parametrised) + .env.example
├── blocklists/          adlists.txt + regex-denylist.txt  (THE source of truth)
├── tools/               adhunt.sh + parse_cap.py  (silent SNI ad-server hunter)
├── windows/             set-dns.ps1 + mktask.ps1  (DNS persistence / logon task)
├── scripts/             bootstrap.sh · load-blocklists.sh · export-config.sh
├── ansible/             playbook.yml + roles (prereqs, pihole, tools, windows_persistence)
└── docs/                ARCHITECTURE.md · RUNBOOK.md
```

## Day-to-day

- **Hunt a leaking ad** (trigger ads in-game during the capture): `bash tools/adhunt.sh`
- **Add what you find** to `blocklists/regex-denylist.txt`, then `./scripts/load-blocklists.sh`
- **Back up live changes** into the repo: `./scripts/export-config.sh` then commit.
- **Dashboard:** http://localhost:8053/admin

See [docs/RUNBOOK.md](docs/RUNBOOK.md) for operations & troubleshooting.
