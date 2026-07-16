"""Instantly.ai (v2 API) -> CRM sync: pull per-lead campaign status.

Gmail sync already records *when* you contacted a lead; Instantly adds what
Gmail can't see — whether a campaign lead **replied / bounced / unsubscribed /
showed interest** — plus a cross-check on contact dates for mailboxes Gmail
isn't watching. The status lands in a dedicated `instantly_status` column so it
never clobbers a status you set by hand.

The API key (a base64 `id:token` string from Instantly → Settings → API) is read
from `instantly.json` ({"api_key": "..."}) or the INSTANTLY_API_KEY env var —
both local and gitignored. No third-party packages; uses urllib.

Field names in the v2 lead object are accessed defensively (several fallbacks)
because they vary; run `diagnose_instantly.py` to confirm the shape against a
real lead before trusting a sync.
"""
import datetime as dt
import json
import os
import statistics as st
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import db

BASE = "https://api.instantly.ai/api/v2"
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(_ROOT, "instantly.json")

STATE = {"running": False, "done": 0, "total": 0, "message": "", "last_run": None}

# Instantly lead interest codes -> label (positive = warm, negative = dead)
_INTEREST = {1: "interested", 2: "meeting booked", 3: "meeting completed",
             4: "closed", -1: "not interested", -2: "wrong person", -3: "lost"}


# --- key / config -----------------------------------------------------------

def api_key():
    key = os.environ.get("INSTANTLY_API_KEY", "").strip()
    if key:
        return key
    if os.path.isfile(CONFIG):
        try:
            with open(CONFIG, encoding="utf-8") as f:
                return (json.load(f).get("api_key") or "").strip()
        except (OSError, ValueError):
            return ""
    return ""


def configured():
    return bool(api_key())


# --- HTTP -------------------------------------------------------------------

def _request(method, path, params=None, body=None, tries=5):
    key = api_key()
    if not key:
        raise RuntimeError("No Instantly API key — add instantly.json or set INSTANTLY_API_KEY.")
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    delay = 1.0
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer " + key)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        # Cloudflare blocks the default python-urllib UA with 403 error 1010;
        # a normal browser UA gets through.
        req.add_header(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < tries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 20)
                continue
            detail = ""
            try:
                detail = e.read().decode()[:300]
            except Exception:
                pass
            raise RuntimeError(f"Instantly API {e.code}: {detail or e.reason}")
        except urllib.error.URLError as e:
            if attempt < tries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 20)
                continue
            raise RuntimeError(f"Instantly API unreachable: {e.reason}")


def _paged(path, params=None, body=None, method="GET"):
    """Yield items across Instantly's cursor pagination (next_starting_after)."""
    params = dict(params or {})
    params.setdefault("limit", 100)
    while True:
        if method == "POST":
            payload = dict(body or {}); payload.update(params)
            resp = _request("POST", path, body=payload)
        else:
            resp = _request("GET", path, params=params)
        items = resp.get("items", resp if isinstance(resp, list) else [])
        for it in items:
            yield it
        cursor = resp.get("next_starting_after")
        if not cursor or not items:
            break
        params["starting_after"] = cursor


def list_campaigns():
    """[(id, name)] of your campaigns."""
    out = []
    for c in _paged("/campaigns"):
        out.append((c.get("id"), c.get("name") or c.get("id")))
    return out


def iter_leads(campaign_id=None):
    body = {"campaign_id": campaign_id} if campaign_id else {}
    yield from _paged("/leads/list", body=body, method="POST")


# --- sending forecast (Campaigns page) --------------------------------------

def _emails_of(c):
    """The sending mailbox address(es) a campaign uses."""
    out = []
    for e in (c.get("email_list") or c.get("accounts") or []):
        if isinstance(e, str):
            out.append(e)
        elif isinstance(e, dict):
            out.append(e.get("email") or e.get("id"))
    return [e for e in out if e]


def _sends_on(campaign, d):
    """Is an active campaign scheduled to send on date d (any of its schedule
    blocks enables that weekday and the date is within start/end)?"""
    sched = campaign.get("campaign_schedule") or {}
    idx = str((d.weekday() + 1) % 7)          # Instantly days: 0=Sun .. 6=Sat
    for s in (sched.get("schedules") or []):
        if not (s.get("days") or {}).get(idx):
            continue
        sd = (sched.get("start_date") or "")[:10]
        ed = (sched.get("end_date") or "")[:10]
        if sd and d.isoformat() < sd:
            continue
        if ed and d.isoformat() > ed:
            continue
        return True
    return False


def _campaign_rate(cid, daily_limit):
    """A campaign's typical emails/day, from its recent real send history
    (median of its most recent non-zero sending days). Falls back to the
    campaign's daily_limit for a brand-new campaign with no history yet.
    Returns (rate, estimated?)."""
    try:
        rows = _request("GET", "/campaigns/analytics/daily", params={"campaign_id": cid})
    except Exception:
        rows = None
    sends = sorted(((r.get("date"), r.get("sent") or 0) for r in (rows or []) if r.get("date")),
                   reverse=True)
    recent = [s for _, s in sends if s > 0][:8]
    if recent:
        return st.median(recent), False
    return (daily_limit or 0), True


def sending_forecast(days=6):
    """Projected outreach emails per day for today + (days-1) ahead.

    Each active campaign contributes its recent real daily send rate on the days
    its schedule sends; the per-day total is just the sum across campaigns.
    """
    campaigns = list(_paged("/campaigns"))
    active = [c for c in campaigns if c.get("status") == 1]
    rates = {c.get("id"): _campaign_rate(c.get("id"), c.get("daily_limit")) for c in active}

    today = dt.date.today()
    out_days = []
    for i in range(days):
        d = today + dt.timedelta(days=i)
        camps, total = [], 0
        for c in active:
            if not _sends_on(c, d):
                continue
            rate, est = rates[c.get("id")]
            total += rate
            camps.append({"name": c.get("name") or c.get("id"), "count": round(rate), "est": est})
        out_days.append({
            "date": d.isoformat(), "label": d.strftime("%a %b %d"),
            "today": i == 0, "weekend": d.weekday() >= 5,
            "total": round(total),
            "campaigns": sorted(camps, key=lambda x: -x["count"]),
        })

    return {
        "days": out_days,
        "active_campaigns": len(active),
        "total_campaigns": len(campaigns),
        "any_estimated": any(est for _, est in rates.values()),
    }


# --- pure logic (unit-tested) ----------------------------------------------

def _num(lead, *names):
    for n in names:
        v = lead.get(n)
        if isinstance(v, (int, float)):
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)
    return 0


def _first(lead, *names):
    for n in names:
        if lead.get(n) not in (None, ""):
            return lead[n]
    return None


def _truthy(v):
    return v in (True, 1, "1", "true", "True")


def derive_status(lead):
    """Collapse a lead's engagement into one label for the CRM."""
    interest = lead.get("lt_interest_status")
    if isinstance(interest, str) and interest.lstrip("-").isdigit():
        interest = int(interest)
    if interest in _INTEREST:
        return _INTEREST[interest]
    if _truthy(_first(lead, "is_unsubscribed", "unsubscribed", "email_unsubscribed")):
        return "unsubscribed"
    if _num(lead, "email_reply_count", "email_replied_count", "reply_count") > 0:
        return "replied"
    if _truthy(_first(lead, "is_bounced", "bounced")):
        return "bounced"
    if _num(lead, "email_open_count", "email_opened_count", "open_count") > 0:
        return "opened"
    if _first(lead, "timestamp_last_contact", "timestamp_last_touch", "last_contacted") or \
       _num(lead, "email_sent_count", "sent_count") > 0:
        return "contacted"
    return "in campaign"


def _iso_date(v):
    """Instantly timestamp (ISO string or ms) -> 'YYYY-MM-DD' or None."""
    if not v:
        return None
    if isinstance(v, (int, float)):
        return dt.datetime.fromtimestamp(v / 1000, dt.timezone.utc).strftime("%Y-%m-%d")
    return str(v)[:10] if str(v)[:4].isdigit() else None


def reconcile(conn, leads, campaigns=None):
    """Fold Instantly leads into the CRM by email. Sets instantly_status +
    campaign name, and nudges last_contacted forward. Returns rows touched."""
    campaigns = campaigns or {}
    touched = 0
    for lead in leads:
        email = (_first(lead, "email") or "").strip().lower()
        if not email:
            continue
        status = derive_status(lead)
        camp = campaigns.get(lead.get("campaign")) or lead.get("campaign_name")
        last = _iso_date(_first(lead, "timestamp_last_contact", "last_contacted"))
        for table in ("channels", "brand_leads"):
            cur = conn.execute(
                f"UPDATE {table} SET instantly_status = ?, instantly_campaign = COALESCE(?, instantly_campaign),"
                f" last_contacted = CASE WHEN ? IS NOT NULL AND (last_contacted IS NULL OR last_contacted < ?)"
                f"   THEN ? ELSE last_contacted END"
                f" WHERE lower(email) = ?",
                (status, camp, last, last, last, email),
            )
            touched += cur.rowcount
    conn.commit()
    return touched


# --- orchestration ----------------------------------------------------------

def sync():
    if not configured():
        STATE["message"] = "No Instantly API key configured (see SETUP_INSTANTLY.md)."
        return False, STATE["message"]
    STATE.update(running=True, done=0, total=0, message="")
    conn = db.connect()
    try:
        campaigns = dict(list_campaigns())
        STATE.update(total=len(campaigns) or 1, message=f"{len(campaigns)} campaign(s)")
        leads = []
        for lead in iter_leads():
            leads.append(lead)
            if len(leads) % 200 == 0:
                STATE["message"] = f"read {len(leads)} leads…"
        touched = reconcile(conn, leads, campaigns)
        conn.execute(
            "INSERT INTO crm_sync (account, last_run, last_result) VALUES ('instantly', ?, ?)"
            " ON CONFLICT(account) DO UPDATE SET last_run = excluded.last_run,"
            " last_result = excluded.last_result",
            (dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
             f"{len(leads)} leads, {touched} CRM update(s)"),
        )
        conn.commit()
        msg = f"Instantly sync: {len(leads)} leads across {len(campaigns)} campaign(s), {touched} CRM update(s)."
        STATE.update(message=msg, last_run=dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
        return True, msg
    except Exception as e:
        import traceback
        traceback.print_exc()
        STATE["message"] = f"Instantly sync error: {e}"
        return False, str(e)
    finally:
        STATE["running"] = False
        conn.close()


def start_sync_in_background():
    if STATE["running"]:
        return False
    threading.Thread(target=sync, daemon=True).start()
    return True


def status():
    conn = db.connect()
    try:
        row = conn.execute("SELECT last_run, last_result FROM crm_sync WHERE account = 'instantly'").fetchone()
    finally:
        conn.close()
    return {
        "configured": configured(),
        "last_run": row["last_run"] if row else None,
        "last_result": row["last_result"] if row else None,
        "state": STATE,
    }
