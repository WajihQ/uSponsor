"""SponsorBlock integration: know a video contains a paid segment even when
the description discloses nothing, and name the brand from auto-captions.

SponsorBlock (sponsor.ajay.app) is a free, community-maintained database of
in-video sponsor segment timestamps. No key needed.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request

API = "https://sponsor.ajay.app/api/skipSegments"
UA = {"User-Agent": "uSponsor/1.0 (local sponsorship tracker)"}


def fetch_segments(video_id, timeout=8):
    """Sponsor segments for a video -> [(start_s, end_s), ...]; [] if none.

    Raises on network trouble so callers can leave the video re-checkable.
    """
    url = API + "?" + urllib.parse.urlencode(
        {"videoID": video_id, "categories": '["sponsor"]'}
    )
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:  # SponsorBlock's "no segments known"
            return []
        raise
    return [
        (float(s["segment"][0]), float(s["segment"][1]))
        for s in data
        if s.get("category") == "sponsor" and len(s.get("segment", [])) == 2
    ]


def pick_caption_url(info):
    """From a yt-dlp info dict, the best English json3 caption URL (or None)."""
    for source in (info.get("subtitles") or {}, info.get("automatic_captions") or {}):
        for lang in ("en", "en-US", "en-GB", "en-orig"):
            for fmt in source.get(lang, []) or []:
                if fmt.get("ext") == "json3" and fmt.get("url"):
                    return fmt["url"]
    return None


def transcript_slice(caption_url, segments, pad=12, timeout=15):
    """Download json3 captions and return the text spoken inside the sponsor
    segments (padded a little on both sides)."""
    req = urllib.request.Request(caption_url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    windows = [(max(0, s - pad), e + pad) for s, e in segments]
    parts = []
    for ev in data.get("events", []) or []:
        t = ev.get("tStartMs", 0) / 1000.0
        if not any(lo <= t <= hi for lo, hi in windows):
            continue
        for seg in ev.get("segs", []) or []:
            parts.append(seg.get("utf8", ""))
    text = re.sub(r"\s+", " ", "".join(parts)).strip()
    return text
