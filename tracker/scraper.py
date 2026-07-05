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
import concurrent.futures as cf
import datetime as dt
import json
import os
import threading
import time

from yt_dlp import YoutubeDL

from . import db, sponsorblock
from .detector import brand_key, detect_sponsors, detect_spoken

LOOKBACK_ENTRIES = 30       # base scan: how many newest uploads to list per channel
MAX_NEW_PER_SCAN = 12       # base scan: cap detail fetches per channel per scan
SCAN_WORKERS = max(1, int(os.environ.get("USPONSOR_WORKERS", "4")))  # base-scan parallelism
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
    return info.get("channel_id") or info.get("id"), name, entries, info.get("channel_follower_count")


def _fetch_video(video_id):
    # player_skip: we only need metadata (title/date/description), so skip the
    # stream-resolution work — noticeably faster per video
    with _ydl({"extractor_args": {"youtube": {"player_skip": ["js", "configs"]}}}) as y:
        return y.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)


def _store_video(conn, ch, v, known=(), aliases=None):
    """Insert a fetched video + its detected sponsorships. Returns (stored?, n_spons, date)."""
    raw_date = v.get("upload_date")  # YYYYMMDD
    upload_date = (
        dt.datetime.strptime(raw_date, "%Y%m%d").date().isoformat() if raw_date else None
    )
    cur = conn.execute(
        "INSERT OR IGNORE INTO videos (video_id, channel_ref, title, url, upload_date, description,"
        " view_count, like_count, comment_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (v["id"], ch["id"], v.get("title"), v.get("webpage_url"), upload_date, v.get("description"),
         v.get("view_count"), v.get("like_count"), v.get("comment_count")),
    )
    if not cur.rowcount:
        return False, 0, upload_date
    n = 0
    for brand, evidence in detect_sponsors(v.get("description"), known):
        brand, key = db.apply_alias(brand, aliases or {})
        conn.execute(
            "INSERT OR IGNORE INTO sponsorships (video_ref, brand, brand_key, evidence)"
            " VALUES (?, ?, ?, ?)",
            (cur.lastrowid, brand, key, evidence),
        )
        n += 1
    # cheap SponsorBlock lookup: does this video contain a paid segment?
    try:
        segs = sponsorblock.fetch_segments(v["id"])
        conn.execute(
            "UPDATE videos SET sb_checked = 1, sb_sponsored = ?, sb_segments = ? WHERE id = ?",
            (1 if segs else 0, json.dumps(segs) if segs else None, cur.lastrowid),
        )
    except Exception:
        pass  # stays sb_checked=0; the post-scan pass retries it
    conn.commit()
    return True, n, upload_date


def rerun_detection():
    """Re-apply the current detector (+ known brands) to stored descriptions.

    Purely offline — no YouTube requests. Only adds sponsorships that
    weren't already recorded. Returns (videos_checked, new_sponsorships).
    """
    conn = db.connect()
    try:
        known = db.known_brand_names(conn)
        aliases = db.alias_map(conn)
        videos = conn.execute(
            "SELECT id, description FROM videos WHERE description IS NOT NULL AND description != ''"
        ).fetchall()
        new = 0
        for v in videos:
            for brand, evidence in detect_sponsors(v["description"], known):
                brand, key = db.apply_alias(brand, aliases)
                cur = conn.execute(
                    "INSERT OR IGNORE INTO sponsorships (video_ref, brand, brand_key, evidence)"
                    " VALUES (?, ?, ?, ?)",
                    (v["id"], brand, key, evidence),
                )
                new += cur.rowcount
        conn.commit()
        return len(videos), new
    finally:
        conn.close()


def _fetch_captions_info(video_id):
    """Full extract (no player_skip) so caption tracks are present."""
    with _ydl() as y:
        return y.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)


def segment_pass(check_limit=300, caption_limit=40):
    """Post-scan SponsorBlock pass. Returns (checked, flagged, named, pending).

    1. Query SponsorBlock for stored videos not yet checked (newest first,
       capped per run so a big backlog drains across scans).
    2. For videos WITH a sponsor segment but NO detected brand, pull the
       auto-captions and run detection on the spoken sponsor read. Whatever
       can't be auto-named lands in the review queue ('pending').
    """
    conn = db.connect()
    checked = flagged = named = pending = 0
    try:
        rows = conn.execute(
            "SELECT id, video_id FROM videos WHERE sb_checked = 0"
            " ORDER BY upload_date DESC LIMIT ?",
            (check_limit,),
        ).fetchall()
        for r in rows:
            try:
                segs = sponsorblock.fetch_segments(r["video_id"])
            except Exception:
                continue  # network hiccup: stays unchecked, retried next pass
            checked += 1
            flagged += bool(segs)
            conn.execute(
                "UPDATE videos SET sb_checked = 1, sb_sponsored = ?, sb_segments = ? WHERE id = ?",
                (1 if segs else 0, json.dumps(segs) if segs else None, r["id"]),
            )
        conn.commit()

        known = db.known_brand_names(conn)
        aliases = db.alias_map(conn)
        todo = conn.execute(
            "SELECT v.id, v.video_id, v.sb_segments FROM videos v"
            " WHERE v.sb_sponsored = 1 AND v.review IS NULL"
            " AND NOT EXISTS (SELECT 1 FROM sponsorships s WHERE s.video_ref = v.id)"
            " ORDER BY v.upload_date DESC LIMIT ?",
            (caption_limit,),
        ).fetchall()
        for v in todo:
            _set_current(f"naming sponsor segments ({named + pending + 1}/{len(todo)})")
            segs = [tuple(s) for s in json.loads(v["sb_segments"] or "[]")]
            text = ""
            try:
                info = _fetch_captions_info(v["video_id"])
                cap_url = sponsorblock.pick_caption_url(info)
                if cap_url:
                    text = sponsorblock.transcript_slice(cap_url, segs)
            except Exception as exc:
                _log(f"  ! captions failed for {v['video_id']}: {exc}")
            brands = detect_spoken(text, known) if text else []
            if brands:
                for brand, evidence in brands:
                    brand, key = db.apply_alias(brand, aliases)
                    conn.execute(
                        "INSERT OR IGNORE INTO sponsorships (video_ref, brand, brand_key, evidence)"
                        " VALUES (?, ?, ?, ?)",
                        (v["id"], brand, key, "spoken: " + evidence),
                    )
                conn.execute("UPDATE videos SET review = 'resolved' WHERE id = ?", (v["id"],))
                named += 1
            else:
                conn.execute(
                    "UPDATE videos SET review = 'pending', review_note = ? WHERE id = ?",
                    (text[:200] or None, v["id"]),
                )
                pending += 1
            conn.commit()
            time.sleep(0.5)
        if checked or todo:
            _log(
                f"Sponsor segments: {checked} video(s) checked, {flagged} with paid segments,"
                f" {named} auto-named from captions, {pending} sent to review"
            )
        return checked, flagged, named, pending
    finally:
        conn.close()


def _update_channel_meta(conn, ch, channel_id, name, subscribers=None):
    conn.execute(
        "UPDATE channels SET channel_id = ?, name = ?, last_scanned = datetime('now'),"
        " subscribers = COALESCE(?, subscribers) WHERE id = ?",
        (channel_id, name, subscribers, ch["id"]),
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
    channel_id, name, entries, subs = _list_uploads(ch["input_url"])
    _update_channel_meta(conn, ch, channel_id, name, subs)
    seen = _known_ids(conn, ch)
    fresh = [e for e in entries if e["id"] not in seen][:MAX_NEW_PER_SCAN]
    known = db.known_brand_names(conn)
    aliases = db.alias_map(conn)

    new_videos = new_spons = 0
    for entry in fresh:
        try:
            v = _fetch_video(entry["id"])
        except Exception as exc:  # video may be private/removed; keep going
            _log(f"  ! skipped {entry['id']}: {exc}")
            continue
        stored, n, _ = _store_video(conn, ch, v, known, aliases)
        new_videos += stored
        new_spons += n
    return name, new_videos, new_spons


def backfill_channel(conn, ch, cutoff):
    """Backfill one channel down to `cutoff` (a date); returns (name, new_videos, new_spons).

    The uploads feed is newest-first, so we stop at the first fetched video
    older than the cutoff. Already-stored videos are skipped without a fetch.
    """
    channel_id, name, entries, subs = _list_uploads(ch["input_url"], limit=None)
    _update_channel_meta(conn, ch, channel_id, name, subs)
    seen = _known_ids(conn, ch)
    known = db.known_brand_names(conn)
    aliases = db.alias_map(conn)

    new_videos = new_spons = fetched = 0
    completed = True
    for entry in entries:
        if entry["id"] in seen:
            stored_date = seen[entry["id"]]
            if stored_date and stored_date < cutoff.isoformat():
                break  # already walked past the cutoff on a previous run
            continue
        if fetched >= BACKFILL_HARD_CAP:
            _log(f"  ! {name}: hit the {BACKFILL_HARD_CAP}-video safety cap — run backfill again to continue")
            completed = False
            break
        try:
            v = _fetch_video(entry["id"])
        except Exception as exc:
            _log(f"  ! skipped {entry['id']}: {exc}")
            continue
        fetched += 1
        stored, n, upload_date = _store_video(conn, ch, v, known, aliases)
        new_videos += stored
        new_spons += n
        _set_current(f"{name} — {new_videos} video(s) so far ({upload_date or '?'})")
        if upload_date and upload_date < cutoff.isoformat():
            break  # reached the cutoff; everything older is out of range
        time.sleep(BACKFILL_SLEEP)
    if completed:
        # remember the covered depth so later backfills skip this channel
        # entirely (keep the deepest coverage if one already exists)
        conn.execute(
            "UPDATE channels SET backfilled_to = MIN(COALESCE(backfilled_to, ?), ?) WHERE id = ?",
            (cutoff.isoformat(), cutoff.isoformat(), ch["id"]),
        )
        conn.commit()
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
        where, wargs = [], []
        if target == "closed":
            where.append("status = 'closed'")
        if mode == "base" and not force:
            where.append("(last_scanned IS NULL OR last_scanned <= datetime('now', '-24 hours'))")
        if mode == "backfill":
            # skip channels a completed backfill already covered to this depth
            where.append("(backfilled_to IS NULL OR backfilled_to > ?)")
            wargs.append(cutoff.isoformat())
        sql = "SELECT * FROM channels"
        if where:
            sql += " WHERE " + " AND ".join(where)
        channels = conn.execute(sql + " ORDER BY id", wargs).fetchall()
        total_all = conn.execute(
            "SELECT COUNT(*) FROM channels" + (" WHERE status = 'closed'" if target == "closed" else "")
        ).fetchone()[0]
        skipped = total_all - len(channels)
        if skipped and mode == "base":
            _log(f"Skipping {skipped} channel(s) scanned within the last 24 hours")
        elif skipped:
            _log(f"Skipping {skipped} channel(s) already backfilled to {cutoff.isoformat()} or deeper")
        if not channels:
            _log("Nothing to scan — all targeted channels are already covered.")
        with _lock:
            STATE["total"] = len(channels)

        def work(ch, wconn=None):
            """Scan one channel with its own DB connection (thread-safe)."""
            label = ch["name"] or ch["input_url"]
            own = wconn or db.connect()
            try:
                if mode == "backfill":
                    name, nv, ns = backfill_channel(own, ch, cutoff)
                else:
                    name, nv, ns = scan_channel(own, ch)
                _log(f"{name}: {nv} new video(s), {ns} sponsorship(s)")
            except Exception as exc:
                _log(f"{label}: FAILED — {exc}")
            finally:
                if own is not wconn:
                    own.close()
            with _lock:
                STATE["done"] += 1
                STATE["current"] = f"{STATE['done']}/{STATE['total']} channels"

        if mode == "backfill":
            # sequential + polite delays: deep history is where rate limits bite
            for ch in channels:
                _set_current(ch["name"] or ch["input_url"])
                work(ch, conn)
        else:
            # base scans are shallow, so a few channels in parallel is safe
            # and cuts wall time roughly by the worker count
            with cf.ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
                list(pool.map(work, channels))
        try:
            segment_pass()
        except Exception as exc:
            _log(f"Sponsor-segment pass failed: {exc}")
    finally:
        conn.close()
        with _lock:
            STATE["running"] = False
            STATE["current"] = ""
            STATE["finished_at"] = dt.datetime.now().strftime("%H:%M:%S")


def start_segment_pass_in_background():
    """Run a standalone sponsor-segment pass (bigger caps than post-scan)."""
    with _lock:
        if STATE["running"]:
            return False
        STATE.update(running=True, mode="segments", done=0, total=0, current="", log=[], finished_at=None)

    def go():
        try:
            segment_pass(check_limit=1000, caption_limit=100)
        except Exception as exc:
            _log(f"Sponsor-segment pass failed: {exc}")
        finally:
            with _lock:
                STATE["running"] = False
                STATE["current"] = ""
                STATE["finished_at"] = dt.datetime.now().strftime("%H:%M:%S")

    threading.Thread(target=go, daemon=True).start()
    return True


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
