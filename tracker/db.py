"""SQLite storage for channels, videos and detected sponsorships."""
import csv
import io
import os
import re
import sqlite3

DB_PATH = os.environ.get(
    "USPONSOR_DB", os.path.join(os.path.dirname(os.path.dirname(__file__)), "sponsors.db")
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id          INTEGER PRIMARY KEY,
    input_url   TEXT NOT NULL,
    channel_id  TEXT UNIQUE,            -- YouTube channel id, filled on first scan
    name        TEXT,                   -- resolved channel name
    added_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_scanned TEXT,
    status      TEXT NOT NULL DEFAULT 'prospect', -- 'prospect' | 'closed'
    niche       TEXT,                             -- e.g. 'Tech'
    subniche    TEXT,                             -- e.g. 'Mini PCs'
    agency      TEXT,                             -- managing agency, if repped elsewhere
    backfilled_to TEXT,                           -- oldest date a completed backfill covered
    subscribers INTEGER,                          -- captured at scan time
    rate_integration TEXT,                        -- media-kit fields, hand-entered
    rate_dedicated TEXT,
    demo_gender TEXT,
    demo_geo    TEXT,
    demo_age    TEXT,
    notes       TEXT,
    -- influencer outreach CRM (mirrors the owner's Google Sheet); kept separate
    -- from `status` above, which drives scanner targeting and the ✓ badge
    email       TEXT,
    instagram   TEXT,
    revisit_later TEXT,                           -- 'yes' | 'no' | 'maybe'
    date_found  TEXT,
    crm_status  TEXT,                             -- outreach lifecycle (wait/soft rejection/...)
    first_contacted TEXT,                         -- date of initial email
    last_contacted  TEXT,                         -- newest follow-up / send (Gmail keeps fresh)
    followup_count  INTEGER NOT NULL DEFAULT 0,
    instantly_status   TEXT,                       -- synced from Instantly (replied/bounced/...)
    instantly_campaign TEXT                        -- campaign the lead sits in
);

CREATE TABLE IF NOT EXISTS brands (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    brand_key TEXT UNIQUE NOT NULL,               -- same normalization as sponsorships
    kind      TEXT NOT NULL DEFAULT 'known',      -- 'known' | 'erroneous'
    added_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS brand_leads (
    id          INTEGER PRIMARY KEY,
    person      TEXT,
    brand       TEXT,
    niche       TEXT,
    linkedin    TEXT,
    role        TEXT,
    email       TEXT,
    country     TEXT,
    location    TEXT,
    comments    TEXT,
    status      TEXT,                             -- outreach lifecycle (in talks / hard rejection / ...)
    influencers TEXT,                             -- which influencers this brand works with
    first_contacted TEXT,                         -- date of initial email
    last_contacted  TEXT,                         -- most recent send (Gmail keeps fresh)
    followup_count  INTEGER NOT NULL DEFAULT 0,
    instantly_status   TEXT,                       -- synced from Instantly (replied/bounced/...)
    instantly_campaign TEXT,                       -- campaign the lead sits in
    added_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS videos (
    id          INTEGER PRIMARY KEY,
    video_id    TEXT UNIQUE NOT NULL,
    channel_ref INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    title       TEXT,
    url         TEXT,
    upload_date TEXT,                   -- YYYY-MM-DD
    scanned_at  TEXT NOT NULL DEFAULT (datetime('now')),
    description TEXT,                   -- kept so detection can be re-run offline
    view_count  INTEGER,                -- stats captured at scan time
    like_count  INTEGER,
    comment_count INTEGER,
    sb_checked  INTEGER NOT NULL DEFAULT 0,  -- SponsorBlock queried yet?
    sb_sponsored INTEGER,               -- 1 = has a sponsor segment
    sb_segments TEXT,                   -- JSON [[start,end],...] seconds
    review      TEXT,                   -- NULL | 'pending' | 'resolved' | 'dismissed'
    review_note TEXT                    -- caption snippet shown in the review queue
);

CREATE TABLE IF NOT EXISTS sponsorships (
    id        INTEGER PRIMARY KEY,
    video_ref INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    brand     TEXT NOT NULL,            -- display name as written
    brand_key TEXT NOT NULL,            -- normalized for grouping
    evidence  TEXT,                     -- the matched disclosure text
    UNIQUE (video_ref, brand_key)
);

CREATE TABLE IF NOT EXISTS brand_aliases (
    alias_key TEXT PRIMARY KEY,                   -- normalized key of the variant name
    canonical TEXT NOT NULL                       -- display name it consolidates into
);

CREATE TABLE IF NOT EXISTS crm_sync (
    account     TEXT PRIMARY KEY,                 -- connected gmail address
    last_epoch  INTEGER,                          -- newest SENT internalDate (ms) processed
    last_run    TEXT,                             -- datetime of last successful sync
    last_result TEXT                              -- short human-readable summary
);

CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos(channel_ref);
CREATE INDEX IF NOT EXISTS idx_videos_date ON videos(upload_date);
CREATE INDEX IF NOT EXISTS idx_spons_brand ON sponsorships(brand_key);
"""


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # concurrent scan workers + web reads
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)
        # migrate databases created before newer channel columns existed
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(channels)")}
        if "status" not in cols:
            conn.execute("ALTER TABLE channels ADD COLUMN status TEXT NOT NULL DEFAULT 'prospect'")
        if "niche" not in cols:
            conn.execute("ALTER TABLE channels ADD COLUMN niche TEXT")
            conn.execute("ALTER TABLE channels ADD COLUMN subniche TEXT")
        if "agency" not in cols:
            conn.execute("ALTER TABLE channels ADD COLUMN agency TEXT")
        if "backfilled_to" not in cols:
            conn.execute("ALTER TABLE channels ADD COLUMN backfilled_to TEXT")
        if "subscribers" not in cols:
            conn.execute("ALTER TABLE channels ADD COLUMN subscribers INTEGER")
            for c in ("rate_integration", "rate_dedicated", "demo_gender", "demo_geo", "demo_age", "notes"):
                conn.execute(f"ALTER TABLE channels ADD COLUMN {c} TEXT")
        if "email" not in cols:  # influencer outreach CRM columns
            for c in ("email", "instagram", "revisit_later", "date_found",
                      "crm_status", "first_contacted", "last_contacted"):
                conn.execute(f"ALTER TABLE channels ADD COLUMN {c} TEXT")
            conn.execute("ALTER TABLE channels ADD COLUMN followup_count INTEGER NOT NULL DEFAULT 0")
        if "instantly_status" not in cols:  # Instantly sync columns
            conn.execute("ALTER TABLE channels ADD COLUMN instantly_status TEXT")
            conn.execute("ALTER TABLE channels ADD COLUMN instantly_campaign TEXT")
        blcols = {r["name"] for r in conn.execute("PRAGMA table_info(brand_leads)")}
        if blcols and "instantly_status" not in blcols:
            conn.execute("ALTER TABLE brand_leads ADD COLUMN instantly_status TEXT")
            conn.execute("ALTER TABLE brand_leads ADD COLUMN instantly_campaign TEXT")
        vcols = {r["name"] for r in conn.execute("PRAGMA table_info(videos)")}
        if "description" not in vcols:
            conn.execute("ALTER TABLE videos ADD COLUMN description TEXT")
        if "view_count" not in vcols:
            conn.execute("ALTER TABLE videos ADD COLUMN view_count INTEGER")
            conn.execute("ALTER TABLE videos ADD COLUMN like_count INTEGER")
            conn.execute("ALTER TABLE videos ADD COLUMN comment_count INTEGER")
        if "sb_checked" not in vcols:
            conn.execute("ALTER TABLE videos ADD COLUMN sb_checked INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE videos ADD COLUMN sb_sponsored INTEGER")
            conn.execute("ALTER TABLE videos ADD COLUMN sb_segments TEXT")
            conn.execute("ALTER TABLE videos ADD COLUMN review TEXT")
            conn.execute("ALTER TABLE videos ADD COLUMN review_note TEXT")
        bcols = {r["name"] for r in conn.execute("PRAGMA table_info(brands)")}
        if bcols and "kind" not in bcols:
            conn.execute("ALTER TABLE brands ADD COLUMN kind TEXT NOT NULL DEFAULT 'known'")
        # purge false-positive "brands" stored by older detector versions
        conn.execute(
            "DELETE FROM sponsorships WHERE brand_key IN"
            " ('http', 'https', 'www', 'link', 'checkout', 'thecheckout', 'cart', 'thecart')"
            " OR brand_key LIKE '%checkout'"
        )


def add_channel(url, niche=None, subniche=None, agency=None):
    """Insert a channel by URL/handle, with optional niche/agency tags.

    If the channel already exists, empty niche/sub-niche/agency fields are
    filled from the arguments (hand-set values are never overwritten).
    Returns (status, info): 'added' | 'updated' | 'duplicate' | 'invalid'.
    """
    url = normalize_channel_url(url)
    if not url:
        return "invalid", "not a recognizable YouTube channel link or @handle"
    fields = {
        "niche": (niche or "").strip()[:40] or None,
        "subniche": (subniche or "").strip()[:40] or None,
        "agency": (agency or "").strip()[:60] or None,
    }
    with connect() as conn:
        row = conn.execute(
            "SELECT id, niche, subniche, agency FROM channels WHERE input_url = ?", (url,)
        ).fetchone()
        if row:
            sets, vals = [], []
            for col, val in fields.items():
                if val and not row[col]:
                    sets.append(f"{col} = ?"); vals.append(val)
            if sets:
                conn.execute(
                    f"UPDATE channels SET {', '.join(sets)} WHERE id = ?", (*vals, row["id"])
                )
                return "updated", url
            return "duplicate", "already in the list"
        conn.execute(
            "INSERT INTO channels (input_url, niche, subniche, agency) VALUES (?, ?, ?, ?)",
            (url, fields["niche"], fields["subniche"], fields["agency"]),
        )
    return "added", url


def normalize_channel_url(raw):
    """Accept full URLs, @handles, /channel/UC… ids. Returns canonical URL or None."""
    s = (raw or "").strip().strip('"').strip("'").rstrip("/")
    if not s or s.startswith("#"):
        return None
    if s.startswith("@"):
        return f"https://www.youtube.com/{s}"
    if s.startswith("UC") and len(s) == 24 and " " not in s:
        return f"https://www.youtube.com/channel/{s}"
    if "youtube.com" in s or "youtu.be" in s:
        if not s.startswith("http"):
            s = "https://" + s
        # strip a trailing tab like /videos or /featured; scraper adds /videos itself
        for tail in ("/videos", "/featured", "/streams", "/shorts", "/community", "/about"):
            if s.endswith(tail):
                s = s[: -len(tail)]
        return s
    return None


def normalize_instagram_url(raw):
    """Accept an instagram.com profile link (or 'instagram.com/handle').
    Returns a canonical https URL, or None if it isn't an Instagram link."""
    s = (raw or "").strip().strip('"').strip("'").rstrip("/")
    if "instagram.com" not in s.lower():
        return None
    if not s.startswith("http"):
        s = "https://" + s
    return s


_HEADER_COLS = {
    "niche": "niche",
    "subniche": "subniche", "sub-niche": "subniche", "sub niche": "subniche",
    "agency": "agency", "management": "agency", "mgmt": "agency",
}


def import_channel_lines(text):
    """Parse a .txt/.csv blob into channels, with optional niche/agency columns.

    Without a header row, cells after a row's (single) channel link are read
    positionally as niche, sub-niche, agency. A header row (e.g.
    "channel,agency" or "link,niche,sub-niche,agency") maps columns by name
    instead, so an agency-only file doesn't need niche placeholders. Rows
    with several links import each link plainly. On re-import of an existing
    channel, blank fields get filled from the file; values already set are
    left alone. Returns (added, updated, skipped) lists.
    """
    added, updated, skipped = [], [], []
    colmap = None  # header name -> column index
    for lineno, line in enumerate(text.splitlines()):
        raw_cells = [c.strip().strip('"') for c in line.replace(";", ",").split(",")]
        cells = [c for c in raw_cells if c]
        links = [(i, c) for i, c in enumerate(raw_cells) if normalize_channel_url(c)]
        if not links:
            if lineno == 0 and cells:  # maybe a header row: map named columns
                found = {
                    _HEADER_COLS[c.lower()]: i
                    for i, c in enumerate(raw_cells)
                    if c.lower() in _HEADER_COLS
                }
                if found:
                    colmap = found
            continue
        if len(links) == 1:
            i, link = links[0]
            if colmap:
                get = lambda f: raw_cells[colmap[f]] if f in colmap and colmap[f] < len(raw_cells) else None
                fields = {f: get(f) for f in ("niche", "subniche", "agency")}
            else:
                extras = [c for c in raw_cells[i + 1 :] if c][:3]
                fields = dict(zip(("niche", "subniche", "agency"), extras + [None] * 3))
            results = [(link, add_channel(link, fields["niche"], fields["subniche"], fields["agency"]))]
        else:
            results = [(link, add_channel(link)) for _, link in links]
        for link, (status, info) in results:
            bucket = {"added": added, "updated": updated}.get(status, skipped)
            bucket.append((link, info))
    return added, updated, skipped


def import_brand_lines(text, kind="known"):
    """Import brand names ('known' or 'erroneous'), one or more per line.

    A name already present has its kind updated instead. Returns (added, skipped).
    """
    from .detector import brand_key
    added, skipped = [], []
    with connect() as conn:
        for line in text.splitlines():
            for cell in line.replace(";", ",").split(","):
                name = cell.strip().strip('"')
                if not name or name.lower() in ("brand", "brands", "name", "brand name"):
                    continue  # blank or header
                key = brand_key(name)
                if len(key) < 2:
                    skipped.append(name)
                    continue
                cur = conn.execute(
                    "INSERT INTO brands (name, brand_key, kind) VALUES (?, ?, ?)"
                    " ON CONFLICT(brand_key) DO UPDATE SET kind = excluded.kind",
                    (name, key, kind),
                )
                (added if cur.rowcount else skipped).append(name)
    return added, skipped


def alias_map(conn):
    """{alias_key: canonical display name} for detection-time consolidation."""
    return {r["alias_key"]: r["canonical"] for r in conn.execute("SELECT * FROM brand_aliases")}


def apply_alias(brand, amap):
    """Map a detected brand name through the alias table. -> (name, key)"""
    from .detector import brand_key
    key = brand_key(brand)
    if key in amap:
        canonical = amap[key]
        return canonical, brand_key(canonical)
    return brand, key


def consolidate_brand(conn, old_key, new_name):
    """Merge a brand's rows onto a canonical name (same effect as the Brands-tab
    rename): move sponsorship rows to the new key, drop duplicates, update the
    brands row, and remember the alias so future scans map the variant straight
    to the canonical name. Caller commits. No-op if the key is unchanged.
    """
    from .detector import brand_key
    new_key = brand_key(new_name)
    if len(new_key) < 2 or new_key == old_key:
        return False
    conn.execute(
        "UPDATE OR IGNORE sponsorships SET brand = ?, brand_key = ? WHERE brand_key = ?",
        (new_name, new_key, old_key),
    )
    conn.execute("DELETE FROM sponsorships WHERE brand_key = ?", (old_key,))
    conn.execute("UPDATE sponsorships SET brand = ? WHERE brand_key = ?", (new_name, new_key))
    conn.execute(
        "UPDATE OR IGNORE brands SET name = ?, brand_key = ? WHERE brand_key = ?",
        (new_name, new_key, old_key),
    )
    conn.execute("DELETE FROM brands WHERE brand_key = ?", (old_key,))
    conn.execute(
        "INSERT INTO brand_aliases (alias_key, canonical) VALUES (?, ?)"
        " ON CONFLICT(alias_key) DO UPDATE SET canonical = excluded.canonical",
        (old_key, new_name),
    )
    for r in conn.execute("SELECT alias_key, canonical FROM brand_aliases").fetchall():
        if r["alias_key"] != old_key and brand_key(r["canonical"]) == old_key:
            conn.execute(
                "UPDATE brand_aliases SET canonical = ? WHERE alias_key = ?",
                (new_name, r["alias_key"]),
            )
    return True


# ---------------------------------------------------------------------------
# CRM CSV import (one-time seed from the owner's Google Sheets)
# ---------------------------------------------------------------------------

def _hkey(s):
    """Normalize a CSV header cell for fuzzy matching (lowercase alnum)."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _parse_date(s):
    """Best-effort date -> 'YYYY-MM-DD', else None. Handles the common
    spreadsheet formats (ISO, US M/D/Y, D/M/Y is ambiguous so US wins)."""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y", "%d %b %Y",
                "%B %d, %Y", "%b %d, %Y", "%Y/%m/%d"):
        try:
            from datetime import datetime
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s[:10] if re.match(r"\d{4}-\d{2}-\d{2}", s) else None


def _parse_subs(s):
    """'1.2M' / '500K' / '12,300' -> int, else None."""
    s = (s or "").strip().replace(",", "")
    m = re.match(r"([\d.]+)\s*([kmb]?)", s.lower())
    if not m:
        return None
    try:
        n = float(m.group(1))
    except ValueError:
        return None
    return int(n * {"k": 1e3, "m": 1e6, "b": 1e9}.get(m.group(2), 1))


def _read_csv(text):
    """Yield header-keyed row dicts (keys normalized via _hkey)."""
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return [], []
    header = [_hkey(c) for c in rows[0]]
    out = []
    for r in rows[1:]:
        if not any(c.strip() for c in r):
            continue
        out.append({header[i]: (r[i].strip() if i < len(r) else "")
                    for i in range(len(header))})
    return header, out


# header alias -> our field, for the two sheets
_INFL_MAP = {
    "name": "name", "link": "link", "channel": "link", "url": "link",
    "niche": "niche", "subniche": "subniche",
    "subscribercount": "subscribers", "subscribers": "subscribers", "subs": "subscribers",
    "email": "email", "instagram": "instagram", "ig": "instagram",
    "additionalinformations": "notes", "additionalinformation": "notes",
    "notes": "notes", "additionalinfo": "notes",
    "revisitlater": "revisit_later",
    "datefound": "date_found",
    "status": "crm_status",
    "dateofinitialemail": "first_contacted", "initialemail": "first_contacted",
    "followup1date": "fu1", "followup2date": "fu2",
    "followup3date": "fu3", "followup4date": "fu4",
}

_BRAND_MAP = {
    "person": "person", "contact": "person", "name": "person",
    "brand": "brand", "company": "brand",
    "niche": "niche",
    "linkedinprofile": "linkedin", "linkedin": "linkedin",
    "role": "role", "title": "role",
    "emailcontact": "email", "email": "email",
    "country": "country", "location": "location",
    "comments": "comments", "notes": "comments",
    "dateofinitialemail": "first_contacted", "initialemail": "first_contacted",
    "status": "status",
    "influencers": "influencers", "influencer": "influencers",
}


def _map_row(row, mapping):
    """Apply a header-alias map to a normalized row dict -> {field: value}."""
    out = {}
    for hkey, val in row.items():
        field = mapping.get(hkey)
        if field and val:
            out[field] = val
    return out


def import_influencer_csv(text):
    """Seed the influencer CRM (the channels table) from a sheet CSV export.

    Rows are matched to existing channels by normalized Link URL (so the roster
    you already scan is enriched, not duplicated); unmatched rows with a valid
    link create a new 'prospect' channel. Blank-fill only: a value already set
    on a channel is never overwritten. Returns (added, updated, skipped).
    """
    added = updated = skipped = 0
    _, rows = _read_csv(text)
    crm_cols = ("name", "niche", "subniche", "subscribers", "notes", "email",
                "instagram", "revisit_later", "date_found", "crm_status",
                "first_contacted", "last_contacted", "followup_count")
    with connect() as conn:
        for row in rows:
            f = _map_row(row, _INFL_MAP)
            url = normalize_channel_url(f.get("link", ""))
            if not url:
                skipped += 1
                continue
            # derive contact tracking from initial + follow-up date columns
            fus = [_parse_date(f.get(k)) for k in ("fu1", "fu2", "fu3", "fu4")]
            fus = [d for d in fus if d]
            first = _parse_date(f.get("first_contacted"))
            dates = ([first] if first else []) + fus
            vals = {
                "name": f.get("name"),
                "niche": (f.get("niche") or "")[:40] or None,
                "subniche": (f.get("subniche") or "")[:40] or None,
                "subscribers": _parse_subs(f.get("subscribers")),
                "notes": f.get("notes"),
                "email": f.get("email"),
                "instagram": f.get("instagram"),
                "revisit_later": (f.get("revisit_later") or "").lower()[:10] or None,
                "date_found": _parse_date(f.get("date_found")),
                "crm_status": f.get("crm_status"),
                "first_contacted": first,
                "last_contacted": max(dates) if dates else None,
                "followup_count": len(fus),
            }
            existing = conn.execute(
                "SELECT * FROM channels WHERE input_url = ?", (url,)
            ).fetchone()
            if existing:
                sets, args = [], []
                for col in crm_cols:
                    new = vals.get(col)
                    if new in (None, "", 0):
                        continue
                    cur = existing[col]
                    if cur in (None, "", 0):  # blank-fill only
                        sets.append(f"{col} = ?")
                        args.append(new)
                # a sheet Status of 'closed' also flips the business relationship
                if (vals.get("crm_status") or "").lower() == "closed" and existing["status"] != "closed":
                    sets.append("status = ?")
                    args.append("closed")
                if sets:
                    conn.execute(
                        f"UPDATE channels SET {', '.join(sets)} WHERE id = ?",
                        (*args, existing["id"]),
                    )
                    updated += 1
                else:
                    skipped += 1
            else:
                cols = ["input_url"] + [c for c in crm_cols if vals.get(c) not in (None, "", 0)]
                if (vals.get("crm_status") or "").lower() == "closed":
                    cols.append("status")
                    vals["status"] = "closed"
                placeholders = ", ".join("?" for _ in cols)
                args = [url] + [vals[c] for c in cols[1:]]
                conn.execute(
                    f"INSERT INTO channels ({', '.join(cols)}) VALUES ({placeholders})",
                    args,
                )
                added += 1
    return added, updated, skipped


def import_brand_leads_csv(text):
    """Seed the brand CRM (brand_leads) from a sheet CSV export.

    Deduped by email, falling back to (person, brand). Blank-fill only on an
    existing lead. Returns (added, updated, skipped).
    """
    added = updated = skipped = 0
    cols = ("person", "brand", "niche", "linkedin", "role", "email", "country",
            "location", "comments", "status", "influencers", "first_contacted",
            "last_contacted", "followup_count")
    with connect() as conn:
        for row in _read_csv(text)[1]:
            f = _map_row(row, _BRAND_MAP)
            if not any(f.get(k) for k in ("email", "person", "brand")):
                skipped += 1
                continue
            first = _parse_date(f.get("first_contacted"))
            vals = {c: f.get(c) for c in cols}
            vals["first_contacted"] = first
            vals["last_contacted"] = first
            vals["followup_count"] = 0
            email = (f.get("email") or "").strip().lower()
            existing = None
            if email:
                existing = conn.execute(
                    "SELECT * FROM brand_leads WHERE lower(email) = ?", (email,)
                ).fetchone()
            if not existing and f.get("person") and f.get("brand"):
                existing = conn.execute(
                    "SELECT * FROM brand_leads WHERE person = ? AND brand = ?",
                    (f["person"], f["brand"]),
                ).fetchone()
            if existing:
                sets, args = [], []
                for col in cols:
                    new = vals.get(col)
                    if new in (None, "", 0):
                        continue
                    if existing[col] in (None, "", 0):
                        sets.append(f"{col} = ?")
                        args.append(new)
                if sets:
                    conn.execute(
                        f"UPDATE brand_leads SET {', '.join(sets)} WHERE id = ?",
                        (*args, existing["id"]),
                    )
                    updated += 1
                else:
                    skipped += 1
            else:
                use = [c for c in cols if vals.get(c) not in (None, "", 0)]
                placeholders = ", ".join("?" for _ in use)
                conn.execute(
                    f"INSERT INTO brand_leads ({', '.join(use)}) VALUES ({placeholders})",
                    [vals[c] for c in use],
                )
                added += 1
    return added, updated, skipped


def known_brand_names(conn):
    """[(display name, brand_key)] of CRM/known brands, for assisted detection."""
    return [
        (r["name"], r["brand_key"])
        for r in conn.execute("SELECT name, brand_key FROM brands WHERE kind = 'known'")
    ]
