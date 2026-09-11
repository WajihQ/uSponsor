"""Gmail → CRM sync: stamp first/last-contacted on leads from your Sent mail.

Reads only header metadata (To/Cc/Bcc/Date) of messages in the SENT label for
every connected Google account, matches recipients to lead emails in the
`channels` (influencer) and `brand_leads` (brand) tables, and records when you
first and last emailed each lead plus a follow-up count.

Auth: either `scripts/gmail/connect_gmail.py` (one browser consent per
account, run from a terminal) or the Settings page's Connect/Reconnect
links (build_auth_url/finish_oauth below — same client_secret.json, just
driven through this app's own server instead of a throwaway one). Tokens
are stored in the `gmail_tokens` DB table (not local files -- survives an
ephemeral host's redeploys). The Google client libraries are imported
lazily so the rest of the app runs without them.

Two sync modes:
  - full resync (authoritative): reads ALL sent mail across every account,
    aggregates, and overwrites the three contact fields with the true values.
    Run once after connecting; safe to re-run.
  - incremental ("Sync now", or POST /cron/gmail-sync on a schedule): per
    account, reads only mail newer than that account's saved watermark and
    folds it in additively.
"""
import datetime as dt
import email.utils
import os
import threading
import time

from . import db

SCOPES = ["https://www.googleapis.com/auth/gmail.metadata"]
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT_SECRET_PATH = os.path.join(_ROOT, "scripts", "gmail", "client_secret.json")
_OAUTH_CALLBACK_PATH = "/crm/gmail/oauth/callback"

# The in-app (web-flow) connect/reconnect routes redirect back to this app's
# own http://127.0.0.1 server, never leaving the machine — safe to relax the
# https-only default that's normally right for a real web app. The desktop
# flow (InstalledAppFlow.run_local_server, used by connect_gmail.py) already
# sets this internally; the web-flow Flow class below does not, so it's set
# once here for both to share.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

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
    """Connected addresses, from the gmail_tokens table."""
    conn = db.connect()
    try:
        return sorted(r["account"] for r in conn.execute("SELECT account FROM gmail_tokens"))
    finally:
        conn.close()


def _save_token_json(account, token_json):
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO gmail_tokens (account, token_json, updated_at)"
            " VALUES (?, ?, datetime('now'))"
            " ON CONFLICT(account) DO UPDATE SET token_json = excluded.token_json,"
            " updated_at = excluded.updated_at",
            (account, token_json),
        )
        conn.commit()
    finally:
        conn.close()


def _creds(account):
    import json
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT token_json FROM gmail_tokens WHERE account = ?", (account,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise RuntimeError(f"No Gmail token stored for {account} — reconnect it.")
    creds = Credentials.from_authorized_user_info(json.loads(row["token_json"]), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_token_json(account, creds.to_json())
    return creds


def _service(creds):
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _save_new_token(creds):
    """Ask Gmail whose token this is, save it to the gmail_tokens table, and
    return the address. Shared by connect_gmail.py (desktop flow) and
    build_auth_url/finish_oauth (in-app web flow) below — one place that
    decides the storage format."""
    profile = _service(creds).users().getProfile(userId="me").execute()
    address = profile["emailAddress"]
    _save_token_json(address, creds.to_json())
    return address


def _oauth_flow(base_url, state=None):
    from google_auth_oauthlib.flow import Flow
    if not os.path.isfile(CLIENT_SECRET_PATH):
        raise RuntimeError(
            f"No client_secret.json at {CLIENT_SECRET_PATH} — see SETUP_GMAIL.md.")
    return Flow.from_client_secrets_file(
        CLIENT_SECRET_PATH, scopes=SCOPES,
        redirect_uri=base_url.rstrip("/") + _OAUTH_CALLBACK_PATH, state=state,
    )


def build_auth_url(base_url, login_hint=None):
    """Start the in-app connect/reconnect flow: returns (google_url, state).
    Caller (the /crm/gmail/connect route) stashes `state` in the session and
    redirects the browser to `google_url`; Google redirects back to
    /crm/gmail/oauth/callback with a `code` for finish_oauth() to exchange.

    prompt="consent" forces a fresh consent screen (and therefore a fresh
    refresh token) even for an account that already granted access before —
    the whole point when reconnecting a dead one. login_hint pre-selects a
    specific Google account in the picker when reconnecting a known address;
    omitted when connecting a brand-new one.
    """
    flow = _oauth_flow(base_url)
    return flow.authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="true",
        login_hint=login_hint or None,
    )


def finish_oauth(base_url, authorization_response_url, state):
    """Exchange the callback's `code` for tokens and save them. Returns the
    connected account's email address."""
    flow = _oauth_flow(base_url, state=state)
    flow.fetch_token(authorization_response=authorization_response_url)
    return _save_new_token(flow.credentials)


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

def _friendly_error(exc):
    """Short, UI-safe summary of a per-account sync failure. A dead/revoked
    refresh token is the recurring real-world case (Google session policies,
    a password change, manual revoke) — call that out specifically since the
    fix is a one-click reconnect, not a bug to chase."""
    from google.auth.exceptions import RefreshError
    if isinstance(exc, RefreshError):
        return "Reconnect this account — token expired or revoked"
    return f"Sync failed: {exc}"


def sync(authoritative=False):
    """Run a sync across all connected accounts. Returns (ok, message).

    Each account's work is isolated in its own try/except: one dead token
    used to abort the whole run, silently leaving every account after it in
    `accounts` (alphabetical order) un-synced too. Now a failing account is
    recorded with a friendly per-account error (via the existing
    `last_result` field — no watermark advance, so the next successful sync
    just resumes where it left off) and the loop continues. `ok=False` only
    when every account failed.
    """
    accounts = list_accounts()
    if not accounts:
        STATE["message"] = "No Gmail accounts connected — use the Connect button on Settings."
        return False, STATE["message"]
    STATE.update(running=True, done=0, total=len(accounts), current="", message="")
    conn = db.connect()
    try:
        failed = []
        if authoritative:
            all_sends, newest_by_acct = [], {}
            for i, acct in enumerate(accounts):
                STATE.update(current=acct, done=i)
                try:
                    svc = _service(_creds(acct))
                    sends, newest = _collect_sends(
                        svc, 0, progress=lambda n, a=acct: STATE.update(message=f"{a}: {n} sent read"))
                except Exception as e:
                    failed.append(acct)
                    _save_watermark(conn, acct, _watermark(conn, acct), _friendly_error(e))
                    continue
                all_sends += sends
                newest_by_acct[acct] = newest
            n = reconcile(conn, aggregate(all_sends), authoritative=True)
            for acct, newest in newest_by_acct.items():
                _save_watermark(conn, acct, newest, "full resync")
            ok_count = len(accounts) - len(failed)
            msg = f"Full resync: {n} lead field(s) updated from {ok_count}/{len(accounts)} account(s)."
        else:
            total = 0
            for i, acct in enumerate(accounts):
                STATE.update(current=acct, done=i)
                wm = _watermark(conn, acct)
                try:
                    svc = _service(_creds(acct))
                    sends, newest = _collect_sends(
                        svc, wm, progress=lambda n, a=acct: STATE.update(message=f"{a}: {n} new read"))
                except Exception as e:
                    failed.append(acct)
                    _save_watermark(conn, acct, wm, _friendly_error(e))
                    continue
                t = reconcile(conn, aggregate(sends), authoritative=False)
                total += t
                _save_watermark(conn, acct, max(newest, wm or 0), f"{t} update(s)")
            ok_count = len(accounts) - len(failed)
            msg = f"Sync: {total} lead field(s) updated across {ok_count}/{len(accounts)} account(s)."
        if failed:
            msg += f" {len(failed)} need reconnecting: {', '.join(failed)}."
        STATE.update(message=msg, done=len(accounts))
        STATE["last_run"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        return bool(ok_count), msg
    except Exception as e:  # anything outside the per-account try (DB, etc.) — surface, don't crash
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
