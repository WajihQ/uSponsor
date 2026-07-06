# uSponsor — project context for Claude

Local Flask + yt-dlp app that tracks which brands sponsor YouTube creators for an
influencer-marketing agency (tech/gaming niches). No paid APIs. Single user, runs
on the owner's Windows PC (`python app.py` → http://127.0.0.1:5000).

## Architecture

- `app.py` — all Flask routes. Pages: Dashboard `/`, Brands `/brands`, Channels
  `/channels`, plus detail pages `/brand/<key>` and `/creator/<id>`.
- `tracker/db.py` — SQLite schema + migrations (idempotent `init_db()` runs on
  every start; new columns are added via PRAGMA checks — always migrate this way,
  the user has a live `sponsors.db` they must never lose). WAL mode for the
  parallel scanner.
- `tracker/scraper.py` — yt-dlp scanning. Base scan: newest 30 listed, ≤12
  fetched per channel, skips channels scanned <24h ago, 4 parallel workers
  (`USPONSOR_WORKERS`). Backfill scan: sequential + 1.5s sleeps, walks full
  uploads feed to a cutoff, remembers depth per channel (`backfilled_to`).
  Post-scan `segment_pass()` queries SponsorBlock and auto-names sponsors from
  caption slices.
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
- YouTube/SponsorBlock may be unreachable in sandboxes — test scraping with
  monkeypatched `_list_uploads`/`_fetch_video` (see git history for patterns).
- Windows matters: no `%-d` strftime, no glibc-only anything.
- Precision over recall in the detector; every detection stores its evidence.
- Creator stats use a trimmed mean: last 12 videos with view data, drop the
  single highest+lowest, average 10; engagement = (likes+comments)/views over
  the same set.
- Row actions must not reload the page (`data-ajax` pattern).
- Commit style: what+why prose; push to `main` (owner works from main).

## Where we left off / agreed next step

Owner approved adding an **optional LLM tier** for brand extraction:
- Claude Haiku 4.5 (`claude-haiku-4-5`), key via `ANTHROPIC_API_KEY`, feature
  fully optional (no key → app behaves exactly as today).
- LLM is the THIRD tier only: regex first, known-brands/alias pass second, LLM
  only for the residue (SponsorBlock-flagged videos with no named brand, failed
  caption auto-naming, maybe low-confidence captures). Keeps cost ~pennies/day.
- Fold into `segment_pass()` + a "run LLM check" button on the Brands tab.
- Prompt should include the known-brand/alias list so outputs land on canonical
  names; output "none" must be handled.

Other backlog ideas discussed: rising-brands widget (30d vs prior 30d), lapsed
sponsors (brand sponsored creator before but not in 90d), CSV export of filtered
views, coverage gaps (closed creators with no recent sponsor).
