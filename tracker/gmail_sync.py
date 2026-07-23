"""Gmail → CRM sync: stamp first/last-contacted on leads from your Sent mail.

Reads only header metadata (To/Cc/Bcc/Date) of messages in the SENT label for
every connected Google account, matches recipients to lead emails in the
`channels` (influencer) and `brand_leads` (brand) tables, and records when you
first and last emailed each lead plus a follow-up count.

Auth is set up out-of-band by `connect_gmail.py` (one browser consent per
account); tokens land in `gmail_tokens/<address>.json`. The Google client
libraries are imported lazily so the rest of the app runs without them.

Two modes:
  - full resync (authoritative): reads ALL sent mail across every account,
    aggregates, and overwrites the three contact fields with the true values.
    Run once after connecting; safe to re-run.
  - incremental (interval + "Sync now"): per account, reads only mail newer
    than that account's saved watermark and folds it in additively.
"""
import datetime as dt
import email.utils
import os
import threading
import time

from . import db

SCOPES = ["https://www.googleapis.com/auth/gmail.metadata"]
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKENS_DIR = os.path.join(_ROOT, "gmail_tokens")

STATE = {"running": False, "current": "", "done": 0, "total": 0,
         "message": "", "last_run": None}


# --- pure helpers (unit-tested) --------------------------------------------

def _extract_emails(header_value):
    """Lowercased addresses out of a To/Cc/Bcc header string."""
    if not header_value:
        return []
    return [addr.lower() for _, addr in email.utils.getaddresses([header_value]) if "@" in addr]


def _epoch_to_date(ms):
    return dt.datetime.fromtimestamp(int(ms) / 1000, dt.timezone.utc).strftime("%Y-%m-%d")


def aggregate(sends):
    """[(email, epoch_ms), ...] -> {email: [first_ms, last_ms, count]}."""
    agg = {}
    for addr, ts in sends:
        a = agg.get(addr)
        if a is None:
            agg[addr] = [ts, ts, 1]
        else:
            a[0] = min(a[0], ts)
            a[1] = max(a[1], ts)
            a[2] += 1
    return agg


def reconcile(conn, agg, authoritative):
    """Fold aggregated sends into the influencer + brand lead tables.

    authoritative=True overwrites the three contact fields from Gmail truth
    (use after a full, all-accounts scan). authoritative=False folds in new
    mail additively: earliest first-contact wins, latest last-contact wins, and
    the follow-up count grows by each new send (the very first send to a lead
    is the initial email, not a follow-up). Returns rows touched.
    """
    touched = 0
    for addr, (first_ms, last_ms, count) in agg.items():
        first, last = _epoch_to_date(first_ms), _epoch_to_date(last_ms)
        for table in ("channels", "brand_leads"):
            if authoritative:
                cur = conn.execute(
                    f"UPDATE {table} SET first_contacted = ?, last_contacted = ?,"
                    f" followup_count = ? WHERE lower(email) = ?",
                    (first, last, max(count - 1, 0), addr),
                )
            else:
                cur = conn.execute(
                    f"UPDATE {table} SET"
                    f" first_contacted = CASE WHEN first_contacted IS NULL OR first_contacted > ?"
                    f"   THEN ? ELSE first_contacted END,"
                    f" last_contacted = CASE WHEN last_contacted IS NULL OR last_contacted < ?"
                    f"   THEN ? ELSE last_contacted END,"
                    f" followup_count = COALESCE(followup_count, 0) + ?"
                    f"   - (CASE WHEN first_contacted IS NULL THEN 1 ELSE 0 END)"
                    f" WHERE lower(email) = ?",
                    (first, first, last, last, count, addr),
                )
            touched += cur.rowcount
    conn.commit()
    return touched


# --- account / credential plumbing -----------------------------------------

def list_accounts():
    """Connected addresses, from token filenames in gmail_tokens/."""
    if not os.path.isdir(TOKENS_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(TOKENS_DIR) if f.endswith(".json"))


def _token_path(account):
    return os.path.join(TOKENS_DIR, account + ".json")


def _creds(token_path):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return creds


def _service(creds):
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _execute(request, tries=6):
    """Run a Gmail API request, retrying transient errors with backoff.

    Gmail intermittently returns 429/500/502/503 (e.g. "Authentication backend
    unavailable") — without this a single blip would abort a whole sync.
    """
    from googleapiclient.errors import HttpError
    delay = 1.0
    for attempt in range(tries):
        try:
            return request.execute()
        except HttpError as e:
            status = getattr(e, "status_code", None) or getattr(getattr(e, "resp", None), "status", None)
            if int(status or 0) in (429, 500, 502, 503) and attempt < tries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            raise


def _collect_sends(service, since_epoch, progress=None):
    """Read SENT metadata newest-first until older than since_epoch.

    Returns (list of (email, epoch_ms), newest_epoch_seen). Fetches messages
    sequentially — slower than batching but reliable (batched metadata reads
    trip Gmail's per-user rate limit). For a one-time full history that's a
    minute or two; incremental syncs read only the new tail.
    """
    sends, newest, page, seen = [], since_epoch or 0, None, 0
    while True:
        resp = _execute(service.users().messages().list(
            userId="me", labelIds=["SENT"], pageToken=page, maxResults=100
        ))
        msgs = resp.get("messages", [])
        if not msgs:
            break
        stop = False
        for m in msgs:  # list is newest-first; honour the watermark
            msg = _execute(service.users().messages().get(
                userId="me", id=m["id"], format="metadata",
                metadataHeaders=["To", "Cc", "Bcc"],
            ))
            ts = int(msg.get("internalDate", 0))
            if since_epoch and ts <= since_epoch:
                stop = True
                break
            newest = max(newest, ts)
            headers = {h["name"].lower(): h["value"]
                       for h in msg.get("payload", {}).get("headers", [])}
            for field in ("to", "cc", "bcc"):
                for addr in _extract_emails(headers.get(field, "")):
                    sends.append((addr, ts))
            seen += 1
            if progress and seen % 25 == 0:
                progress(seen)
        if stop or "nextPageToken" not in resp:
            break
        page = resp["nextPageToken"]
    if progress:
        progress(seen)
    return sends, newest


def _watermark(conn, account):
    row = conn.execute("SELECT last_epoch FROM crm_sync WHERE account = ?", (account,)).fetchone()
    return row["last_epoch"] if row and row["last_epoch"] else 0


def _save_watermark(conn, account, epoch, result):
    conn.execute(
        "INSERT INTO crm_sync (account, last_epoch, last_run, last_result) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(account) DO UPDATE SET last_epoch = excluded.last_epoch,"
        " last_run = excluded.last_run, last_result = excluded.last_result",
        (account, epoch, dt.datetime.now().strftime("%Y-%m-%d %H:%M"), result),
    )
    conn.commit()


# --- orchestration ----------------------------------------------------------

def sync(authoritative=False):
    """Run a sync across all connected accounts. Returns (ok, message)."""
    accounts = list_accounts()
    if not accounts:
        STATE["message"] = "No Gmail accounts connected — run connect_gmail.py first."
        return False, STATE["message"]
    STATE.update(running=True, done=0, total=len(accounts), current="", message="")
    conn = db.connect()
    try:
        if authoritative:
            all_sends, newest_by_acct = [], {}
            for i, acct in enumerate(accounts):
                STATE.update(current=acct, done=i)
                svc = _service(_creds(_token_path(acct)))
                sends, newest = _collect_sends(
                    svc, 0, progress=lambda n, a=acct: STATE.update(message=f"{a}: {n} sent read"))
                all_sends += sends
                newest_by_acct[acct] = newest
            n = reconcile(conn, aggregate(all_sends), authoritative=True)
            for acct, newest in newest_by_acct.items():
                _save_watermark(conn, acct, newest, "full resync")
            msg = f"Full resync: {n} lead field(s) updated from {len(accounts)} account(s)."
        else:
            total = 0
            for i, acct in enumerate(accounts):
                STATE.update(current=acct, done=i)
                wm = _watermark(conn, acct)
                svc = _service(_creds(_token_path(acct)))
                sends, newest = _collect_sends(
                    svc, wm, progress=lambda n, a=acct: STATE.update(message=f"{a}: {n} new read"))
                t = reconcile(conn, aggregate(sends), authoritative=False)
                total += t
                _save_watermark(conn, acct, max(newest, wm or 0), f"{t} update(s)")
            msg = f"Sync: {total} lead field(s) updated."
        STATE.update(message=msg, done=len(accounts))
        STATE["last_run"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        return True, msg
    except Exception as e:  # network / auth / quota — surface, don't crash the app
        import traceback
        traceback.print_exc()  # show the real cause in the app's terminal
        STATE["message"] = f"Sync error: {e}"
        return False, str(e)
    finally:
        STATE.update(running=False, current="")
        conn.close()


def start_sync_in_background(authoritative=False):
    """Kick off a sync in a daemon thread. False if one is already running."""
    if STATE["running"]:
        return False
    threading.Thread(target=sync, kwargs={"authoritative": authoritative}, daemon=True).start()
    return True


def start_interval(minutes=30):
    """Background heartbeat: incremental sync now, then every `minutes` while
    the app runs. Runs immediately on start (not just after the first sleep)
    since this app is typically run for short sessions shorter than the
    interval — without an immediate run, a short session could see zero
    automatic syncs and last_contacted would look stale until "Sync now" is
    clicked by hand.
    """
    if not list_accounts():
        return False

    def _loop():
        sync(authoritative=False)
        while True:
            time.sleep(minutes * 60)
            if not STATE["running"]:
                sync(authoritative=False)

    threading.Thread(target=_loop, daemon=True).start()
    return True


def status():
    """Connected accounts with their last-sync info, for the CRM UI."""
    conn = db.connect()
    try:
        seen = {r["account"]: dict(r) for r in conn.execute("SELECT * FROM crm_sync")}
    finally:
        conn.close()
    return {
        "accounts": [
            {"account": a, "last_run": seen.get(a, {}).get("last_run"),
             "last_result": seen.get(a, {}).get("last_result")}
            for a in list_accounts()
        ],
        "state": STATE,
    }
