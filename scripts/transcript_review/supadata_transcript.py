"""Supadata transcript client (api.supadata.ai) — replaces yt-dlp/cookies as
the caption source for the transcript-review pass. See
TRANSCRIPT_REVIEW_HANDOFF.md for why: yt-dlp's caption path shares YouTube's
bot-detection circuit breaker with everything else and trips unpredictably
(sometimes 100/100 clean, sometimes 1/100), costing hours of cooldown wait
per batch. Supadata is a paid, managed service (~$47/mo Mega plan covers the
whole backlog) that returns the same timestamped-chunk shape without any of
that risk on our end.

Needs a key: SUPADATA_API_KEY env var, or supadata_api.json
({"api_key": "..."}) in the project root — same pattern as youtube_api.json.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG = os.path.join(_ROOT, "supadata_api.json")
BASE = "https://api.supadata.ai/v1"

# Account is rate-limited to 50 req/s. Stay comfortably under it — this is a
# shared token bucket so concurrent fetch workers (see fetch_transcript_batch.py)
# collectively respect the limit rather than each assuming they have the
# whole budget to themselves.
_RATE_LIMIT = 35  # requests/sec, leaving headroom under the account's 50/s cap
_bucket_lock = threading.Lock()
_bucket_tokens = _RATE_LIMIT
_bucket_last = time.monotonic()


def _throttle():
    global _bucket_tokens, _bucket_last
    with _bucket_lock:
        now = time.monotonic()
        _bucket_tokens = min(_RATE_LIMIT, _bucket_tokens + (now - _bucket_last) * _RATE_LIMIT)
        _bucket_last = now
        if _bucket_tokens < 1:
            wait = (1 - _bucket_tokens) / _RATE_LIMIT
        else:
            wait = 0
        _bucket_tokens -= 1
    if wait > 0:
        time.sleep(wait)


class TranscriptUnavailable(Exception):
    """No transcript exists for this video (native or AI-generated)."""


def api_key():
    key = os.environ.get("SUPADATA_API_KEY", "").strip()
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


def fetch_chunks(video_id, timeout=30):
    """[{"text", "offset" (ms), "duration" (ms), "lang"}, ...] for a video,
    newest-caption-first as returned by Supadata. Raises TranscriptUnavailable
    if the video genuinely has no transcript (native or AI-generated);
    raises the underlying error otherwise (network/auth/etc — caller decides
    whether to retry)."""
    key = api_key()
    if not key:
        raise RuntimeError("No Supadata API key configured (supadata_api.json)")
    url = BASE + "/transcript?" + urllib.parse.urlencode({
        "url": f"https://youtu.be/{video_id}",
        "text": "false",
        "mode": "auto",
    })
    req = urllib.request.Request(url, headers={"x-api-key": key})
    _throttle()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.load(r)
    except urllib.error.HTTPError as exc:
        try:
            err = json.loads(exc.read().decode("utf-8", "replace"))
        except (json.JSONDecodeError, ValueError):
            err = {}
        if err.get("error") == "transcript-unavailable" or exc.code == 404:
            raise TranscriptUnavailable(video_id) from exc
        raise
    if "jobId" in body:
        # video was large enough to need async processing — poll for it
        return _poll_job(body["jobId"], key, timeout)
    return body["content"]


def _poll_job(job_id, key, timeout, tries=10, wait=3):
    import time
    for _ in range(tries):
        time.sleep(wait)
        req = urllib.request.Request(
            f"{BASE}/transcript/{job_id}", headers={"x-api-key": key}
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.load(r)
        if body.get("status") == "completed":
            return body["content"]
        if body.get("status") == "failed":
            raise TranscriptUnavailable(job_id)
    raise TimeoutError(f"Supadata job {job_id} didn't finish in time")
