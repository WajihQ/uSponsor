"""Incremental channel scraping via yt-dlp (no API keys).

Two scan modes, both writing to the same database:

- **base** (the default): lists the newest uploads per channel (one cheap
  flat request), then fetches full metadata only for videos we haven't
  stored yet — repeat runs stay fast and light.
- **backfill**: lists the channel's *entire* uploads feed and walks it
  newest → oldest, fetching every unseen video until it reaches the
  cutoff (N years back). Slower by nature, so it sleeps between fetches
  to stay under YouTube's radar.
"""
import datetime as dt
import threading
import time

from yt_dlp import YoutubeDL

from . import db
from .detector import brand_key, detect_sponsors

LOOKBACK_ENTRIES = 30       # base scan: how many newest uploads to list per channel
MAX_NEW_PER_SCAN = 12       # base scan: cap detail fetches per channel per scan
BACKFILL_SLEEP = 1.5        # backfill: polite delay (seconds) between video fetches
BACKFILL_HARD_CAP = 600     # backfill: safety cap on fetches per channel per run

# Shared progress state for the web UI.
STATE = {
    "running": False,
    "mode": "base",
    "current": "",
    "done": 0,
    "total": 0,
    "log": [],
    "finished_at": None,
}
_lock = threading.Lock()


def _log(msg):
    with _lock:
        STATE["log"].append(msg)
        STATE["log"][:] = STATE["log"][-200:]


def _set_current(label):
    with _lock:
        STATE["current"] = label


def _ydl(extra=None):
    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    if extra:
        opts.update(extra)
    return YoutubeDL(opts)


def _list_uploads(channel_url, limit=LOOKBACK_ENTRIES):
    """One flat request: channel name/id + newest-first video entries.

    limit=None lists the entire uploads feed (used by backfill).
    """
    url = channel_url.rstrip("/") + "/videos"
    opts = {"extract_flat": "in_playlist"}
    if limit:
        opts["playlistend"] = limit
    with _ydl(opts) as y:
        info = y.extract_info(url, download=False)
    entries = [e for e in (info.get("entries") or []) if e and e.get("id")]
    name = info.get("channel") or info.get("uploader") or info.get("title") or channel_url
    name = name.removesuffix(" - Videos")
    return info.get("channel_id") or info.get("id"), name, entries


def _fetch_video(video_id):
    with _ydl() as y:
        return y.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)


def _store_video(conn, ch, v):
    """Insert a fetched video + its detected sponsorships. Returns (stored?, n_spons, date)."""
    raw_date = v.get("upload_date")  # YYYYMMDD
    upload_date = (
        dt.datetime.strptime(raw_date, "%Y%m%d").date().isoformat() if raw_date else None
    )
    cur = conn.execute(
        "INSERT OR IGNORE INTO videos (video_id, channel_ref, title, url, upload_date)"
        " VALUES (?, ?, ?, ?, ?)",
        (v["id"], ch["id"], v.get("title"), v.get("webpage_url"), upload_date),
    )
    if not cur.rowcount:
        return False, 0, upload_date
    n = 0
    for brand, evidence in detect_sponsors(v.get("description")):
        conn.execute(
            "INSERT OR IGNORE INTO sponsorships (video_ref, brand, brand_key, evidence)"
            " VALUES (?, ?, ?, ?)",
            (cur.lastrowid, brand, brand_key(brand), evidence),
        )
        n += 1
    conn.commit()
    return True, n, upload_date


def _update_channel_meta(conn, ch, channel_id, name):
    conn.execute(
        "UPDATE channels SET channel_id = ?, name = ?, last_scanned = datetime('now') WHERE id = ?",
        (channel_id, name, ch["id"]),
    )
    conn.commit()


def _known_ids(conn, ch):
    """Map of already-stored video_id -> upload_date for a channel."""
    return {
        r["video_id"]: r["upload_date"]
        for r in conn.execute(
            "SELECT video_id, upload_date FROM videos WHERE channel_ref = ?", (ch["id"],)
        )
    }


def scan_channel(conn, ch):
    """Base scan of one channel row; returns (name, new_videos, new_sponsorships)."""
    channel_id, name, entries = _list_uploads(ch["input_url"])
    _update_channel_meta(conn, ch, channel_id, name)
    known = _known_ids(conn, ch)
    fresh = [e for e in entries if e["id"] not in known][:MAX_NEW_PER_SCAN]

    new_videos = new_spons = 0
    for entry in fresh:
        try:
            v = _fetch_video(entry["id"])
        except Exception as exc:  # video may be private/removed; keep going
            _log(f"  ! skipped {entry['id']}: {exc}")
            continue
        stored, n, _ = _store_video(conn, ch, v)
        new_videos += stored
        new_spons += n
    return name, new_videos, new_spons


def backfill_channel(conn, ch, cutoff):
    """Backfill one channel down to `cutoff` (a date); returns (name, new_videos, new_spons).

    The uploads feed is newest-first, so we stop at the first fetched video
    older than the cutoff. Already-stored videos are skipped without a fetch.
    """
    channel_id, name, entries = _list_uploads(ch["input_url"], limit=None)
    _update_channel_meta(conn, ch, channel_id, name)
    known = _known_ids(conn, ch)

    new_videos = new_spons = fetched = 0
    for entry in entries:
        if entry["id"] in known:
            stored_date = known[entry["id"]]
            if stored_date and stored_date < cutoff.isoformat():
                break  # already walked past the cutoff on a previous run
            continue
        if fetched >= BACKFILL_HARD_CAP:
            _log(f"  ! {name}: hit the {BACKFILL_HARD_CAP}-video safety cap — run backfill again to continue")
            break
        try:
            v = _fetch_video(entry["id"])
        except Exception as exc:
            _log(f"  ! skipped {entry['id']}: {exc}")
            continue
        fetched += 1
        stored, n, upload_date = _store_video(conn, ch, v)
        new_videos += stored
        new_spons += n
        _set_current(f"{name} — {new_videos} video(s) so far ({upload_date or '?'})")
        if upload_date and upload_date < cutoff.isoformat():
            break  # reached the cutoff; everything older is out of range
        time.sleep(BACKFILL_SLEEP)
    return name, new_videos, new_spons


def run_scan(mode="base", years=1, target="all", force=False):
    """Scan channels in the DB. Safe to call from a background thread.

    target: "all" or "closed" (only channels marked closed).
    Base scans skip channels scanned within the last 24 hours unless
    force=True; backfills always visit every targeted channel (they skip
    already-stored videos anyway).
    """
    with _lock:
        if STATE["running"]:
            return
        STATE.update(running=True, mode=mode, done=0, total=0, current="", log=[], finished_at=None)
    cutoff = dt.date.today() - dt.timedelta(days=int(years) * 365)
    if mode == "backfill":
        _log(f"Backfill scan: going back {years} year(s), to {cutoff.isoformat()}")
    conn = db.connect()
    try:
        where = []
        if target == "closed":
            where.append("status = 'closed'")
        if mode == "base" and not force:
            where.append("(last_scanned IS NULL OR last_scanned <= datetime('now', '-24 hours'))")
        sql = "SELECT * FROM channels"
        if where:
            sql += " WHERE " + " AND ".join(where)
        channels = conn.execute(sql + " ORDER BY id").fetchall()
        total_all = conn.execute(
            "SELECT COUNT(*) FROM channels" + (" WHERE status = 'closed'" if target == "closed" else "")
        ).fetchone()[0]
        skipped = total_all - len(channels)
        if skipped:
            _log(f"Skipping {skipped} channel(s) scanned within the last 24 hours")
        if not channels:
            _log("Nothing to scan — all targeted channels were scanned recently.")
        with _lock:
            STATE["total"] = len(channels)
        for ch in channels:
            label = ch["name"] or ch["input_url"]
            _set_current(label)
            try:
                if mode == "backfill":
                    name, nv, ns = backfill_channel(conn, ch, cutoff)
                else:
                    name, nv, ns = scan_channel(conn, ch)
                _log(f"{name}: {nv} new video(s), {ns} sponsorship(s)")
            except Exception as exc:
                _log(f"{label}: FAILED — {exc}")
            with _lock:
                STATE["done"] += 1
    finally:
        conn.close()
        with _lock:
            STATE["running"] = False
            STATE["current"] = ""
            STATE["finished_at"] = dt.datetime.now().strftime("%H:%M:%S")


def start_scan_in_background(mode="base", years=1, target="all", force=False):
    """Kick off a scan thread if one isn't already running. Returns started?"""
    with _lock:
        if STATE["running"]:
            return False
    threading.Thread(
        target=run_scan,
        kwargs={"mode": mode, "years": years, "target": target, "force": force},
        daemon=True,
    ).start()
    return True
