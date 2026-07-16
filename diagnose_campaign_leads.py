"""Read-only probe of lead-level state, to rebuild the sending projection from
the live pipeline (excluding bounced/unsubscribed/replied leads).

    python diagnose_campaign_leads.py

Dumps: the Africa campaign's sequence delays + every one of its leads in full
(so we can see how a BOUNCED lead is marked and where step/last-contact live),
plus a per-active-campaign breakdown of how many leads are live vs dead.
"""
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")
from tracker import instantly as ins

if not ins.configured():
    sys.exit("No API key configured.")

DEAD = {"bounced", "unsubscribed", "replied", "interested", "meeting booked",
        "meeting completed", "closed", "not interested", "wrong person", "lost"}

camps = list(ins._paged("/campaigns"))

# 1. Africa campaign in full detail
africa = [c for c in camps if "africa" in (c.get("name") or "").lower()]
for c in africa:
    print(f"===== {c.get('name')}  (status={c.get('status')}, daily_limit={c.get('daily_limit')}) =====")
    for seq in (c.get("sequences") or []):
        for i, stp in enumerate(seq.get("steps") or []):
            print(f"  step {i+1}: delay={stp.get('delay')} {stp.get('delay_unit')}")
    leads = list(ins.iter_leads(c.get("id")))
    print(f"  {len(leads)} lead(s):\n")
    for L in leads:
        print("  " + json.dumps({k: L[k] for k in sorted(L)}, default=str)[:1400])
        print(f"      -> derive_status={ins.derive_status(L)}\n")

# 2. live-vs-dead breakdown for every active campaign
print("\n===== live vs dead leads per ACTIVE campaign =====")
names = {c.get("id"): c.get("name") for c in camps}
active = {c.get("id") for c in camps if c.get("status") == 1}
by = {}
for L in ins.iter_leads():
    cid = L.get("campaign")
    if cid not in active:
        continue
    d = by.setdefault(cid, {"total": 0, "dead": 0, "status": {}})
    d["total"] += 1
    s = ins.derive_status(L)
    d["status"][s] = d["status"].get(s, 0) + 1
    if s in DEAD:
        d["dead"] += 1
for cid, d in sorted(by.items(), key=lambda kv: -kv[1]["total"]):
    live = d["total"] - d["dead"]
    print(f"  {str(names.get(cid))[:38]:38} total={d['total']:4} live={live:4} dead={d['dead']:4}  {d['status']}")
