# uSponsor — project context for Claude

Local Flask app that tracks which brands sponsor YouTube creators for an
influencer-marketing agency (tech/gaming niches). No paid APIs — metadata comes
from the YouTube Data API v3 free tier (primary, added 2026-07-29) with
yt-dlp/cookies as the automatic fallback. Single user, runs on the owner's
Windows PC (`python app.py` → http://127.0.0.1:5000).

## Architecture

- `app.py` — all Flask routes. Pages: Dashboard `/`, Brands `/brands`, Channels
  `/channels`, plus detail pages `/brand/<key>` and `/creator/<id>`.
- `tracker/db.py` — SQLite schema + migrations (idempotent `init_db()` runs on
  every start; new columns are added via PRAGMA checks — always migrate this way,
  the user has a live `sponsors.db` they must never lose). WAL mode for the
  parallel scanner.
- `tracker/youtube_api.py` — YouTube Data API v3 (free tier, `youtube_api.json`
  or `YOUTUBE_API_KEY`). Primary source for channel listings and video stats:
  `videos.list`/`playlistItems.list`, quota-metered (not the scraping
  throttle), batches up to 50 video ids/call. Auto-captions aren't available
  through it, so spoken-sponsor detection stays on yt-dlp regardless.
- `tracker/scraper.py` — metadata fetch is API-first, yt-dlp/cookies fallback
  (per-call, via `_fetch_video`/`_list_uploads`; `scan_channel` and
  `stats_backfill_pass` batch their API lookups directly for efficiency). A
  circuit breaker (`Throttled`/`throttle_active`/`_trip_throttle`) sits under
  every yt-dlp call: a real throttle/block response pauses all further
  requests for a cooldown (20-60 min depending on severity) instead of
  grinding through the rest of the queue, and resumes on its own once it
  passes — added 2026-07-25 after an unpaced backfill tripped a YouTube
  session-level block. Base scan: newest 30 listed, ≤12 fetched per channel,
  skips channels scanned <24h ago, 4 parallel workers (`USPONSOR_WORKERS`).
  Backfill scan: sequential + 1.5s sleeps (yt-dlp path only), walks full
  uploads feed to a cutoff, remembers depth per channel (`backfilled_to`).
  Post-scan `segment_pass()` queries SponsorBlock and auto-names sponsors from
  caption slices; `stats_backfill_pass()` drains any videos missing
  `view_count` (capped per run — `USPONSOR_STATS_BACKFILL_LIMIT_API` when the
  Data API is available, `USPONSOR_STATS_BACKFILL_LIMIT` on the yt-dlp
  fallback) so creator stats self-heal over time instead of needing a one-off
  repair script — added 2026-07-25 after a batch of videos backfilled before
  `view_count` was captured (2026-07-05) turned out permanently stuck at
  NULL, since scan modes never revisit an already-stored video.
- `tracker/detector.py` — regex sponsor detection over descriptions
  ("sponsored by X", "% off X", "use code Y at X"…), with a cleaning pipeline
  (junk like "checkout", "code NUTTY", "the link below" is rejected), a
  known-brands assist pass, and `detect_spoken()` for caption transcripts.
- `tracker/sponsorblock.py` — free SponsorBlock API (in-video paid segments) +
  json3 caption slicing.
- `templates/` — server-rendered Jinja; row actions post via fetch()
  (`data-ajax` attributes in `base.html`) and update the DOM in place so scroll
  position is preserved. Light+dark theme via CSS custom properties.
- Data: `sponsors.db` (SQLite, gitignored) + `uploads/<channel id>/` images
  (gitignored). Both live next to app.py; back up together.

## Key domain concepts

- **channels**: status ('prospect'|'closed' = signed with us), niche/subniche,
  agency (repped by a competitor), media-kit fields (rates, demographics),
  subscribers; `backfilled_to` marks completed backfill depth.
- **sponsorships**: one row per (video, brand_key). `brand_key` =
  lowercase-alphanumeric normalization (see `detector.brand_key`). Evidence text
  is stored ("spoken:" prefix = from captions, "manual:" = review queue).
- **brands** table = CRM state: kind 'known' (in the user's external CRM),
  'erroneous' (junk detection — hidden everywhere), 'boycott' (never suggest,
  but keep visible on dashboard with 🚫).
- **brand_aliases**: rename-consolidations ("Opera Air"→"Opera") are remembered
  and applied at scan time via `db.apply_alias`.
- **Review queue**: videos with a SponsorBlock segment but no named brand →
  `videos.review='pending'`, resolved via one-field form on the Brands tab.
- Dashboard filters (time/brand/creator/agency/niche/subniche/status) drive ALL
  widgets via one shared SQL condition. Erroneous brands are excluded there.

## Conventions

- Test with `python demo_seed.py` (wipes sponsors.db, seeds fake data).
- `tests/` has Playwright browser tests for the shared spreadsheet component
  (`base.html`'s virtualized filter/sort/paginate JS behind the Influencer CRM
  and Brand CRM tables) — real DOM/JS behavior that a Flask `test_client()`
  can't exercise. One-time setup: `pip install -r requirements-dev.txt &&
  playwright install chromium`. Run with `pytest`. Uses its own throwaway
  SQLite DB (`USPONSOR_DB` env var, set in `tests/conftest.py`), never
  `sponsors.db`.
- YouTube/SponsorBlock may be unreachable in sandboxes — test scraping with
  monkeypatched `_list_uploads`/`_fetch_video` (see git history for patterns).
- Windows matters: no `%-d` strftime, no glibc-only anything.
- Precision over recall in the detector; every detection stores its evidence.
- Creator stats use a trimmed mean: last 12 videos with view data, drop the
  single highest+lowest, average 10; engagement = (likes+comments)/views over
  the same set.
- Row actions must not reload the page (`data-ajax` pattern).
- Commit style: what+why prose; push to `main` (owner works from main).

## DB cleanup: Claude does it in-session, on request (no LLM APIs)

A free-tier LLM tier (Gemini/Groq/OpenRouter fallback chain) was built, used
once for a bulk cleanup, and **removed on 2026-07-07** — the classifier proved
unreliable (it flagged real brands like Acer/AMD/Samsung as junk because it
didn't recognise them), and gating its output just duplicated the judgment work.
Owner's standing instruction: **when asked, Claude scans the DB directly in the
session and cleans up erroneous brands itself** — no API keys, no credits.

How to run a cleanup (the proven method, done 2026-07-06/07):

1. Pull distinct brands not yet in the `brands` table, with a sample `evidence`
   per brand; read them yourself. Junk = discount phrases ("with code X",
   "on Amazon", "until 12/1"), codes, dates/prices, symbol/emoji leads, generic
   filler. KEEP anything naming a real brand/product — even messy variants and
   @handles; never suppress a real brand. Precision over recall.
2. Removals: back up rows to a timestamped `cleanup_backup_*.csv` first, mark
   the junk string `erroneous`, delete its rows, and flag any video left with
   no sponsor as `review='recover'`.
3. Recovery: for each `review='recover'` video, read its stored description
   (slice around sponsor keywords) and name the real sponsor; insert with
   evidence `"manual: recovered from description (Claude review)"` and set
   `review='resolved'`. Genuine false positives ("NOT sponsored by X", jokes,
   editorial, pure affiliate lists) and no-description videos → `review=NULL`.
4. Consolidations: map variants ("@asusrog", "Dreame X50 Ultra") onto canonical
   names via `db.consolidate_brand()` (shared with the Brands-tab rename; it
   records an alias so future scans map the variant automatically).

Results so far: 7,249 → ~6,600 sponsorship rows; 1,673 → ~870 distinct brands;
~650 junk strings hidden as `erroneous`; 300+ sponsors hand-recovered; recover
queue drained to 0. Backups: `cleanup_backup_*.csv` in the project root
(gitignored).

Other backlog ideas discussed: rising-brands widget (30d vs prior 30d), lapsed
sponsors (brand sponsored creator before but not in 90d), CSV export of filtered
views, coverage gaps (closed creators with no recent sponsor).
