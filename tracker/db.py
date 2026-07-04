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
    subniche    TEXT                              -- e.g. 'Mini PCs'
);

CREATE TABLE IF NOT EXISTS videos (
    id          INTEGER PRIMARY KEY,
    video_id    TEXT UNIQUE NOT NULL,
    channel_ref INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    title       TEXT,
    url         TEXT,
    upload_date TEXT,                   -- YYYY-MM-DD
    scanned_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sponsorships (
    id        INTEGER PRIMARY KEY,
    video_ref INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    brand     TEXT NOT NULL,            -- display name as written
    brand_key TEXT NOT NULL,            -- normalized for grouping
    evidence  TEXT,                     -- the matched disclosure text
    UNIQUE (video_ref, brand_key)
);

CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos(channel_ref);
CREATE INDEX IF NOT EXISTS idx_videos_date ON videos(upload_date);
CREATE INDEX IF NOT EXISTS idx_spons_brand ON sponsorships(brand_key);
"""


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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


def add_channel(url, niche=None, subniche=None):
    """Insert a channel by URL/handle, with optional niche tags.

    If the channel already exists, empty niche/sub-niche fields are filled
    from the arguments (hand-set values are never overwritten). Returns
    (status, info) where status is 'added' | 'updated' | 'duplicate' | 'invalid'.
    """
    url = normalize_channel_url(url)
    if not url:
        return "invalid", "not a recognizable YouTube channel link or @handle"
    niche = (niche or "").strip()[:40] or None
    subniche = (subniche or "").strip()[:40] or None
    with connect() as conn:
        row = conn.execute(
            "SELECT id, niche, subniche FROM channels WHERE input_url = ?", (url,)
        ).fetchone()
        if row:
            sets, vals = [], []
            if niche and not row["niche"]:
                sets.append("niche = ?"); vals.append(niche)
            if subniche and not row["subniche"]:
                sets.append("subniche = ?"); vals.append(subniche)
            if sets:
                conn.execute(
                    f"UPDATE channels SET {', '.join(sets)} WHERE id = ?", (*vals, row["id"])
                )
                return "updated", url
            return "duplicate", "already in the list"
        conn.execute(
            "INSERT INTO channels (input_url, niche, subniche) VALUES (?, ?, ?)",
            (url, niche, subniche),
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


def import_channel_lines(text):
    """Parse a .txt/.csv blob into channels, with optional niche columns.

    A row with exactly one channel link may carry up to two extra cells
    after it — niche and sub-niche (e.g. "youtube.com/@x, Tech, Mini PCs").
    Rows with several links import each link plainly. On re-import of an
    existing channel, blank niche fields get filled from the file; values
    already set are left alone. Returns (added, updated, skipped) lists.
    """
    added, updated, skipped = [], [], []
    for line in text.splitlines():
        cells = [c.strip() for c in line.replace(";", ",").split(",")]
        cells = [c for c in cells if c]
        links = [(i, c) for i, c in enumerate(cells) if normalize_channel_url(c)]
        if not links:
            continue  # header rows, comments, junk
        if len(links) == 1:
            i, link = links[0]
            extras = cells[i + 1 : i + 3]  # niche, sub-niche
            niche = extras[0] if extras else None
            subniche = extras[1] if len(extras) > 1 else None
            results = [(link, add_channel(link, niche, subniche))]
        else:
            results = [(link, add_channel(link)) for _, link in links]
        for link, (status, info) in results:
            bucket = {"added": added, "updated": updated}.get(status, skipped)
            bucket.append((link, info))
    return added, updated, skipped
