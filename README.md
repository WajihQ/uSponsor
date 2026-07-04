# uSponsor

Track which brands sponsor which YouTube creators — no paid APIs, no manual
video-opening. Point it at your channel list, hit **Scan now**, and get a
dashboard of who sponsored whom and when.

## How it works

- **Scraping** uses [yt-dlp](https://github.com/yt-dlp/yt-dlp) (free) to list
  each channel's newest uploads and read video descriptions.
- **Detection** parses standard sponsor-disclosure language: "sponsored by X",
  "thanks to X for sponsoring", "in partnership with X", "brought to you by X",
  "use code Y at X", and so on. Each hit is stored with the exact disclosure
  text so you can verify it.
- **Incremental**: scans only fetch videos not seen before (newest 30 listed,
  max 12 detail-fetched per channel per scan), so repeat runs are quick and
  easy on older hardware.

## Setup

```bash
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000

## Using it

1. **Channels page** — add channels one at a time (paste any channel URL or
   `@handle`), or import a `.txt`/`.csv` file with links (one or more per
   line; see `sample_channels.csv`). Optional columns after each link set
   the niche and sub-niche (`youtube.com/@x, Tech, Mini PCs`). Re-importing
   your roster is safe: existing channels aren't duplicated, blank niche
   fields get filled from the file, and hand-set niches are never
   overwritten. You can add more channels any time — they live in the
   app's database, not in code.
2. Hit **Scan now** (top right). Progress shows in the header; the page
   refreshes when done. First scan of a channel pulls its recent uploads;
   later scans only pick up new videos. Channels scanned within the last
   24 hours are skipped automatically — so after adding a batch of new
   channels, a scan only touches the new ones. The dropdown next to the
   button picks the target: **All channels**, **Closed only**, or
   **Force all** (ignores the 24-hour rule).
3. Tag each creator with a **niche and sub-niche** (e.g. Tech / Mini PCs)
   right in the channel list — the dashboard log can then be filtered by
   niche, and the search box above the channel list matches names, links
   and niches.
4. Mark influencers you've signed as **closed** on the Channels page
   ("Mark closed" / "Reopen"). Closed creators get a ✓ badge across the
   app, the dashboard log can filter to closed-only, and both scan types
   can target just your closed roster.
5. **Dashboard** shows:
   - **This week's sponsorships** — creator × day grid of brand chips; click
     a chip to open the video.
   - **Brand × creator frequency** — heatmap of how many times each brand
     sponsored each creator (e.g. Geekom × Aman = 3).
   - **Sponsorship log** — every detection with date, video link and the
     disclosure text found, filterable by time range / brand / creator.

## Backfill scans

**Scan now** only covers each channel's newest uploads. To pull in older
history, use the **Backfill scan** card on the Channels page: pick how many
years to go back (1–5) and start it. The backfill walks every channel's full
upload feed newest-to-oldest, skips videos already in the database, and stops
at the cutoff. Everything lands in the same database, so the dashboard's
time-range filters cover the backfilled history too.

Backfills are deliberately slow (a built-in 1.5s delay between video fetches
plus one to two seconds per fetch) to avoid YouTube rate limiting — deep
backfills over many channels can take hours. Each run also caps at 600
fetched videos per channel as a safety valve; if a channel hits the cap, run
the backfill again and it picks up where it left off. You typically backfill
once, then let regular scans keep things current.

## Headless scanning (optional)

Run scans on a schedule without opening the browser:

```bash
python scan.py                  # scan all channels in the DB
python scan.py channels.txt     # import channels from a file, then scan
python scan.py --backfill 2     # backfill scan going back 2 years
```

Point Windows Task Scheduler or cron at it, then just open the dashboard.

## Preview with demo data

```bash
python demo_seed.py   # wipes sponsors.db and fills it with fake data
python app.py
```

## Notes & limits

- Detection is only as good as the disclosure: sponsors mentioned *only*
  in-video (no description text) won't be caught. Precision is favoured over
  recall to keep the dashboard clean.
- All data lives in a single `sponsors.db` SQLite file next to the app —
  back it up or delete it to start fresh.
- Be reasonable with channel counts per scan; yt-dlp fetches one page per
  new video, and hammering YouTube can get you rate-limited.
