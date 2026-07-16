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


def _steps_of(campaign):
    steps = []
    for s in (campaign.get("sequences") or []):
        steps.extend(s.get("steps") or [])
    return steps


def _lead_step(lead):
    """0-based index of the last step a lead received, or -1 if not yet sent.
    stepID looks like 'seq_step_variant' e.g. '0_1_0' = 2nd email sent."""
    ls = (lead.get("status_summary") or {}).get("lastStep") or {}
    sid = ls.get("stepID")
    if not sid or not lead.get("timestamp_last_contact"):
        return -1
    try:
        return int(str(sid).split("_")[1])
    except (IndexError, ValueError):
        return -1


def _next_send_day(campaign, base, limit=21):
    """First date >= base the campaign's schedule actually sends on."""
    for i in range(limit):
        d = base + dt.timedelta(days=i)
        if _sends_on(campaign, d):
            return d
    return None


def sending_forecast(days=6):
    """Projected emails per day for today + (days-1) ahead, from the live lead
    pipeline. A lead counts toward a day only if it's ACTIVE (Instantly
    status == 1 — excludes bounced/unsubscribed = negative status, and completed
    = status 3), hasn't replied, and its next sequence step falls on that day.
    """
    campaigns = list(_paged("/campaigns"))
    active = [c for c in campaigns if c.get("status") == 1]

    leads_by = {}                       # campaign id -> [leads] (API can't filter, so group here)
    for L in iter_leads():
        leads_by.setdefault(L.get("campaign"), []).append(L)

    today = dt.date.today()
    horizon = [today + dt.timedelta(days=i) for i in range(days)]
    hset = set(horizon)
    per_day = {d: {} for d in horizon}  # date -> {campaign name: count}

    def add(d, name, n=1):
        if d in hset and n:
            per_day[d][name] = per_day[d].get(name, 0) + n

    for c in active:
        steps = _steps_of(c)
        nsteps = len(steps)
        name = c.get("name") or c.get("id")
        new_leads = 0
        for L in leads_by.get(c.get("id"), []):
            if L.get("status") != 1:                 # active only (drops bounced/unsub/completed)
                continue
            if _num(L, "email_reply_count", "email_replied_count") > 0:
                continue                             # replied -> sequence stops
            cur = _lead_step(L)
            lc = _iso_date(_first(L, "timestamp_last_contact"))
            if cur < 0 or not lc:
                new_leads += 1                       # never sent yet -> paced below
                continue
            d = dt.date.fromisoformat(lc)
            add(d, name, 1)                          # the step just sent on last_contact (counts if today)
            step = cur
            while step + 1 < nsteps:                 # then chain every remaining step forward
                step += 1
                base = max(d + dt.timedelta(days=int(steps[step].get("delay") or 0)), today)
                d = _next_send_day(c, base)
                if d is None or d > horizon[-1]:
                    break
                add(d, name, 1)
        # never-contacted leads get step 1 on upcoming send days, paced by daily_limit
        if new_leads:
            limit = c.get("daily_limit") or new_leads
            d = _next_send_day(c, today)
            while new_leads > 0 and d in hset:
                take = min(limit, new_leads)
                add(d, name, take)
                new_leads -= take
                nd = _next_send_day(c, d + dt.timedelta(days=1))
                if not nd:
                    break
                d = nd

    out_days = []
    for d in horizon:
        camps = sorted(({"name": n, "count": ct} for n, ct in per_day[d].items()),
                       key=lambda x: -x["count"])
        out_days.append({
            "date": d.isoformat(), "label": d.strftime("%a %b %d"),
            "today": d == today, "weekend": d.weekday() >= 5,
            "total": sum(x["count"] for x in camps), "campaigns": camps,
        })
    return {
        "days": out_days,
        "active_campaigns": len(active),
        "total_campaigns": len(campaigns),
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
