# logs/

Runtime traffic logs written by `windows/safehouse-adblock.ps1`.

- `traffic.csv` - one row per observed connection: timestamp, IP, ASN, country, owner,
  reverse-DNS, and the AD/ok verdict. This is the "net" that records what the crosvm VM talks
  to so you can analyze trends and refresh the firewall rules over time.

These files are **gitignored on purpose**. They record every server this machine connects to,
which is local and personal, so they stay out of the public repo. Only the rulesets
(`blocklists/ad-ip-ranges.txt`, `blocklists/ad-watchlist.txt`) are committed.

## Analyze
```powershell
# which owners are contacted most
Import-Csv traffic.csv | Group-Object owner | Sort-Object Count -Descending | Select Count, Name

# only the ad verdicts, newest first
Import-Csv traffic.csv | Where-Object verdict -eq 'AD' | Sort-Object ts -Descending

# distinct ad IPs seen (candidates for the ruleset)
Import-Csv traffic.csv | Where-Object verdict -eq 'AD' | Select-Object -Expand ip -Unique
```
