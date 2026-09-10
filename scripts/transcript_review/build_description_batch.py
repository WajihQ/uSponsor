"""Phase 1 of the two-phase review: pull the next N `pending` queue rows and
dump their stored description + existing evidence — no transcript fetch, no
API cost. Claude reads this batch and, for each entry, either:
  - judges it directly from the description (real sponsor confirmed, or
    affiliate/gift-only and rejected) — no transcript needed, or
  - flags it "needs transcript" for genuine ambiguity.

Doesn't touch transcript_review_queue.csv status — that only flips to
'fetched'/'done' once a transcript is actually pulled (phase 2, via
fetch_transcript_batch.py's --ids mode) or a decision is applied directly
via apply_transcript_batch.py for the description-only cases.

Usage: python scripts/transcript_review/build_description_batch.py [batch_size] [bucket]
"""
import csv
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from tracker import db  # noqa: E402 -- follows USPONSOR_DB / TURSO_DATABASE_URL, not a hardcoded path

QUEUE_PATH = os.path.join(ROOT, "transcript_review_queue.csv")


def main():
    batch_size = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    bucket_filter = sys.argv[2] if len(sys.argv) > 2 else "all"

    with open(QUEUE_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    pending = [
        r for r in rows
        if r["status"] == "pending" and (bucket_filter == "all" or r["bucket"] == bucket_filter)
    ]
    if not pending:
        print("No pending rows left in this bucket.")
        return
    batch = pending[:batch_size]

    conn = db.connect()

    results = []
    for row in batch:
        db_id = row["video_db_id"]
        v = conn.execute(
            "SELECT title, description, upload_date FROM videos WHERE id = ?", (db_id,)
        ).fetchone()
        existing = conn.execute(
            "SELECT brand, brand_key, evidence FROM sponsorships WHERE video_ref = ?", (db_id,)
        ).fetchall()
        results.append({
            "video_db_id": db_id,
            "video_id": row["video_id"],
            "channel_name": row["channel_name"],
            "bucket": row["bucket"],
            "title": v["title"] if v else None,
            "upload_date": v["upload_date"] if v else None,
            "description": v["description"] if v else None,
            "current_sponsorships": [dict(e) for e in existing],
        })
    conn.close()

    ts = time.strftime("%Y%m%d-%H%M%S")
    out_path = os.path.join(ROOT, f"description_batch_{ts}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"Wrote {len(results)} entries (description-only, no transcript fetched) to {out_path}")


if __name__ == "__main__":
    main()
