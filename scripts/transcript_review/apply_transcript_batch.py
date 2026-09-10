"""Apply Claude's judged decisions from a transcript-review batch to the DB.
Backs up every row touched to a timestamped cleanup_backup_*.csv first
(matching the existing manual-cleanup convention), then:
  - erroneous_keys: mark brand_key erroneous (brands table) + delete its
    sponsorship rows globally (same two-step the Brands-tab/manual passes use)
  - consolidate: [{old_key, new_name}] -> db.consolidate_brand
  - add_sponsors: [{brand, evidence}] -> INSERT OR IGNORE onto video_db_id
  - delete_rows: [brand_key, ...] -> delete just THIS video's row for that
    key, without touching the brands table or other videos (for a key that's
    correct elsewhere but misattributed/editorial on this one video —
    row-level surgery per the documented "generic-word collision" failure mode)
  - rename_display: [{old_key, new_display}] -> cosmetic text-only fix (same
    brand_key, just cleans up display text like a stray trailing symbol) —
    NOT for renames that change the key; use consolidate for that
  - review_outcome: 'resolved' | 'recover' | None -> videos.review (only
    meaningful for bucket == review_queue)
Then marks the queue row 'done' with outcome/note/timestamp.

Usage: python scripts/transcript_review/apply_transcript_batch.py decisions.json
Decision object shape:
{
  "video_db_id": "100350", "bucket": "unclassified_brand",
  "erroneous_keys": ["untilaugust31"],
  "consolidate": [{"old_key": "bigidesign", "new_name": "Big Idea Design"}],
  "add_sponsors": [{"brand": "Aecooly", "evidence": "manual: recovered from description (Claude review)"}],
  "review_outcome": null,
  "note": "junk date-phrase capture; real sponsor Aecooly recovered, already known in CRM"
}
"""
import csv
import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from tracker import db  # noqa: E402
from tracker.detector import brand_key as bkey  # noqa: E402

QUEUE_PATH = os.path.join(ROOT, "transcript_review_queue.csv")


def main():
    decisions_path = sys.argv[1]
    with open(decisions_path, encoding="utf-8") as f:
        decisions = json.load(f)

    ts = time.strftime("%Y%m%d-%H%M%S")
    backup_path = os.path.join(ROOT, f"cleanup_backup_{ts}_transcript_review.csv")
    backup_rows = []

    conn = db.connect()
    try:
        for d in decisions:
            video_db_id = int(d["video_db_id"])

            for key in d.get("erroneous_keys", []):
                for r in conn.execute(
                    "SELECT s.*, v.video_id FROM sponsorships s JOIN videos v ON v.id=s.video_ref"
                    " WHERE s.brand_key = ?", (key,)
                ):
                    backup_rows.append(dict(r))
                conn.execute(
                    "INSERT INTO brands (brand_key, name, kind) VALUES (?, ?, 'erroneous')"
                    " ON CONFLICT(brand_key) DO UPDATE SET kind='erroneous'",
                    (key, key),
                )
                conn.execute("DELETE FROM sponsorships WHERE brand_key = ?", (key,))

            for c in d.get("consolidate", []):
                for r in conn.execute(
                    "SELECT s.*, v.video_id FROM sponsorships s JOIN videos v ON v.id=s.video_ref"
                    " WHERE s.brand_key = ?", (c["old_key"],)
                ):
                    backup_rows.append(dict(r))
                db.consolidate_brand(conn, c["old_key"], c["new_name"])

            for sp in d.get("add_sponsors", []):
                name = sp["brand"]
                key = bkey(name)
                conn.execute(
                    "INSERT OR IGNORE INTO sponsorships (video_ref, brand, brand_key, evidence)"
                    " VALUES (?, ?, ?, ?)",
                    (video_db_id, name, key, sp.get("evidence", "manual: recovered from description (Claude review)")),
                )

            for key in d.get("delete_rows", []):
                for r in conn.execute(
                    "SELECT s.*, v.video_id FROM sponsorships s JOIN videos v ON v.id=s.video_ref"
                    " WHERE s.brand_key = ? AND s.video_ref = ?", (key, video_db_id)
                ):
                    backup_rows.append(dict(r))
                conn.execute(
                    "DELETE FROM sponsorships WHERE brand_key = ? AND video_ref = ?", (key, video_db_id)
                )

            for rn in d.get("rename_display", []):
                conn.execute(
                    "UPDATE sponsorships SET brand = ? WHERE brand_key = ?",
                    (rn["new_display"], rn["old_key"]),
                )

            outcome = d.get("review_outcome")
            if outcome:
                conn.execute("UPDATE videos SET review = ? WHERE id = ?", (outcome, video_db_id))
            elif d.get("bucket") == "review_queue" and not d.get("add_sponsors"):
                # explicitly reviewed, no recoverable sponsor -> clear the queue
                conn.execute("UPDATE videos SET review = NULL WHERE id = ?", (video_db_id,))

            conn.commit()

        if backup_rows:
            with open(backup_path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(backup_rows[0].keys()))
                w.writeheader()
                w.writerows(backup_rows)
            print(f"Backed up {len(backup_rows)} row(s) to {backup_path}")
    finally:
        conn.close()

    # update queue checkpoint
    with open(QUEUE_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    fieldnames = list(rows[0].keys())
    by_id = {(str(d["video_db_id"]), d["bucket"]): d for d in decisions}
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    n_done = 0
    for r in rows:
        k = (r["video_db_id"], r["bucket"])
        if k in by_id:
            d = by_id[k]
            r["status"] = "done"
            r["reviewed_at"] = now
            r["outcome"] = (
                "erroneous" if d.get("erroneous_keys") else
                "consolidated" if d.get("consolidate") else
                "row_deleted" if d.get("delete_rows") else
                "renamed" if d.get("rename_display") else
                "recovered" if d.get("add_sponsors") else
                "leave"
            )
            r["note"] = d.get("note", "")
            n_done += 1
    with open(QUEUE_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    print(f"Applied {len(decisions)} decision(s); marked {n_done} queue row(s) done.")


if __name__ == "__main__":
    main()
