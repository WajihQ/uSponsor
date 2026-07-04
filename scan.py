"""Headless scan — for cron / Task Scheduler, no browser needed.

Usage:
  python scan.py                     # base scan of all channels
  python scan.py channels.txt        # import channels from a file, then scan
  python scan.py --backfill 2        # backfill scan: go back 2 years
  python scan.py --backfill 2 channels.txt
"""
import sys

from tracker import db, scraper

if __name__ == "__main__":
    args = sys.argv[1:]
    mode, years = "base", 1
    if "--backfill" in args:
        i = args.index("--backfill")
        try:
            years = int(args[i + 1])
            del args[i : i + 2]
        except (IndexError, ValueError):
            sys.exit("usage: python scan.py [--backfill YEARS] [channels-file]")
        mode = "backfill"
    db.init_db()
    if args:
        with open(args[0], encoding="utf-8") as f:
            added, skipped = db.import_channel_lines(f.read())
        print(f"Imported {len(added)} channel(s), skipped {len(skipped)}.")
    scraper.run_scan(mode=mode, years=years)
    for line in scraper.STATE["log"]:
        print(line)
