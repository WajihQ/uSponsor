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


def _distribute(cap, want):
    """Share `cap` sends across campaigns proportional to their want (dict
    id->want). Returns id->sent ints summing to min(cap, total want)."""
    total = sum(want.values())
    if total <= cap:
        return dict(want)
    out = {k: v * cap // total for k, v in want.items()}          # floors
    rem = cap - sum(out.values())
    for k in sorted(want, key=lambda k: -((want[k] * cap) % total))[:rem]:
        out[k] += 1
    return out


def sending_forecast(days=6):
    """Projected emails per day for today + (days-1) ahead, from the live lead
    pipeline, then capped by real daily sending limits.

    A lead counts as *demand* on a day only if it's ACTIVE (status == 1; negative
    = bounced/unsubscribed, 3 = completed), hasn't replied, and its next sequence
    step is due that day. Demand is then capped per mailbox at the mailbox's
    daily_limit and per campaign at the campaign's daily_limit; anything over the
    cap staggers to the next scheduled day (this is what Instantly actually does,
    so the number tracks what really goes out, not just what's due).
    """
    campaigns = list(_paged("/campaigns"))
    active = [c for c in campaigns if c.get("status") == 1]
    accounts = {a["email"]: (a.get("daily_limit") or 0)
                for a in _paged("/accounts") if a.get("email")}

    leads_by = {}                       # campaign id -> [leads] (API can't filter, so group here)
    for L in iter_leads():
        leads_by.setdefault(L.get("campaign"), []).append(L)

    today = dt.date.today()
    horizon = [today + dt.timedelta(days=i) for i in range(days)]
    hset = set(horizon)

    # ---- 1. demand: leads due per (campaign, day), uncapped ----
    due = {c.get("id"): {d: 0 for d in horizon} for c in active}
    for c in active:
        cid = c.get("id")
        steps, nsteps = _steps_of(c), len(_steps_of(c))
        new_leads = 0
        def bump(d, n=1):
            if d in hset:
                due[cid][d] = due[cid].get(d, 0) + n
        for L in leads_by.get(cid, []):
            if L.get("status") != 1:
                continue
            if _num(L, "email_reply_count", "email_replied_count") > 0:
                continue
            cur = _lead_step(L)
            lc = _iso_date(_first(L, "timestamp_last_contact"))
            if cur < 0 or not lc:
                new_leads += 1
                continue
            d = dt.date.fromisoformat(lc)
            bump(d)                                          # step sent on last_contact (counts if today)
            step = cur
            while step + 1 < nsteps:
                delay = int(steps[step].get("delay") or 0)   # a step's delay is the wait AFTER it
                step += 1
                d = _next_send_day(c, max(d + dt.timedelta(days=delay), today))
                if d is None or d > horizon[-1]:
                    break
                bump(d)
        if new_leads:                                        # never-contacted -> first step upcoming
            d = _next_send_day(c, today)
            while new_leads > 0 and d in hset:
                bump(d, new_leads)                           # per-campaign cap applied below
                new_leads = 0

    # ---- 2. apply mailbox + campaign daily caps, staggering overflow forward ----
    by_mbox = {}                        # mailbox email -> [campaigns]
    for c in active:
        by_mbox.setdefault((_emails_of(c) or [None])[0], []).append(c)
    per_day = {d: {} for d in horizon}
    capped = set()
    for mb, camps in by_mbox.items():
        mlimit = accounts.get(mb)                            # None -> unknown mailbox, no cap
        carry = {c.get("id"): 0 for c in camps}
        for d in horizon:
            want, demand = {}, {}
            for c in camps:
                cid = c.get("id")
                dm = (due[cid][d] + carry[cid]) if _sends_on(c, d) else 0
                demand[cid] = dm
                want[cid] = min(dm, c.get("daily_limit") or dm)   # a campaign can't exceed its own limit
            twant = sum(want.values())
            if twant == 0:
                continue
            cap = twant if mlimit is None else min(twant, mlimit)
            sent = _distribute(cap, {k: v for k, v in want.items() if v})
            for c in camps:
                cid = c.get("id")
                s = sent.get(cid, 0)
                carry[cid] = demand[cid] - s                 # unsent leads wait for the next day
                if s:
                    nm = c.get("name") or cid
                    per_day[d][nm] = per_day[d].get(nm, 0) + s
            if sum(demand.values()) > sum(sent.values()):
                capped.add(d)

    out_days = []
    for d in horizon:
        camps = sorted(({"name": n, "count": ct} for n, ct in per_day[d].items()),
                       key=lambda x: -x["count"])
        out_days.append({
            "date": d.isoformat(), "label": d.strftime("%a %b %d"),
            "today": d == today, "weekend": d.weekday() >= 5, "capped": d in capped,
            "total": sum(x["count"] for x in camps), "campaigns": camps,
        })
    return {
        "days": out_days,
        "active_campaigns": len(active),
        "total_campaigns": len(campaigns),
        "generated_at": dt.datetime.now().strftime("%b %d, %H:%M"),
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
