"""Read-only Instantly diagnostic — confirms the API works and the data shape.

    python scripts/instantly/diagnose_instantly.py   (run from the repo root)

Changes nothing. Prints: whether the key is accepted, your campaigns, the FULL
field set of one sample lead (so we lock the status mapping to reality), the
status our logic derives for a few leads, and how many Instantly lead emails
match a CRM lead.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

sys.stdout.reconfigure(encoding="utf-8")
from tracker import db, instantly as ins

if not ins.configured():
    sys.exit("No API key. Create instantly.json with {\"api_key\": \"...\"} "
             "or set INSTANTLY_API_KEY (see SETUP_INSTANTLY.md).")

try:
    campaigns = ins.list_campaigns()
except Exception as e:
    sys.exit(f"API call failed: {e}")

print(f"Key accepted. Campaigns: {len(campaigns)}")
for cid, name in campaigns[:20]:
    print(f"  - {name}  ({cid})")

# pull a small sample of leads
sample = []
try:
    for lead in ins.iter_leads():
        sample.append(lead)
        if len(sample) >= 25:
            break
except Exception as e:
    sys.exit(f"Lead listing failed: {e}")

print(f"\nPulled {len(sample)} sample lead(s).")
if sample:
    print("\n--- FULL FIELDS of one lead (paste this back to me) ---")
    print(json.dumps({k: sample[0][k] for k in sorted(sample[0])}, indent=2, default=str)[:2500])
    print("\n--- derived status for the sample ---")
    for lead in sample[:10]:
        print(f"  {lead.get('email'):40} -> {ins.derive_status(lead)}")

# match against CRM emails
conn = db.connect()
leads_emails = {(r[0] or "").strip().lower() for r in
                conn.execute("SELECT email FROM channels WHERE email!=''")} | \
               {(r[0] or "").strip().lower() for r in
                conn.execute("SELECT email FROM brand_leads WHERE email!=''")}
conn.close()
matched = [l.get("email") for l in sample if (l.get("email") or "").lower() in leads_emails]
print(f"\nSample emails matching a CRM lead: {len(matched)}/{len(sample)}  e.g. {matched[:6]}")
