"""Validate the sending model against finished campaigns' real send history.

    python scripts/instantly/verify_campaigns.py   (run from the repo root)

Read-only. For each completed/paused campaign it pulls actual emails-sent-per-day
and compares to the model: daily_limit on scheduled weekdays. It separates the
STEADY pattern (days at/near the peak) from stragglers (the ramp-down tail and
limit-staggered spillover) so outliers don't distort the check.
"""
import datetime as dt
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

sys.stdout.reconfigure(encoding="utf-8")
from tracker import instantly as ins

if not ins.configured():
    sys.exit("No API key configured.")

DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

def sched_weekdays(c):
    """Set of python weekday indices this campaign is scheduled to send on."""
    out = set()
    for s in ((c.get("campaign_schedule") or {}).get("schedules") or []):
        for k, on in (s.get("days") or {}).items():
            if on:
                out.add((int(k) - 1) % 7)   # Instantly 0=Sun..6=Sat -> python Mon=0..Sun=6
    return out

def daily_sends(cid):
    """[(date, sent)] for a campaign, sent>0 only. Tries the per-campaign filter."""
    for params in ({"campaign_id": cid}, {"id": cid}):
        try:
            rows = ins._request("GET", "/campaigns/analytics/daily", params=params)
        except Exception:
            continue
        if isinstance(rows, list):
            got = [(r.get("date"), r.get("sent") or 0) for r in rows]
            return [(d, s) for d, s in got if d and s > 0]
    return []

camps = list(ins._paged("/campaigns"))
finished = [c for c in camps if c.get("status") != 1]
print(f"{len(camps)} campaigns, {len(finished)} finished/paused\n")

checked = 0
for c in finished:
    days = daily_sends(c.get("id"))
    if not days:
        continue
    checked += 1
    if checked > 8:
        print("... (stopping at 8 campaigns)")
        break
    days.sort()
    limit = c.get("daily_limit")
    mbs = ins._emails_of(c)
    sched = sched_weekdays(c)
    counts = [s for _, s in days]
    peak = max(counts)
    steady = [s for s in counts if s >= peak * 0.6]      # regular pattern
    strag = [s for s in counts if s < peak * 0.6]        # ramp/stragglers
    off_sched = [(d, s) for d, s in days
                 if dt.date.fromisoformat(d).weekday() not in sched]

    print(f"### {c.get('name')}")
    print(f"    daily_limit={limit}  mailbox={mbs}  scheduled={sorted(DOW[w] for w in sched)}")
    print(f"    sent on {len(days)} day(s), {days[0][0]} .. {days[-1][0]}, total {sum(counts)}")
    print(f"    steady days (>=60% of peak): n={len(steady)} "
          f"median={st.median(steady) if steady else 0:g} min={min(steady) if steady else 0} max={max(steady) if steady else 0}")
    print(f"    stragglers (< 60% peak): {sorted(strag, reverse=True)}")
    print(f"    peak day = {peak}   exceeds daily_limit? {'YES -> limit not a hard cap' if limit and peak > limit else 'no'}")
    print(f"    off-schedule send days: {off_sched if off_sched else 'none'}")
    print("    --- day-by-day: " + ", ".join(
        f"{d}({DOW[dt.date.fromisoformat(d).weekday()]})={s}" for d, s in days))
    print()

if not checked:
    print("No finished campaigns returned send history (per-campaign analytics may "
          "need a different param — paste this note back).")
