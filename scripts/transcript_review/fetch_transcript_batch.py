"""Phase 2 of the two-phase review: fetch actual transcripts via Supadata
(see supadata_transcript.py) for videos where the description alone wasn't
conclusive. SponsorBlock-segment slice when timestamps exist, keyword-window
scan of the full transcript otherwise. Writes a batch JSON for Claude to
read/judge, same shape as before. Read-only against sponsors.db.

Usage:
  python scripts/transcript_review/fetch_transcript_batch.py --ids 100350,98860,97978   # explicit list
    (from a description_batch_*.json phase-1 pass where these were flagged
    "needs transcript" — the normal path now)
  python scripts/transcript_review/fetch_transcript_batch.py 100 all                    # legacy mode:
    next N pending rows off the queue, no phase-1 pre-filter. Still useful
    for topping up review_queue-bucket videos, which usually need the
    transcript regardless since there's no sponsor name to judge from
    description alone in the first place.
"""
import json
import os
import re
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

import supadata_transcript as supadata  # noqa: E402

QUEUE_PATH = os.path.join(ROOT, "transcript_review_queue.csv")
KEYWORDS = re.compile(
    r"sponsor|brought to you by|partnered with|partnership with|paid promotion",
    re.I,
)


def _keyword_windows(full_text, pad_chars=200, max_windows=6):
    hits = list(KEYWORDS.finditer(full_text))
    windows = []
    for m in hits[:max_windows]:
        start = max(0, m.start() - pad_chars)
        end = min(len(full_text), m.end() + pad_chars)
        windows.append((start, end))
    windows.sort()
    merged = []
    for s, e in windows:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return " ... ".join(full_text[s:e] for s, e in merged)


def _segment_slice(chunks, segments, pad_ms=20000):
    windows = [(max(0, s * 1000 - pad_ms), e * 1000 + pad_ms) for s, e in segments]
    parts = [c["text"] for c in chunks if any(lo <= c["offset"] <= hi for lo, hi in windows)]
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _fetch_one(conn, row):
    vid = row["video_id"]
    db_id = row["video_db_id"]
    v = conn.execute(
        "SELECT title, description, upload_date, sb_sponsored, sb_segments FROM videos WHERE id = ?",
        (db_id,),
    ).fetchone()
    existing = conn.execute(
        "SELECT brand, brand_key, evidence FROM sponsorships WHERE video_ref = ?", (db_id,)
    ).fetchall()

    entry = {
        "video_db_id": db_id,
        "video_id": vid,
        "channel_name": row["channel_name"],
        "bucket": row["bucket"],
        "title": v["title"] if v else None,
        "upload_date": v["upload_date"] if v else None,
        "current_sponsorships": [dict(e) for e in existing],
        "transcript_excerpt": None,
        "fetch_error": None,
    }
    try:
        chunks = supadata.fetch_chunks(vid)
        segs = json.loads(v["sb_segments"] or "[]") if v and v["sb_sponsored"] else []
        if segs:
            entry["transcript_excerpt"] = _segment_slice(chunks, [tuple(s) for s in segs])
        else:
            full_text = re.sub(r"\s+", " ", " ".join(c["text"] for c in chunks)).strip()
            entry["transcript_excerpt"] = _keyword_windows(full_text) or full_text[:1500]
    except supadata.TranscriptUnavailable:
        entry["fetch_error"] = "no_transcript"
    except Exception as exc:
        entry["fetch_error"] = f"{type(exc).__name__}: {exc}"
    return entry


def _rows_from_ids(all_queue_rows, ids):
    by_id = {r["video_db_id"]: r for r in all_queue_rows}
    missing = [i for i in ids if i not in by_id]
    if missing:
        print(f"Warning: {len(missing)} id(s) not found in queue, skipping: {missing[:10]}")
    return [by_id[i] for i in ids if i in by_id]


def main():
    import csv

    if not supadata.configured():
        print("ERROR: Supadata not configured — fill in supadata_api.json first.")
        sys.exit(1)

    with open(QUEUE_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    fieldnames = list(rows[0].keys())

    if "--ids" in sys.argv:
        idx = sys.argv.index("--ids")
        ids = sys.argv[idx + 1].split(",")
        batch = _rows_from_ids(rows, ids)
    else:
        batch_size = int(sys.argv[1]) if len(sys.argv) > 1 else 30
        bucket_filter = sys.argv[2] if len(sys.argv) > 2 else "all"
        pending = [
            r for r in rows
            if r["status"] == "pending" and (bucket_filter == "all" or r["bucket"] == bucket_filter)
        ]
        batch = pending[:batch_size]

    if not batch:
        print("Nothing to fetch.")
        return

    # Supadata's rate limit is per-account, not per-connection, and
    # sqlite3 connections aren't thread-safe to share — give each worker
    # its own connection (they're all read-only lookups, cheap to open).
    import concurrent.futures as cf

    import threading

    workers = min(20, len(batch))
    results = [None] * len(batch)
    done = 0
    lock = threading.Lock()

    def _worker(i, row):
        conn = sqlite3.connect(os.path.join(ROOT, "sponsors.db"))
        conn.row_factory = sqlite3.Row
        try:
            return i, _fetch_one(conn, row)
        finally:
            conn.close()

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_worker, i, row) for i, row in enumerate(batch)]
        for fut in cf.as_completed(futures):
            i, entry = fut.result()
            results[i] = entry
            with lock:
                done += 1
                print(f"[{done}/{len(batch)}] {entry['video_id']} — {'OK' if entry['transcript_excerpt'] else entry['fetch_error']}")

    ts = time.strftime("%Y%m%d-%H%M%S")
    out_path = os.path.join(ROOT, f"transcript_review_batch_{ts}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    fetched_ids = {r["video_db_id"] for r in results}
    for r in rows:
        if r["video_db_id"] in fetched_ids:
            r["status"] = "fetched"
    with open(QUEUE_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    print(f"\nWrote {len(results)} entries to {out_path}")


if __name__ == "__main__":
    main()
