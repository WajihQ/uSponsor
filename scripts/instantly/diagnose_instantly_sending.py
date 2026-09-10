"""Read-only probe of the Instantly sending-capacity data shapes.

    python scripts/instantly/diagnose_instantly_sending.py   (run from the repo root)

Changes nothing. Dumps one campaign object, one sending-account object, and
tries the analytics endpoints, so we can map fields for a "sending load" view
(daily limits, active status, which mailboxes a campaign uses, sends per day).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

sys.stdout.reconfigure(encoding="utf-8")
from tracker import instantly as ins

if not ins.configured():
    sys.exit("No API key configured (instantly.json / INSTANTLY_API_KEY).")


def show(label, obj, limit=2500):
    print(f"\n===== {label} =====")
    print(json.dumps(obj, indent=1, default=str)[:limit])


# 1. campaigns — full shape of one, plus a status/limit summary of all
try:
    camps = ins._request("GET", "/campaigns", params={"limit": 100})
    items = camps.get("items", camps if isinstance(camps, list) else [])
    print(f"campaigns returned: {len(items)}")
    if items:
        show("ONE FULL CAMPAIGN (paste this)", items[0])
        print("\n-- per-campaign key fields (guessed) --")
        for c in items:
            print(f"  {str(c.get('name'))[:34]:34} "
                  f"status={c.get('status')}  "
                  f"daily_limit={c.get('daily_limit')}  "
                  f"accounts={len(c.get('email_list') or c.get('accounts') or [])}")
except Exception as e:
    print("campaigns failed:", e)

# 2. sending accounts (mailboxes) — the real daily-capacity source
for path in ("/accounts", "/account"):
    try:
        acc = ins._request("GET", path, params={"limit": 100})
        items = acc.get("items", acc if isinstance(acc, list) else [])
        print(f"\naccounts via {path}: {len(items)}")
        if items:
            show("ONE FULL ACCOUNT (paste this)", items[0])
        break
    except Exception as e:
        print(f"accounts via {path} failed:", e)

# 3. analytics endpoints — historical daily sends
for path in ("/campaigns/analytics/daily", "/campaigns/analytics",
             "/analytics/campaigns/daily"):
    try:
        an = ins._request("GET", path)
        show(f"ANALYTICS via {path}", an, 1500)
        break
    except Exception as e:
        print(f"analytics via {path} failed:", e)
