"""Trace why the APAC July-15 campaigns' leads are / aren't in the forecast.
Read-only.  python diagnose_apac.py
"""
import datetime as dt
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")
from tracker import instantly as ins

camps = list(ins._paged("/campaigns"))
active = [c for c in camps if c.get("status") == 1]
target = [c for c in active if "15th" in (c.get("name") or "").lower()] or active

leads_by = {}
for L in ins.iter_leads():
    leads_by.setdefault(L.get("campaign"), []).append(L)

today = dt.date.today()
print(f"today = {today} ({today.strftime('%a')})\n")

for c in target:
    steps = ins._steps_of(c)
    print(f"=== {c.get('name')} ===")
    print(f"  step delays: {[s.get('delay') for s in steps]}")
    for s in ((c.get('campaign_schedule') or {}).get('schedules') or []):
        print(f"  schedule days: {s.get('days')}  tz={s.get('timezone')}")
    ls = leads_by.get(c.get("id"), [])
    print(f"  {len(ls)} leads   status counts: {dict(Counter(L.get('status') for L in ls))}")
    due = Counter()
    for L in ls:
        st, step = L.get("status"), ins._lead_step(L)
        lc = ins._iso_date(L.get("timestamp_last_contact"))
        if st != 1:
            reason = f"EXCLUDED (status={st})"
        elif ins._num(L, "email_reply_count") > 0:
            reason = "EXCLUDED (replied)"
        elif step < 0:
            reason = "NEW (paced from today)"; due[str(ins._next_send_day(c, today))] += 1
        else:
            nxt = step + 1
            if nxt >= len(steps):
                reason = "EXCLUDED (sequence complete)"
            else:
                base = dt.date.fromisoformat(lc) + dt.timedelta(days=int(steps[nxt].get("delay") or 0))
                base = max(base, today)
                d = ins._next_send_day(c, base)
                reason = f"next step {nxt} due {d}"; due[str(d)] += 1
        if len(ls) <= 12 or step < 0 or st != 1:
            print(f"    {str(L.get('email'))[:34]:34} status={st} step={step} last={lc} -> {reason}")
    print(f"  => my forecast would place these leads on: {dict(due)}\n")
