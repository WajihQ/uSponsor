"""Incremental channel scraping via yt-dlp (no API keys).

Each scan lists the newest uploads per channel (one cheap flat request),
then fetches full metadata only for videos we haven't stored yet — so
repeat runs stay fast and light.
"""
import datetime as dt
import threading

from yt_dlp import YoutubeDL

from . import db
from .detector import brand_key, detect_sponsors

LOOKBACK_ENTRIES = 30       # how many newest uploads to list per channel
MAX_NEW_PER_SCAN = 12       # cap detail fetches per channel per scan

# Shared progress state for the web UI.
STATE = {
    "running": False,
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


def _ydl(extra=None):
    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    if extra:
        opts.update(extra)
    return YoutubeDL(opts)


def _list_uploads(channel_url):
    """One flat request: channel name/id + newest video entries."""
    url = channel_url.rstrip("/") + "/videos"
    with _ydl({"extract_flat": "in_playlist", "playlistend": LOOKBACK_ENTRIES}) as y:
        info = y.extract_info(url, download=False)
    entries = [e for e in (info.get("entries") or []) if e and e.get("id")]
    name = info.get("channel") or info.get("uploader") or info.get("title") or channel_url
    name = name.removesuffix(" - Videos")
    return info.get("channel_id") or info.get("id"), name, entries


def _fetch_video(video_id):
    with _ydl() as y:
        return y.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)


def scan_channel(conn, ch):
    """Scan one channel row; returns (new_videos, new_sponsorships)."""
    channel_id, name, entries = _list_uploads(ch["input_url"])
    conn.execute(
        "UPDATE channels SET channel_id = ?, name = ?, last_scanned = datetime('now') WHERE id = ?",
        (channel_id, name, ch["id"]),
    )
    conn.commit()

    known = {
        r["video_id"]
        for r in conn.execute("SELECT video_id FROM videos WHERE channel_ref = ?", (ch["id"],))
    }
    fresh = [e for e in entries if e["id"] not in known][:MAX_NEW_PER_SCAN]

    new_videos = new_spons = 0
    for entry in fresh:
        try:
            v = _fetch_video(entry["id"])
        except Exception as exc:  # video may be private/removed; keep going
            _log(f"  ! skipped {entry['id']}: {exc}")
            continue
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
            continue
        video_ref = cur.lastrowid
        new_videos += 1
        for brand, evidence in detect_sponsors(v.get("description")):
            conn.execute(
                "INSERT OR IGNORE INTO sponsorships (video_ref, brand, brand_key, evidence)"
                " VALUES (?, ?, ?, ?)",
                (video_ref, brand, brand_key(brand), evidence),
            )
            new_spons += 1
        conn.commit()
    return name, new_videos, new_spons


def run_scan():
    """Scan every channel in the DB. Safe to call from a background thread."""
    with _lock:
        if STATE["running"]:
            return
        STATE.update(running=True, done=0, total=0, current="", log=[], finished_at=None)
    conn = db.connect()
    try:
        channels = conn.execute("SELECT * FROM channels ORDER BY id").fetchall()
        with _lock:
            STATE["total"] = len(channels)
        for ch in channels:
            label = ch["name"] or ch["input_url"]
            with _lock:
                STATE["current"] = label
            try:
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


def start_scan_in_background():
    """Kick off a scan thread if one isn't already running. Returns started?"""
    with _lock:
        if STATE["running"]:
            return False
    threading.Thread(target=run_scan, daemon=True).start()
    return True
