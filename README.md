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
   line; see `sample_channels.csv`). You can add more channels any time —
   they live in the app's database, not in code.
2. Hit **Scan now** (top right). Progress shows in the header; the page
   refreshes when done. First scan of a channel pulls its recent uploads;
   later scans only pick up new videos.
3. **Dashboard** shows:
   - **This week's sponsorships** — creator × day grid of brand chips; click
     a chip to open the video.
   - **Brand × creator frequency** — heatmap of how many times each brand
     sponsored each creator (e.g. Geekom × Aman = 3).
   - **Sponsorship log** — every detection with date, video link and the
     disclosure text found, filterable by time range / brand / creator.

## Headless scanning (optional)

Run scans on a schedule without opening the browser:

```bash
python scan.py                  # scan all channels in the DB
python scan.py channels.txt     # import channels from a file, then scan
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
