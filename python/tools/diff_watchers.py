"""Compare the PowerShell watcher against the Python one running in shadow mode.

This is the gate for Phase 4 of docs/PYTHON-PORT.md. Cutover happens when the two agree,
and "agree" needs to mean something measurable rather than two logs that looked similar
when someone scrolled them.

    C:\\SafeHouse\\python\\python.exe tools\\diff_watchers.py

Pairs samples by timestamp within a tolerance (the two watchers tick on independent
clocks and drift apart) and reports the disagreement rate on the two things that matter:

  * **watching** -- did both decide the child was watching? A disagreement here means the
    budget accrues at different rates, which eventually means blocking at the wrong time.
  * **blocked**  -- did both reach the same enforcement decision? This one is the actual
    product, and any disagreement is disqualifying.

Absolute `used=` totals are deliberately NOT compared: the shadow watcher starts from a
fresh state file part-way through a day, so its total is legitimately lower. The *rate*
is what has to match, and that is what the watching agreement measures.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SAMPLE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+\[sample\]\s+"
    r"(?P<activity>active|idle)"
    r".*?used=(?P<used>\d+)s"
    r".*?blocked=(?P<blocked>True|False)"
    r".*?kbps=(?P<kbps>\d+)"
    r".*?pins=(?P<pins>\d+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Sample:
    when: datetime
    watching: bool
    used: int
    blocked: bool
    kbps: int
    pins: int


def parse_log(path: Path) -> list[Sample]:
    out: list[Sample] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = SAMPLE_RE.match(line.strip())
        if not m:
            continue
        out.append(
            Sample(
                when=datetime.strptime(m["ts"], "%Y-%m-%d %H:%M:%S"),
                watching=m["activity"].lower() == "active",
                used=int(m["used"]),
                blocked=m["blocked"].lower() == "true",
                kbps=int(m["kbps"]),
                pins=int(m["pins"]),
            )
        )
    return out


def pair(a: list[Sample], b: list[Sample], tolerance_sec: int) -> list[tuple[Sample, Sample]]:
    """Nearest-neighbour pairing within a tolerance.

    The watchers tick on independent clocks, so samples never line up exactly; pairing on
    equality would report every sample as missing.
    """
    pairs: list[tuple[Sample, Sample]] = []
    used_b: set[int] = set()
    for sa in a:
        best_i, best_gap = None, None
        for i, sb in enumerate(b):
            if i in used_b:
                continue
            gap = abs((sa.when - sb.when).total_seconds())
            if gap <= tolerance_sec and (best_gap is None or gap < best_gap):
                best_i, best_gap = i, gap
        if best_i is not None:
            used_b.add(best_i)
            pairs.append((sa, b[best_i]))
    return pairs


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", type=Path, default=Path(r"C:\ProgramData\SafeHouse"))
    p.add_argument("--live", type=Path, default=None, help="PowerShell watcher log")
    p.add_argument("--shadow", type=Path, default=None, help="Python shadow log")
    p.add_argument("--tolerance", type=int, default=10,
                   help="seconds within which two samples are considered the same moment")
    p.add_argument("--show", type=int, default=10, help="how many disagreements to list")
    args = p.parse_args(argv)

    live_path = args.live or args.root / "youtube-budget.log"
    shadow_path = args.shadow or args.root / "youtube-budget-python-shadow.log"

    live = parse_log(live_path)
    shadow = parse_log(shadow_path)

    print(f"PowerShell : {len(live):5d} samples  {live_path}")
    print(f"Python     : {len(shadow):5d} samples  {shadow_path}")
    if not live or not shadow:
        print("\nNothing to compare - is the shadow task running?")
        return 1

    # Only the overlapping period is meaningful; the shadow watcher started later.
    start = max(live[0].when, shadow[0].when)
    end = min(live[-1].when, shadow[-1].when)
    live = [s for s in live if start <= s.when <= end]
    shadow = [s for s in shadow if start <= s.when <= end]
    print(f"Overlap    : {start:%Y-%m-%d %H:%M:%S} .. {end:%H:%M:%S}")

    pairs = pair(live, shadow, args.tolerance)
    if not pairs:
        print("\nNo samples paired - clocks further apart than the tolerance?")
        return 1

    watch_disagree = [(a, b) for a, b in pairs if a.watching != b.watching]
    block_disagree = [(a, b) for a, b in pairs if a.blocked != b.blocked]

    print(f"Paired     : {len(pairs)}")
    print()
    print(f"  watching agreement : {100 * (1 - len(watch_disagree) / len(pairs)):6.2f}%"
          f"   ({len(watch_disagree)} disagreements)")
    print(f"  blocked  agreement : {100 * (1 - len(block_disagree) / len(pairs)):6.2f}%"
          f"   ({len(block_disagree)} disagreements)")

    pin_gap = sum(abs(a.pins - b.pins) for a, b in pairs) / len(pairs)
    print(f"  mean pin-count gap : {pin_gap:6.2f}   "
          f"(PowerShell accumulated for longer; should converge)")

    if watch_disagree:
        print(f"\n  first {min(args.show, len(watch_disagree))} watching disagreements:")
        for a, b in watch_disagree[: args.show]:
            print(f"    {a.when:%H:%M:%S}  ps={'active' if a.watching else 'idle  '}"
                  f" kbps={a.kbps:<7} | py={'active' if b.watching else 'idle  '}"
                  f" kbps={b.kbps:<7}")

    if block_disagree:
        print(f"\n  first {min(args.show, len(block_disagree))} BLOCKED disagreements"
              f" (these are disqualifying):")
        for a, b in block_disagree[: args.show]:
            print(f"    {a.when:%H:%M:%S}  ps blocked={a.blocked} used={a.used}"
                  f" | py blocked={b.blocked} used={b.used}")

    print()
    if block_disagree:
        print("VERDICT: do NOT cut over - the two disagree on enforcement.")
        return 2
    if len(watch_disagree) / len(pairs) > 0.05:
        print("VERDICT: not yet - metering diverges by more than 5%.")
        return 2
    print("VERDICT: the two agree. Cutover is defensible once the overlap covers")
    print("         a full day including a midnight reset and a real block/unblock.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
