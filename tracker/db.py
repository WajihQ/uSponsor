"""SQLite storage for channels, videos and detected sponsorships."""
import os
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
    backfilled_to TEXT                            -- oldest date a completed backfill covered
);

CREATE TABLE IF NOT EXISTS brands (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    brand_key TEXT UNIQUE NOT NULL,               -- same normalization as sponsorships
    kind      TEXT NOT NULL DEFAULT 'known',      -- 'known' | 'erroneous'
    added_at  TEXT NOT NULL DEFAULT (datetime('now'))
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
        vcols = {r["name"] for r in conn.execute("PRAGMA table_info(videos)")}
        if "description" not in vcols:
            conn.execute("ALTER TABLE videos ADD COLUMN description TEXT")
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


def known_brand_names(conn):
    """[(display name, brand_key)] of CRM/known brands, for assisted detection."""
    return [
        (r["name"], r["brand_key"])
        for r in conn.execute("SELECT name, brand_key FROM brands WHERE kind = 'known'")
    ]
