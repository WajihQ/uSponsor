"""YouTube Data API v3 — free-tier (10,000 quota units/day by default)
metadata lookups. This is the *primary* source for video stats and channel
upload listings; tracker/scraper.py falls back to yt-dlp/cookies whenever
this isn't configured, a lookup fails, or the daily quota runs out. Unlike
yt-dlp scraping, this is an official, quota-metered API — not subject to
YouTube's bot-detection throttling (see scraper.py's Throttled breaker).

Needs a key: YOUTUBE_API_KEY env var, or youtube_api.json ({"api_key": "..."})
in the project root. Restrict the key to "YouTube Data API v3" only in
Google Cloud Console (Credentials -> API restrictions).

What it can't do: fetch auto-generated caption text (no practical
unauthenticated/API-key path for that), so segment_pass()'s spoken-sponsor
detection stays on yt-dlp regardless of whether this is configured.
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import db

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(_ROOT, "youtube_api.json")
BASE = "https://www.googleapis.com/youtube/v3"
UA = {"User-Agent": "uSponsor/1.0 (local sponsorship tracker)"}

QUOTA_COOLDOWN_HOURS = 6  # how long to stand down after a quotaExceeded error
_quota_exhausted_until = 0.0  # epoch seconds; 0 = not on cooldown


class QuotaExceeded(Exception):
    pass


def _load_quota_cooldown():
    """Pick up a cooldown a *previous process* already started — from the DB
    (app_config), matching tracker/scraper.py's throttle-state pattern, so a
    quota cooldown survives an ephemeral host's redeploys too, not just a
    restart on the same machine."""
    global _quota_exhausted_until
    try:
        conn = db.connect()
        try:
            raw = db.get_config(conn, "youtube_quota_exhausted_until")
        finally:
            conn.close()
        if raw:
            _quota_exhausted_until = max(_quota_exhausted_until, float(raw))
    except Exception:
        pass  # e.g. a brand-new DB with no app_config table yet — fine, just start clean


def _save_quota_cooldown():
    try:
        conn = db.connect()
        try:
            db.set_config(conn, "youtube_quota_exhausted_until", str(_quota_exhausted_until))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


_load_quota_cooldown()


def api_key():
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if key:
        return key
    if os.path.isfile(CONFIG):
        try:
            with open(CONFIG, encoding="utf-8") as f:
                return (json.load(f).get("api_key") or "").strip()
        except (OSError, json.JSONDecodeError):
            return ""
    return ""


def configured():
    return bool(api_key())


def quota_exhausted_until():
    """Epoch seconds the current quota cooldown ends, or None."""
    global _quota_exhausted_until
    if _quota_exhausted_until and time.time() >= _quota_exhausted_until:
        _quota_exhausted_until = 0.0
        _save_quota_cooldown()
    return _quota_exhausted_until or None


def available():
    """False if not configured, or a recent quotaExceeded put us on cooldown."""
    return configured() and quota_exhausted_until() is None


def _get(path, params, tries=3):
    key = api_key()
    if not key:
        raise RuntimeError("No YouTube Data API key configured")
    url = f"{BASE}/{path}?{urllib.parse.urlencode(dict(params, key=key))}"
    delay = 1.0
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.load(r)
        except urllib.error.HTTPError as exc:
            reason = ""
            try:
                body = json.loads(exc.read().decode("utf-8", "replace"))
                reason = body.get("error", {}).get("errors", [{}])[0].get("reason", "")
            except (json.JSONDecodeError, IndexError, AttributeError, ValueError):
                pass
            if reason in ("quotaExceeded", "dailyLimitExceeded"):
                global _quota_exhausted_until
                _quota_exhausted_until = time.time() + QUOTA_COOLDOWN_HOURS * 3600
                _save_quota_cooldown()
                raise QuotaExceeded(reason) from exc
            if exc.code in (429, 500, 502, 503) and attempt < tries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 20)
                continue
            raise
        except urllib.error.URLError:
            if attempt < tries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 20)
                continue
            raise


_ISO8601_DURATION_RE = re.compile(
    r"^PT(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?$"
)


def _parse_duration(iso):
    """ISO 8601 duration ('PT1M30S') -> whole seconds, or None if unparseable
    (e.g. a livestream still in progress reports no duration)."""
    m = _ISO8601_DURATION_RE.match(iso or "")
    if not m or not any(m.groups()):
        return None
    h, mi, s = (int(g) if g else 0 for g in m.groups())
    return h * 3600 + mi * 60 + s


def _to_ytdlp_shape(item):
    """Reshape a Data API `videos` item into the same keys scraper._store_video
    already expects from yt-dlp's extract_info, so callers don't need to care
    which source filled them in."""
    snip = item.get("snippet", {}) or {}
    stats = item.get("statistics", {}) or {}
    content = item.get("contentDetails", {}) or {}
    published = snip.get("publishedAt", "") or ""  # "2026-07-20T14:00:00Z"
    upload_date = published[:10].replace("-", "") if published else None
    return {
        "id": item["id"],
        "title": snip.get("title"),
        "webpage_url": f"https://www.youtube.com/watch?v={item['id']}",
        "upload_date": upload_date,
        "description": snip.get("description"),
        "view_count": int(stats["viewCount"]) if "viewCount" in stats else None,
        "like_count": int(stats["likeCount"]) if "likeCount" in stats else None,
        "comment_count": int(stats["commentCount"]) if "commentCount" in stats else None,
        "duration": _parse_duration(content.get("duration")),
    }


def videos_batch(video_ids):
    """{video_id: info-dict} for as many of `video_ids` as the API returns
    (up to 50 per underlying call, chunked automatically). A private/deleted
    video is simply absent from the result — callers already treat "no data"
    as a per-video failure and know to fall back for it.
    """
    out = {}
    ids = list(dict.fromkeys(video_ids))  # de-dupe, preserve order
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        data = _get("videos", {"id": ",".join(chunk), "part": "snippet,statistics,contentDetails"})
        for item in data.get("items", []):
            out[item["id"]] = _to_ytdlp_shape(item)
    return out


def fetch_video(video_id):
    """Single-video convenience wrapper. Raises LookupError if the API
    didn't return it (private/deleted/etc — caller falls back to yt-dlp)."""
    result = videos_batch([video_id])
    if video_id not in result:
        raise LookupError(f"{video_id}: not returned by the Data API")
    return result[video_id]


def _channel_lookup_params(channel_url):
    """Best-effort id/handle/username param for channels.list from a stored
    channel URL (see db.normalize_channel_url for the shapes we store).
    Raises LookupError for shapes with no reliable API key (e.g. /c/<name>
    vanity URLs) rather than guessing — a wrong guess could silently match
    an unrelated channel that happens to share the slug as its @handle.
    """
    url = channel_url.rstrip("/")
    if "/channel/" in url:
        return {"id": url.rsplit("/channel/", 1)[1]}
    if "/@" in url:
        return {"forHandle": "@" + url.rsplit("/@", 1)[1]}
    if "/user/" in url:
        return {"forUsername": url.rsplit("/user/", 1)[1]}
    raise LookupError(f"no reliable Data API lookup for {channel_url}")


def list_channel_videos(channel_url, limit=None, cutoff=None):
    """(channel_id, name, entries, subscriber_count) — same shape as
    scraper._list_uploads. entries are newest-first [{"id": video_id}, ...].
    limit=None walks the entire uploads playlist (backfill).

    cutoff (a date) stops pagination once a video published before it is
    seen, instead of always walking a channel's whole lifetime history — a
    backfill only needs videos back to a cutoff, and a long-running channel
    can have thousands of uploads. playlistItems' contentDetails includes
    videoPublishedAt, so this needs no extra calls. The boundary video
    itself is still included, matching how backfill_channel already stops
    at "the first video older than cutoff" using its real upload date.
    """
    params = dict(_channel_lookup_params(channel_url), part="snippet,statistics,contentDetails")
    data = _get("channels", params)
    items = data.get("items", [])
    if not items:
        raise LookupError(f"channel not found via Data API: {channel_url}")
    ch = items[0]
    channel_id = ch["id"]
    name = (ch.get("snippet", {}) or {}).get("title") or channel_url
    subs = (ch.get("statistics", {}) or {}).get("subscriberCount")
    subs = int(subs) if subs is not None else None
    uploads_playlist = (ch.get("contentDetails", {}) or {}).get("relatedPlaylists", {}).get("uploads")

    cutoff_str = cutoff.isoformat() if cutoff else None
    entries = []
    page_token = None
    while uploads_playlist:
        page_size = min(50, limit - len(entries)) if limit else 50
        if limit and page_size <= 0:
            break
        params = {"playlistId": uploads_playlist, "part": "contentDetails", "maxResults": page_size}
        if page_token:
            params["pageToken"] = page_token
        resp = _get("playlistItems", params)
        hit_cutoff = False
        for it in resp.get("items", []):
            cd = it.get("contentDetails", {}) or {}
            vid = cd.get("videoId")
            if not vid:
                continue
            entries.append({"id": vid})
            published = cd.get("videoPublishedAt", "") or ""
            if cutoff_str and published[:10] < cutoff_str:
                hit_cutoff = True
                break  # newest-first playlist: everything after this is older too
        page_token = resp.get("nextPageToken")
        if hit_cutoff or not page_token or (limit and len(entries) >= limit):
            break
    return channel_id, name, entries, subs
