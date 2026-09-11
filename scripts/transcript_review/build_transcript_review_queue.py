"""One-off: build the resumable queue for the Aug-2026 full transcript review
pass (unclassified brand_key sponsorships + the sb pending-review queue).
Run once; re-running is safe (won't duplicate existing rows)."""
import csv
import os
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
QUEUE_PATH = os.path.join(ROOT, "transcript_review_queue.csv")

conn = sqlite3.connect(os.path.join(ROOT, "sponsors.db"))
conn.row_factory = sqlite3.Row

existing = set()
if os.path.exists(QUEUE_PATH):
    with open(QUEUE_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            existing.add((row["video_db_id"], row["bucket"]))

rows_out = []

for r in conn.execute(
    """
    SELECT DISTINCT v.id AS video_db_id, v.video_id, c.name AS channel_name
    FROM sponsorships s
    JOIN videos v ON v.id = s.video_ref
    JOIN channels c ON c.id = v.channel_ref
    WHERE s.brand_key NOT IN (SELECT brand_key FROM brands)
    ORDER BY v.upload_date DESC
    """
):
    key = (str(r["video_db_id"]), "unclassified_brand")
    if key in existing:
        continue
    rows_out.append({
        "video_db_id": r["video_db_id"],
        "video_id": r["video_id"],
        "channel_name": r["channel_name"],
        "bucket": "unclassified_brand",
        "status": "pending",
        "reviewed_at": "",
        "outcome": "",
        "note": "",
    })

for r in conn.execute(
    """
    SELECT v.id AS video_db_id, v.video_id, c.name AS channel_name
    FROM videos v
    JOIN channels c ON c.id = v.channel_ref
    WHERE v.review = 'pending'
    ORDER BY v.upload_date DESC
    """
):
    key = (str(r["video_db_id"]), "review_queue")
    if key in existing:
        continue
    rows_out.append({
        "video_db_id": r["video_db_id"],
        "video_id": r["video_id"],
        "channel_name": r["channel_name"],
        "bucket": "review_queue",
        "status": "pending",
        "reviewed_at": "",
        "outcome": "",
        "note": "",
    })

write_header = not os.path.exists(QUEUE_PATH)
with open(QUEUE_PATH, "a", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=[
        "video_db_id", "video_id", "channel_name", "bucket",
        "status", "reviewed_at", "outcome", "note",
    ])
    if write_header:
        w.writeheader()
    for row in rows_out:
        w.writerow(row)

print(f"Appended {len(rows_out)} new queue rows to {QUEUE_PATH}")
total = sum(1 for _ in open(QUEUE_PATH, encoding="utf-8")) - 1
print(f"Total queue rows: {total}")
