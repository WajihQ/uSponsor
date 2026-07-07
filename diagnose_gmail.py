"""Read-only Gmail sync diagnostic. Changes nothing — just reports.

    python diagnose_gmail.py

Tells you: which accounts are connected, whether the Gmail API responds, what
recipients your recent Sent mail has, and how many of them match a lead email
in your CRM (the thing the sync actually needs to work).
"""
import sys
import traceback

sys.stdout.reconfigure(encoding="utf-8")
from tracker import db, gmail_sync as g

accounts = g.list_accounts()
print("Connected accounts:", accounts or "(none)")
if not accounts:
    print("-> No tokens in gmail_tokens/. Run:  python connect_gmail.py")
    sys.exit()

conn = db.connect()
infl = {(r[0] or "").strip().lower() for r in
        conn.execute("SELECT email FROM channels WHERE email IS NOT NULL AND email != ''")}
brand = {(r[0] or "").strip().lower() for r in
         conn.execute("SELECT email FROM brand_leads WHERE email IS NOT NULL AND email != ''")}
leads = {e for e in (infl | brand) if e}
print(f"Lead emails in CRM: {len(infl)} influencer + {len(brand)} brand "
      f"= {len(leads)} distinct with an email\n")

for acct in accounts:
    print("===", acct, "===")
    try:
        svc = g._service(g._creds(g._token_path(acct)))
        resp = svc.users().messages().list(
            userId="me", labelIds=["SENT"], maxResults=40).execute()
        ids = [m["id"] for m in resp.get("messages", [])]
        print(f"  SENT reachable. sample={len(ids)}  "
              f"est. total sent={resp.get('resultSizeEstimate')}")
        recips = []
        for mid in ids:
            msg = svc.users().messages().get(
                userId="me", id=mid, format="metadata",
                metadataHeaders=["To", "Cc", "Bcc"]).execute()
            hdr = {h["name"].lower(): h["value"]
                   for h in msg.get("payload", {}).get("headers", [])}
            for f in ("to", "cc", "bcc"):
                recips += g._extract_emails(hdr.get(f, ""))
        uniq = sorted(set(recips))
        matched = [e for e in uniq if e in leads]
        print(f"  recipients in sample: {len(uniq)}  e.g. {uniq[:6]}")
        print(f"  MATCHED to CRM leads: {len(matched)}  e.g. {matched[:6]}")
        if uniq and not matched:
            print("  ^ mail is readable but none of these addresses are in your CRM's "
                  "email column — that's why nothing got stamped.")
    except Exception:
        print("  ERROR talking to Gmail:")
        traceback.print_exc()
    print()

print("crm_sync watermarks / last results:")
rows = list(conn.execute("SELECT * FROM crm_sync"))
for r in rows:
    print("  ", dict(r))
if not rows:
    print("   (empty — no sync has recorded a watermark yet)")
print("\nLast sync status message:", g.STATE.get("message") or "(none)")
conn.close()
