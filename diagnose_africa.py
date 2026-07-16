"""Isolate the Africa campaign's own leads (client-side filter) to confirm how a
BOUNCED lead is marked. Read-only. Short output.

    python diagnose_africa.py
"""
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")
from tracker import instantly as ins

camps = list(ins._paged("/campaigns"))
africa = [c for c in camps if "africa" in (c.get("name") or "").lower()]
ids = {c["id"] for c in africa}
for c in africa:
    print(f"Africa campaign id: {c['id']}  status={c.get('status')}  name={c.get('name')}")

# campaign-level analytics — may carry a bounce count directly
for c in africa:
    for path in ("/campaigns/analytics", "/campaigns/analytics/overview"):
        try:
            an = ins._request("GET", path, params={"campaign_id": c["id"]})
            print(f"\nanalytics via {path}?campaign_id=:")
            print(json.dumps(an, indent=1, default=str)[:1200])
            break
        except Exception as e:
            print(f"  {path} failed: {e}")

# the campaign's OWN leads, filtered client-side
leads = [L for L in ins.iter_leads() if L.get("campaign") in ids]
print(f"\n{len(leads)} lead(s) actually in Africa:\n")
for L in leads:
    keep = {k: L[k] for k in L if k not in ("payload", "organization", "assigned_to",
            "added_by", "modified_by", "id", "company_domain")}
    print("  " + json.dumps(keep, default=str))
    print(f"      email={L.get('email')}  derive_status={ins.derive_status(L)}\n")
