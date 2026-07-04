"""Headless scan — for cron / Task Scheduler, no browser needed.

Usage:  python scan.py [channels.txt]
An optional file argument imports channels before scanning.
"""
import sys

from tracker import db, scraper

if __name__ == "__main__":
    db.init_db()
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8") as f:
            added, skipped = db.import_channel_lines(f.read())
        print(f"Imported {len(added)} channel(s), skipped {len(skipped)}.")
    scraper.run_scan()
    for line in scraper.STATE["log"]:
        print(line)
