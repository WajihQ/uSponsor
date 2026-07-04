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
    status      TEXT NOT NULL DEFAULT 'prospect'  -- 'prospect' | 'closed'
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
        # migrate databases created before the status column existed
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(channels)")}
        if "status" not in cols:
            conn.execute("ALTER TABLE channels ADD COLUMN status TEXT NOT NULL DEFAULT 'prospect'")


def add_channel(url):
    """Insert a channel by URL/handle. Returns (added, reason)."""
    url = normalize_channel_url(url)
    if not url:
        return False, "not a recognizable YouTube channel link or @handle"
    with connect() as conn:
        dup = conn.execute("SELECT 1 FROM channels WHERE input_url = ?", (url,)).fetchone()
        if dup:
            return False, "already in the list"
        conn.execute("INSERT INTO channels (input_url) VALUES (?)", (url,))
    return True, url


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
    """Parse a .txt/.csv blob: pull every channel-looking token from every line."""
    added, skipped = [], []
    for line in text.splitlines():
        for cell in line.replace(";", ",").split(","):
            cell = cell.strip()
            if not cell:
                continue
            if normalize_channel_url(cell):
                ok, info = add_channel(cell)
                (added if ok else skipped).append((cell, info))
    return added, skipped
