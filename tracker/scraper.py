"""Incremental channel scraping: YouTube Data API v3 first, yt-dlp/cookies
as the automatic fallback (see tracker/youtube_api.py).

Two scan modes, both writing to the same database:

- **base** (the default): lists the newest uploads per channel (one cheap
  flat request), then fetches full metadata only for videos we haven't
  stored yet — repeat runs stay fast and light.
- **backfill**: lists the channel's *entire* uploads feed and walks it
  newest → oldest, fetching every unseen video until it reaches the
  cutoff (N years back). Slower by nature, so it sleeps between fetches
  to stay under YouTube's radar.

Every metadata fetch tries the Data API first (_fetch_video/_list_uploads;
scan_channel and stats_backfill_pass batch their lookups directly, up to 50
video ids per call) and only drops to yt-dlp when the API isn't configured,
a lookup fails, or the daily quota runs out — captions for spoken-sponsor
detection (segment_pass) have no practical API path, so that stays on
yt-dlp regardless.

A circuit breaker (Throttled / throttle_active / _trip_throttle) sits under
every yt-dlp call: once a response looks like real throttling, it stops
making further requests for a cooldown window instead of grinding through
the rest of the queue, and resumes on its own once the cooldown passes — no
manual restart needed. The Data API path has its own, separate cooldown
(youtube_api.QuotaExceeded) for when the daily quota runs out.
"""
import concurrent.futures as cf
import datetime as dt
import json
import os
import tempfile
import threading
import time

from yt_dlp import YoutubeDL

from . import db, sponsorblock, youtube_api
from .detector import brand_key, detect_sponsors, detect_spoken

LOOKBACK_ENTRIES = 30       # base scan: how many newest uploads to list per channel
MAX_NEW_PER_SCAN = 12       # base scan: cap detail fetches per channel per scan
SCAN_WORKERS = max(1, int(os.environ.get("USPONSOR_WORKERS", "4")))  # base-scan parallelism
BACKFILL_SLEEP = 1.5        # backfill: polite delay (seconds) between video fetches
BACKFILL_HARD_CAP = 600     # backfill: safety cap on fetches per channel per run
STATS_BACKFILL_LIMIT = int(os.environ.get("USPONSOR_STATS_BACKFILL_LIMIT", "150"))  # per-scan cap, yt-dlp fallback path
STATS_BACKFILL_LIMIT_API = int(os.environ.get("USPONSOR_STATS_BACKFILL_LIMIT_API", "2000"))  # per-scan cap via Data API
STATS_BACKFILL_SLEEP = 1.5  # polite delay (seconds) between yt-dlp stats-backfill fetches
STATS_REFRESH_STALE_HOURS = int(os.environ.get("USPONSOR_STATS_REFRESH_STALE_HOURS", "24"))  # matches the base scan's own freshness window
STATS_REFRESH_LIMIT = int(os.environ.get("USPONSOR_STATS_REFRESH_LIMIT", "50"))  # per-scan cap, yt-dlp fallback path
STATS_REFRESH_LIMIT_API = int(os.environ.get("USPONSOR_STATS_REFRESH_LIMIT_API", "500"))  # per-scan cap via Data API
SHORTS_MAX_SECONDS = 180    # YouTube's Shorts definition (extended to 3 min, Oct 2024)
DURATION_BACKFILL_LIMIT = int(os.environ.get("USPONSOR_DURATION_BACKFILL_LIMIT", "150"))  # per-scan cap, yt-dlp fallback path
DURATION_BACKFILL_LIMIT_API = int(os.environ.get("USPONSOR_DURATION_BACKFILL_LIMIT_API", "2000"))  # per-scan cap via Data API
SPONSORBLOCK_WORKERS = max(1, int(os.environ.get("USPONSOR_SPONSORBLOCK_WORKERS", "8")))  # sponsor.ajay.app is a plain HTTP API, not YouTube — safe to fan out

# Shared progress state for the web UI.
STATE = {
    "running": False,
    "mode": "base",
    "current": "",
    "done": 0,
    "total": 0,
    "log": [],
    "finished_at": None,
    "throttled_until": None,   # epoch seconds; set while a YouTube cooldown is active
}
_lock = threading.Lock()


def _log(msg):
    with _lock:
        STATE["log"].append(msg)
        STATE["log"][:] = STATE["log"][-200:]


def _set_current(label):
    with _lock:
        STATE["current"] = label


class Throttled(Exception):
    """Raised by _extract() instead of making a request while the circuit
    breaker (see _trip_throttle/throttle_active) is cooling down."""


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_throttle_until = 0.0  # epoch seconds; 0 = not currently throttled


def _load_throttle_state():
    """Pick up a cooldown a *previous process* already started — from the DB
    (app_config), so it survives an ephemeral host's redeploys, not just a
    previous process on the same machine. Without this, a fresh process
    forgets the block ever happened and immediately re-triggers it — exactly
    what kept extending the 2026-08 YouTube block across several back-to-back
    manual retries."""
    global _throttle_until
    try:
        conn = db.connect()
        try:
            raw = db.get_config(conn, "throttle_state")
        finally:
            conn.close()
        until = float(json.loads(raw).get("throttled_until") or 0) if raw else 0
        _throttle_until = max(_throttle_until, until)
    except Exception:
        pass  # e.g. a brand-new DB with no app_config table yet — fine, just start clean


def _save_throttle_state():
    try:
        conn = db.connect()
        try:
            db.set_config(conn, "throttle_state", json.dumps({"throttled_until": _throttle_until}))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


_load_throttle_state()


def throttle_active():
    """True if a prior YouTube block is still cooling down. Every fetch loop
    checks this (via _extract) instead of grinding through its remaining
    queue on a block that YouTube already told us isn't clearing yet."""
    global _throttle_until
    if _throttle_until and time.time() >= _throttle_until:
        _throttle_until = 0.0
        STATE["throttled_until"] = None
        _save_throttle_state()
    return time.time() < _throttle_until


def _trip_throttle(minutes, reason):
    """Start (or extend) a cooldown. Logs once per cooldown, not once per
    request, so a run doesn't drown its own log in duplicate warnings."""
    global _throttle_until
    was_active = throttle_active()
    _throttle_until = max(_throttle_until, time.time() + minutes * 60)
    STATE["throttled_until"] = _throttle_until
    _save_throttle_state()
    if not was_active:
        until = time.strftime("%H:%M", time.localtime(_throttle_until))
        _log(f"  ⚠ {reason} — pausing all YouTube requests until {until} (resumes automatically)")


_cookie_valid_cache = {}  # path -> (mtime, size, valid) — re-checked only when the file changes


def _cookiefile_valid(path):
    """Cheap sanity check on a Netscape-format cookies.txt.

    A bad re-upload or an unclean shutdown mid-write can leave cookies.txt
    truncated/null-padded (happened 2026-07-21 — 3.7KB of zero bytes); yt-dlp
    then hard-fails *every* request with a DownloadError instead of just
    ignoring the file. Checking the shape ourselves lets a corrupt file
    degrade to unauthenticated scanning (slower, but working) instead of
    breaking every scan until someone notices and re-uploads.
    """
    try:
        st = os.stat(path)
    except OSError:
        return False
    cached = _cookie_valid_cache.get(path)
    if cached and (cached[0], cached[1]) == (st.st_mtime, st.st_size):
        return cached[2]
    valid = False
    try:
        with open(path, "rb") as fh:
            head = fh.read(8192)
        if head and b"\x00" not in head:
            lines = [ln for ln in head.split(b"\n") if ln.strip() and not ln.lstrip().startswith(b"#")]
            valid = any(len(ln.split(b"\t")) >= 6 for ln in lines)
    except OSError:
        valid = False
    was_valid = cached[2] if cached else None
    _cookie_valid_cache[path] = (st.st_mtime, st.st_size, valid)
    if was_valid is not False and not valid:
        _log(f"  ! {os.path.basename(path)} doesn't look like a valid cookies file (corrupt or empty) —"
             f" scanning unauthenticated until it's replaced (Influencer CRM page → re-upload)")
    return valid


_DB_COOKIES_CACHE_PATH = os.path.join(tempfile.gettempdir(), "usponsor_cookies.txt")
_db_cookies_materialized_for = None  # the app_config value last written to _DB_COOKIES_CACHE_PATH


def _materialize_db_cookies():
    """yt-dlp needs a real file path (cookiefile), it can't take cookie
    content directly — so app_config['cookies_txt'] (the durable copy, set
    by POST /scan/cookies) gets written out to a local temp file once per
    process/whenever it changes, not once per video (that would mean a DB
    round trip per fetch). Returns the temp path, or None if nothing's
    stored in the DB either."""
    global _db_cookies_materialized_for
    try:
        conn = db.connect()
        try:
            raw = db.get_config(conn, "cookies_txt")
        finally:
            conn.close()
    except Exception:
        return None
    if not raw:
        return None
    if raw != _db_cookies_materialized_for:
        with open(_DB_COOKIES_CACHE_PATH, "w", encoding="utf-8", newline="\n") as f:
            f.write(raw)
        _db_cookies_materialized_for = raw
    return _DB_COOKIES_CACHE_PATH


def _cookiefile_path():
    """Which cookies.txt cookie_opts() would use, ignoring validity. Checks
    local files first (USPONSOR_COOKIES_FILE, then a project-root cookies.txt
    for local dev), then falls back to the DB-stored copy — the only source
    once hosted, where local disk doesn't survive a redeploy."""
    f = os.environ.get("USPONSOR_COOKIES_FILE", "").strip()
    if f and os.path.isfile(f):
        return f
    default = os.path.join(_ROOT, "cookies.txt")
    if os.path.isfile(default):
        return default
    return _materialize_db_cookies()


def cookie_opts():
    """yt-dlp cookie options for *authenticated* requests, which get far higher
    rate limits and dodge the "confirm you're not a bot" wall. In priority order:
    USPONSOR_COOKIES_FILE, a cookies.txt in the project root, then
    USPONSOR_COOKIES_BROWSER (e.g. 'chrome' / 'edge' / 'firefox'). A present
    but corrupt cookie file falls through to the next option (browser, then
    unauthenticated) rather than raised — see _cookiefile_valid."""
    path = _cookiefile_path()
    if path and _cookiefile_valid(path):
        return {"cookiefile": path}
    browser = os.environ.get("USPONSOR_COOKIES_BROWSER", "").strip()
    if browser:
        return {"cookiesfrombrowser": (browser,)}
    return {}


def cookies_active():
    """Human-readable description of the cookie source in use, or None if
    scanning runs unauthenticated (nothing configured, or see cookies_broken())."""
    path = _cookiefile_path()
    if path and _cookiefile_valid(path):
        return os.path.basename(path)
    browser = os.environ.get("USPONSOR_COOKIES_BROWSER", "").strip()
    if browser:
        return browser + " browser"
    return None


def cookies_broken():
    """Basename of a present-but-corrupt cookies.txt, or None."""
    path = _cookiefile_path()
    return os.path.basename(path) if path and not _cookiefile_valid(path) else None


def _ydl(extra=None):
    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    opts.update(cookie_opts())
    if extra:
        opts.update(extra)
    return YoutubeDL(opts)


_RATE_HINTS = ("sign in to confirm", "not a bot", "http error 429", "too many requests",
               "rate limit", "rate-limit", "temporarily", "http error 403")


def _extract(url, opts=None, tries=4, base_wait=20):
    """extract_info with retry+backoff on YouTube throttling / bot-check errors.

    Raises Throttled without attempting a request if the circuit breaker is
    already cooling down. On a hit, a message that names an explicit
    session-level block skips straight to a long cooldown (retrying it would
    just waste 4 tries for nothing); a generic throttle signal still gets
    the normal short backoff first, and only trips the breaker if that
    backoff doesn't resolve it either.
    """
    if throttle_active():
        until = time.strftime("%H:%M", time.localtime(_throttle_until))
        raise Throttled(f"cooling down until {until}")
    for attempt in range(tries):
        try:
            with _ydl(opts) as y:
                return y.extract_info(url, download=False)
        except Exception as exc:
            msg = str(exc).lower()
            if "rate-limited" in msg and "session" in msg:
                _trip_throttle(60, "YouTube reported a session-level rate limit")
                raise
            throttled = any(h in msg for h in _RATE_HINTS)
            if throttled and attempt < tries - 1:
                wait = base_wait * (2 ** attempt)
                _log(f"  … throttled by YouTube, waiting {wait}s (retry {attempt + 1}/{tries - 1})")
                time.sleep(wait)
                continue
            if throttled:
                _trip_throttle(20, "Repeated YouTube throttling")
            raise


def _list_uploads_ytdlp(channel_url, limit=LOOKBACK_ENTRIES):
    """One flat request: channel name/id + newest-first video entries.

    limit=None lists the entire uploads feed (used by backfill).
    """
    url = channel_url.rstrip("/") + "/videos"
    opts = {"extract_flat": "in_playlist"}
    if limit:
        opts["playlistend"] = limit
    info = _extract(url, opts)
    entries = [e for e in (info.get("entries") or []) if e and e.get("id")]
    name = info.get("channel") or info.get("uploader") or info.get("title") or channel_url
    name = name.removesuffix(" - Videos")
    return info.get("channel_id") or info.get("id"), name, entries, info.get("channel_follower_count")


def _list_uploads(channel_url, limit=LOOKBACK_ENTRIES, cutoff=None):
    """(channel_id, name, entries, subscriber_count) — Data API v3 first
    (cheap, quota-metered, untouched by YouTube's scraping throttle), yt-dlp
    fallback when it's not configured, the channel URL shape isn't a
    reliable API lookup key (e.g. /c/ vanity URLs), or the quota runs out.

    cutoff (a date, backfill only) stops the API listing once it reaches a
    video older than it, instead of walking a channel's entire lifetime
    history — the yt-dlp fallback has no cheap way to do this, so it's
    ignored there (pre-existing behavior, unchanged).
    """
    if youtube_api.available():
        try:
            return youtube_api.list_channel_videos(channel_url, limit=limit, cutoff=cutoff)
        except youtube_api.QuotaExceeded:
            _log("  ! YouTube Data API quota exhausted for today — falling back to cookie-based scraping")
        except Exception:
            pass  # channel not resolvable via API, or a transient hiccup
    return _list_uploads_ytdlp(channel_url, limit)


def _fetch_video_ytdlp(video_id):
    # player_skip: we only need metadata (title/date/description), so skip the
    # stream-resolution work — noticeably faster per video
    return _extract(f"https://www.youtube.com/watch?v={video_id}",
                    {"extractor_args": {"youtube": {"player_skip": ["js", "configs"]}}})


def _fetch_video(video_id):
    """Single-video metadata fetch: Data API v3 first, yt-dlp fallback.
    Callers that already have a batch of ids on hand (scan_channel,
    stats_backfill_pass) call youtube_api.videos_batch() directly instead —
    one API call for up to 50 videos beats one call each.
    """
    if youtube_api.available():
        try:
            return youtube_api.fetch_video(video_id)
        except youtube_api.QuotaExceeded:
            _log("  ! YouTube Data API quota exhausted for today — falling back to cookie-based scraping")
        except Exception:
            pass  # not found via API, or a transient hiccup — try yt-dlp
    return _fetch_video_ytdlp(video_id)


def _is_short(duration_seconds):
    """None (unknown duration) defaults to 0 — long-form is already the
    existing behavior for videos we can't classify, so unknowns don't get
    silently dropped out of the long-form stats."""
    return 1 if duration_seconds is not None and duration_seconds <= SHORTS_MAX_SECONDS else 0


def _store_video(conn, ch, v, known=(), aliases=None):
    """Insert a fetched video + its detected sponsorships.
    Returns (stored?, n_spons, date, row_id) — row_id is None when the video
    already existed. SponsorBlock isn't checked here: callers batch it across
    everything they just stored via _sponsorblock_check_batch (one parallel
    fan-out instead of one sequential HTTP call per video).
    """
    raw_date = v.get("upload_date")  # YYYYMMDD
    upload_date = (
        dt.datetime.strptime(raw_date, "%Y%m%d").date().isoformat() if raw_date else None
    )
    duration = v.get("duration")
    cur = conn.execute(
        "INSERT OR IGNORE INTO videos (video_id, channel_ref, title, url, upload_date, description,"
        " view_count, like_count, comment_count, duration_seconds, is_short, stats_checked_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
        (v["id"], ch["id"], v.get("title"), v.get("webpage_url"), upload_date, v.get("description"),
         v.get("view_count"), v.get("like_count"), v.get("comment_count"), duration, _is_short(duration)),
    )
    if not cur.rowcount:
        return False, 0, upload_date, None
    n = 0
    for brand, evidence in detect_sponsors(v.get("description"), known):
        brand, key = db.apply_alias(brand, aliases or {})
        conn.execute(
            "INSERT OR IGNORE INTO sponsorships (video_ref, brand, brand_key, evidence)"
            " VALUES (?, ?, ?, ?)",
            (cur.lastrowid, brand, key, evidence),
        )
        n += 1
    conn.commit()
    return True, n, upload_date, cur.lastrowid


def _sponsorblock_check_batch(video_ids):
    """{video_id: segs} for videos whose SponsorBlock lookup succeeded (segs
    is [] when no paid segment was found); a failed lookup is simply absent
    so the caller leaves it sb_checked=0 for a retry later. Looked up in
    parallel — sponsor.ajay.app is a plain community HTTP API, not YouTube,
    so it's untouched by the scraping throttle and cheap to fan out.
    """
    results = {}
    ids = list(dict.fromkeys(video_ids))
    if not ids:
        return results
    with cf.ThreadPoolExecutor(max_workers=min(SPONSORBLOCK_WORKERS, len(ids))) as pool:
        futures = {pool.submit(sponsorblock.fetch_segments, vid): vid for vid in ids}
        for fut in cf.as_completed(futures):
            vid = futures[fut]
            try:
                results[vid] = fut.result()
            except Exception:
                pass  # network hiccup — stays unchecked, retried next pass
    return results


def _apply_sponsorblock_results(conn, row_id_by_video, results):
    for video_id, segs in results.items():
        row_id = row_id_by_video.get(video_id)
        if row_id is None:
            continue
        conn.execute(
            "UPDATE videos SET sb_checked = 1, sb_sponsored = ?, sb_segments = ? WHERE id = ?",
            (1 if segs else 0, json.dumps(segs) if segs else None, row_id),
        )
    conn.commit()


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
    """Caption-track lookup via the android player client, skipping the
    webpage fetch entirely (player_skip webpage+configs). Captions come from
    the player response, not the HTML, so they're still present — but this
    avoids the actual watch-page request, which is where YouTube's "sign in
    to confirm you're not a bot" wall lives. Added 2026-08 after repeated
    web-client caption fetches (this call, run in a loop across a large
    backlog) tripped an escalating block. Not a permanent fix — still keep
    volume low and spread runs out; see _trip_throttle."""
    return _extract(
        f"https://www.youtube.com/watch?v={video_id}",
        {"extractor_args": {"youtube": {"player_client": ["android"], "player_skip": ["webpage", "configs"]}}},
    )


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
        row_id_by_video = {r["video_id"]: r["id"] for r in rows}
        sb_results = _sponsorblock_check_batch(list(row_id_by_video))
        _apply_sponsorblock_results(conn, row_id_by_video, sb_results)
        checked = len(sb_results)
        flagged = sum(1 for segs in sb_results.values() if segs)

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
            except Throttled:
                # stop naming for this run rather than mis-marking the rest
                # 'pending' (review=pending means "tried and couldn't tell")
                break
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


def stats_backfill_pass(limit=None):
    """Fill in view_count/like_count/comment_count for stored videos missing
    it — either backfilled before that data was captured (pre-2026-07-05),
    or any fetch that transiently returned it as None. Scan modes only ever
    fetch videos they haven't stored yet, so without this pass those NULLs
    are permanent; running a capped batch after every scan drains the
    backlog over time instead of needing a one-off repair script. Newest
    videos first, since that's the window creator-stats reads.

    Data API v3 first, batched up to 50 ids/call — cheap enough to use a
    much bigger cap than the yt-dlp fallback path, which stays paced and
    small since it shares the scraping throttle with everything else.

    Returns (filled, attempted).
    """
    if limit is None:
        limit = STATS_BACKFILL_LIMIT_API if youtube_api.available() else STATS_BACKFILL_LIMIT
    conn = db.connect()
    filled = 0
    try:
        rows = conn.execute(
            "SELECT id, video_id FROM videos WHERE view_count IS NULL"
            " ORDER BY upload_date DESC LIMIT ?",
            (limit,),
        ).fetchall()
        by_video_id = {r["video_id"]: r["id"] for r in rows}
        remaining = list(by_video_id)

        if youtube_api.available() and remaining:
            try:
                api_data = youtube_api.videos_batch(remaining)
            except youtube_api.QuotaExceeded:
                _log("  ! YouTube Data API quota exhausted for today — falling back to cookie-based scraping")
                api_data = {}
            except Exception as exc:
                _log(f"  ! YouTube Data API batch lookup failed ({exc}) — falling back to cookie-based scraping")
                api_data = {}
            for video_id, v in api_data.items():
                duration = v.get("duration")
                conn.execute(
                    "UPDATE videos SET view_count = ?, like_count = ?, comment_count = ?,"
                    " duration_seconds = ?, is_short = ?, stats_checked_at = datetime('now') WHERE id = ?",
                    (v.get("view_count"), v.get("like_count"), v.get("comment_count"),
                     duration, _is_short(duration), by_video_id[video_id]),
                )
                conn.commit()
                filled += 1
            remaining = [vid for vid in remaining if vid not in api_data]

        for video_id in remaining:  # not configured, quota-exhausted, or missing from the API result
            try:
                v = _fetch_video_ytdlp(video_id)
            except Throttled:
                break  # stop this pass; the rest are retried once the cooldown clears
            except Exception:
                continue  # private/removed — retried next pass too
            duration = v.get("duration")
            conn.execute(
                "UPDATE videos SET view_count = ?, like_count = ?, comment_count = ?,"
                " duration_seconds = ?, is_short = ?, stats_checked_at = datetime('now') WHERE id = ?",
                (v.get("view_count"), v.get("like_count"), v.get("comment_count"),
                 duration, _is_short(duration), by_video_id[video_id]),
            )
            conn.commit()
            filled += 1
            time.sleep(STATS_BACKFILL_SLEEP)
        if rows:
            _log(f"Stats backfill: filled in {filled}/{len(rows)} video(s) missing view counts")
        return filled, len(rows)
    finally:
        conn.close()


def stats_refresh_pass(limit_api=None, limit_ytdlp=None, stale_hours=None):
    """Re-fetch view/like/comment counts that have gone stale, unlike
    stats_backfill_pass above which only ever fills a NULL once. Nothing
    else in the pipeline updates a video's stats after it's first stored —
    scan_channel/backfill_channel skip anything already known — so a video
    sitting in a creator's average-views window would otherwise report
    whatever number it had the day it was first scanned, forever.

    Scoped to exactly what the displayed stats use: each channel's newest
    12 long-form + newest 12 Shorts with a positive view_count (the same
    window app._channel_stats / the Influencer CRM's avg_views already
    read) — no point refreshing a video nobody's average depends on. Same
    API-first/yt-dlp-fallback/paced/capped shape as stats_backfill_pass.

    Returns (refreshed, candidates).
    """
    limit_api = STATS_REFRESH_LIMIT_API if limit_api is None else limit_api
    limit_ytdlp = STATS_REFRESH_LIMIT if limit_ytdlp is None else limit_ytdlp
    stale_hours = STATS_REFRESH_STALE_HOURS if stale_hours is None else stale_hours
    conn = db.connect()
    refreshed = 0
    try:
        cutoff = (dt.datetime.now() - dt.timedelta(hours=stale_hours)).strftime("%Y-%m-%d %H:%M:%S")
        rows = conn.execute(
            """
            WITH windowed AS (
                SELECT id, video_id, stats_checked_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY channel_ref, COALESCE(is_short, 0)
                           ORDER BY upload_date DESC
                       ) AS rn
                FROM videos
                WHERE view_count IS NOT NULL AND view_count > 0
            )
            SELECT id, video_id FROM windowed
            WHERE rn <= 12 AND (stats_checked_at IS NULL OR stats_checked_at < ?)
            ORDER BY stats_checked_at IS NULL DESC, stats_checked_at ASC
            LIMIT ?
            """,
            (cutoff, limit_api + limit_ytdlp),
        ).fetchall()
        by_video_id = {r["video_id"]: r["id"] for r in rows}
        remaining = list(by_video_id)

        if youtube_api.available() and remaining:
            batch = remaining[:limit_api]
            try:
                api_data = youtube_api.videos_batch(batch)
            except youtube_api.QuotaExceeded:
                _log("  ! YouTube Data API quota exhausted for today — falling back to cookie-based scraping")
                api_data = {}
            except Exception as exc:
                _log(f"  ! YouTube Data API batch lookup failed ({exc}) — falling back to cookie-based scraping")
                api_data = {}
            for video_id, v in api_data.items():
                duration = v.get("duration")
                conn.execute(
                    "UPDATE videos SET view_count = ?, like_count = ?, comment_count = ?,"
                    " duration_seconds = ?, is_short = ?, stats_checked_at = datetime('now') WHERE id = ?",
                    (v.get("view_count"), v.get("like_count"), v.get("comment_count"),
                     duration, _is_short(duration), by_video_id[video_id]),
                )
                conn.commit()
                refreshed += 1
            remaining = [vid for vid in remaining if vid not in api_data]

        for video_id in remaining[:limit_ytdlp]:  # not configured, quota-exhausted, or missing from the batch
            try:
                v = _fetch_video_ytdlp(video_id)
            except Throttled:
                break  # stop this pass; the rest are retried once the cooldown clears
            except Exception:
                continue  # private/removed — retried next pass too
            duration = v.get("duration")
            conn.execute(
                "UPDATE videos SET view_count = ?, like_count = ?, comment_count = ?,"
                " duration_seconds = ?, is_short = ?, stats_checked_at = datetime('now') WHERE id = ?",
                (v.get("view_count"), v.get("like_count"), v.get("comment_count"),
                 duration, _is_short(duration), by_video_id[video_id]),
            )
            conn.commit()
            refreshed += 1
            time.sleep(STATS_BACKFILL_SLEEP)
        if rows:
            _log(f"Stats refresh: refreshed {refreshed}/{len(rows)} stale video(s) in creators' stats windows")
        return refreshed, len(rows)
    finally:
        conn.close()


def duration_backfill_pass(limit=None):
    """Classify Shorts vs. long-form for stored videos captured before Shorts
    tracking existed (2026-08-07) — those rows already have view_count filled
    (stats_backfill_pass wouldn't touch them), so is_short would otherwise
    stay NULL forever since scan modes never revisit an already-stored video.
    Mirrors stats_backfill_pass's drain-over-time approach.

    Returns (filled, attempted).
    """
    if limit is None:
        limit = DURATION_BACKFILL_LIMIT_API if youtube_api.available() else DURATION_BACKFILL_LIMIT
    conn = db.connect()
    filled = 0
    try:
        rows = conn.execute(
            "SELECT id, video_id FROM videos WHERE is_short IS NULL"
            " ORDER BY upload_date DESC LIMIT ?",
            (limit,),
        ).fetchall()
        by_video_id = {r["video_id"]: r["id"] for r in rows}
        remaining = list(by_video_id)

        if youtube_api.available() and remaining:
            try:
                api_data = youtube_api.videos_batch(remaining)
            except youtube_api.QuotaExceeded:
                _log("  ! YouTube Data API quota exhausted for today — falling back to cookie-based scraping")
                api_data = {}
            except Exception as exc:
                _log(f"  ! YouTube Data API batch lookup failed ({exc}) — falling back to cookie-based scraping")
                api_data = {}
            for video_id, v in api_data.items():
                duration = v.get("duration")
                conn.execute(
                    "UPDATE videos SET duration_seconds = ?, is_short = ? WHERE id = ?",
                    (duration, _is_short(duration), by_video_id[video_id]),
                )
                conn.commit()
                filled += 1
            remaining = [vid for vid in remaining if vid not in api_data]

        for video_id in remaining:  # not configured, quota-exhausted, or missing from the API result
            try:
                v = _fetch_video_ytdlp(video_id)
            except Throttled:
                break  # stop this pass; the rest are retried once the cooldown clears
            except Exception:
                continue  # private/removed — retried next pass too
            duration = v.get("duration")
            conn.execute(
                "UPDATE videos SET duration_seconds = ?, is_short = ? WHERE id = ?",
                (duration, _is_short(duration), by_video_id[video_id]),
            )
            conn.commit()
            filled += 1
            time.sleep(STATS_BACKFILL_SLEEP)
        if rows:
            _log(f"Shorts backfill: classified {filled}/{len(rows)} video(s)")
        return filled, len(rows)
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

    # one batched Data API call for the whole channel beats one yt-dlp call
    # per video; only videos it doesn't cover fall through to yt-dlp below
    api_data = {}
    if youtube_api.available() and fresh:
        try:
            api_data = youtube_api.videos_batch([e["id"] for e in fresh])
        except youtube_api.QuotaExceeded:
            _log("  ! YouTube Data API quota exhausted for today — falling back to cookie-based scraping")
        except Exception as exc:
            _log(f"  ! YouTube Data API batch lookup failed ({exc}) — falling back to cookie-based scraping")

    new_videos = new_spons = errors = 0
    throttled_early = False
    row_id_by_video = {}
    for entry in fresh:
        v = api_data.get(entry["id"])
        if v is None:
            try:
                v = _fetch_video_ytdlp(entry["id"])
            except Throttled:
                throttled_early = True  # cooldown just tripped — stop burning fetches on it
                break
            except Exception as exc:  # video may be private/removed
                _log(f"  ! skipped {entry['id']}: {exc}")
                errors += 1
                continue
        stored, n, _, row_id = _store_video(conn, ch, v, known, aliases)
        new_videos += stored
        new_spons += n
        if row_id is not None:
            row_id_by_video[entry["id"]] = row_id
    if row_id_by_video:
        results = _sponsorblock_check_batch(list(row_id_by_video))
        _apply_sponsorblock_results(conn, row_id_by_video, results)
    # had new videos to fetch but every fetch failed (rate-limited, or all private/removed)
    # -> don't count this as scanned, so the next run retries instead of skipping it for 24h
    if throttled_early or (fresh and errors == len(fresh)):
        conn.execute("UPDATE channels SET last_scanned = NULL WHERE id = ?", (ch["id"],))
        conn.commit()
        if throttled_early:
            _log(f"  ! {name}: paused by YouTube throttling — will resume next scan")
        else:
            _log(f"  ! {name}: all {errors} fetch(es) failed — will retry next scan")
    return name, new_videos, new_spons


def backfill_channel(conn, ch, cutoff):
    """Backfill one channel down to `cutoff` (a date); returns (name, new_videos, new_spons).

    The uploads feed is newest-first, so we stop at the first fetched video
    older than the cutoff. Already-stored videos are skipped without a fetch.

    Data API v3 first, batched up to 50 ids/call — it's quota-metered, not
    subject to yt-dlp's scraping throttle, so it isn't paced. BACKFILL_SLEEP
    only applies to whatever falls through to the yt-dlp fallback per video.
    """
    channel_id, name, entries, subs = _list_uploads(ch["input_url"], limit=None, cutoff=cutoff)
    _update_channel_meta(conn, ch, channel_id, name, subs)
    seen = _known_ids(conn, ch)
    known = db.known_brand_names(conn)
    aliases = db.alias_map(conn)

    # collect the run of not-yet-stored ids to fetch, newest-first, stopping
    # at the hard cap or at an already-stored video older than the cutoff
    # (a previous run already covered everything beyond that point)
    todo = []
    for entry in entries:
        if entry["id"] in seen:
            stored_date = seen[entry["id"]]
            if stored_date and stored_date < cutoff.isoformat():
                break
            continue
        todo.append(entry["id"])
        if len(todo) >= BACKFILL_HARD_CAP:
            break
    hit_hard_cap = len(todo) >= BACKFILL_HARD_CAP

    new_videos = new_spons = 0
    throttled_early = False
    for i in range(0, len(todo), 50):
        chunk = todo[i:i + 50]
        api_data = {}
        if youtube_api.available():
            try:
                api_data = youtube_api.videos_batch(chunk)
            except youtube_api.QuotaExceeded:
                _log("  ! YouTube Data API quota exhausted for today — falling back to cookie-based scraping")
            except Exception as exc:
                _log(f"  ! YouTube Data API batch lookup failed ({exc}) — falling back to cookie-based scraping")

        hit_cutoff = False
        row_id_by_video = {}
        for video_id in chunk:
            v = api_data.get(video_id)
            if v is None:
                try:
                    v = _fetch_video_ytdlp(video_id)
                except Throttled:
                    throttled_early = True
                    break
                except Exception as exc:
                    _log(f"  ! skipped {video_id}: {exc}")
                    continue
                time.sleep(BACKFILL_SLEEP)  # only the yt-dlp fallback needs pacing
            stored, n, upload_date, row_id = _store_video(conn, ch, v, known, aliases)
            new_videos += stored
            new_spons += n
            if row_id is not None:
                row_id_by_video[video_id] = row_id
            _set_current(f"{name} — {new_videos} video(s) so far ({upload_date or '?'})")
            if upload_date and upload_date < cutoff.isoformat():
                hit_cutoff = True
                break
        if row_id_by_video:
            results = _sponsorblock_check_batch(list(row_id_by_video))
            _apply_sponsorblock_results(conn, row_id_by_video, results)
        if throttled_early or hit_cutoff:
            break

    completed = True
    if throttled_early:
        # stop for this channel without marking it complete — a false
        # "complete" here would permanently skip the unfetched depth
        _log(f"  ! {name}: paused by YouTube throttling — will resume next backfill")
        completed = False
    elif hit_hard_cap:
        _log(f"  ! {name}: hit the {BACKFILL_HARD_CAP}-video safety cap — run backfill again to continue")
        completed = False
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
    if throttle_active():
        until = time.strftime("%H:%M", time.localtime(_throttle_until))
        _log(f"Scan skipped — YouTube cooldown active until {until} (resumes automatically)")
        with _lock:
            STATE["running"] = False
            STATE["current"] = ""
            STATE["finished_at"] = dt.datetime.now().strftime("%H:%M:%S")
        return
    cutoff = dt.date.today() - dt.timedelta(days=int(years) * 365)
    if mode == "backfill":
        _log(f"Backfill scan: going back {years} year(s), to {cutoff.isoformat()}")
    conn = db.connect()
    try:
        where, wargs = ["input_url LIKE '%youtube%'"], []   # skip Instagram-only entries
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
            "SELECT COUNT(*) FROM channels WHERE input_url LIKE '%youtube%'"
            + (" AND status = 'closed'" if target == "closed" else "")
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
            except Throttled:
                pass  # already logged once when the cooldown tripped
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
        try:
            stats_backfill_pass()
        except Exception as exc:
            _log(f"Stats backfill pass failed: {exc}")
        try:
            duration_backfill_pass()
        except Exception as exc:
            _log(f"Shorts backfill pass failed: {exc}")
        try:
            stats_refresh_pass()
        except Exception as exc:
            _log(f"Stats refresh pass failed: {exc}")
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
            segment_pass(check_limit=2000, caption_limit=600)
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
