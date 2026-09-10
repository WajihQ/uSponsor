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
  `duration_backfill_pass()` (added 2026-08-07) does the same drain-over-time
  thing for `duration_seconds`/`is_short` on videos stored before Shorts
  tracking existed — everything fetched from here on gets duration for free
  via the Data API's `contentDetails` part (or yt-dlp's `duration` field), so
  this pass only matters for the pre-existing backlog.
- `tracker/detector.py` — regex sponsor detection over descriptions
  ("sponsored by X", "brought to you by X", "partnered with X"…), with a
  cleaning pipeline (junk like "checkout", "code NUTTY", "the link below" is
  rejected), a known-brands assist pass, and `detect_spoken()` for caption
  transcripts. Only actual paid-sponsorship language matches — as of
  2026-08-13 "use code X at Y" and "N% off X" are deliberately **not**
  patterns (removed, previously were): a discount code or affiliate link
  means gifted/affiliate, not paid, and the owner only wants brands that
  actually pay for placement (agency outreach targets). Precision over
  recall applies doubly here — some real sponsors that only ever phrase
  their disclosure as a discount code will now be missed entirely, which is
  an intentional tradeoff, not a bug.
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
- **Shorts** (`videos.is_short`, added 2026-08-07): classified by
  `duration_seconds <= 180` (YouTube's current Shorts definition, extended to
  3min Oct 2024); `NULL` duration defaults to long-form rather than being
  dropped. Sponsorship detection/SponsorBlock/review-queue treat Shorts like
  any other video — only the average-views/engagement-rate stats split them
  out, so a creator's Shorts cadence or virality never dilutes their
  long-form numbers (Influencer CRM avg views, and the creator page's two
  stat-widget groups + two separate "Recent videos"/"Recent Shorts" tables).
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
  the same set. Computed separately for long-form vs. Shorts (`app._channel_stats`).
- Row actions must not reload the page (`data-ajax` pattern).
- Commit style: what+why prose; push to `main` (owner works from main).

## DB cleanup: Claude does it in-session, on request (no LLM APIs)

A free-tier LLM tier (Gemini/Groq/OpenRouter fallback chain) was built, used
once for a bulk cleanup, and **removed on 2026-07-07** — the classifier proved
unreliable (it flagged real brands like Acer/AMD/Samsung as junk because it
didn't recognise them), and gating its output just duplicated the judgment work.
Owner's standing instruction: **when asked, Claude scans the DB directly in the
session and cleans up erroneous brands itself** — no API keys, no credits.

A full per-video transcript/description review pass ran 2026-08-12 through
2026-08-14 (not the brand-key skim described below — reads each video's
actual description, escalating to a Supadata transcript fetch only when the
description alone was ambiguous). All 7,749 queued videos drained to
`status=done`. See "Results so far" below for the summary;
`transcript_review_progress.md` (gitignored) still has the full per-batch
log if a future pass wants the detailed precedent history. If a similar
review needs to run again later (e.g. after a large new batch of
unclassified brand_keys accumulates), `build_transcript_review_queue.py`
can top up the queue and the same two-phase pipeline
(`build_description_batch.py` → judge → `apply_transcript_batch.py`)
applies — see git history around this date for the exact scripts, since
they're gitignored/local-only and not preserved in the repo.

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

Results so far: 7,249 → ~6,600 sponsorship rows (2026-07-06/07 pass); 1,673 →
~870 distinct brands. Second full pass 2026-08-07 (triggered by adding Shorts
tracking, which surfaced that ~1,337 brand_keys — most of the DB's long tail —
had never been classified): read all 1,337 individually against their stored
evidence text (not just the name), verifying anything unfamiliar before
judging. ~883 marked `known`, ~200 `erroneous`, ~250 misattribution/variant
keys consolidated onto a canonical name (e.g. a reseller's "Sponsor: Windows
11 Pro ($26)" ad getting mis-extracted as a "Windows" sponsorship — the real
sponsor was the key-reseller, already captured separately; or "Sponsor:
Newegg Gamdias Atlas M4" splitting into two brand_keys for one co-promo).
Recurring failure modes worth knowing about for the next pass: (1) a brand_key
can mix a genuine sponsorship with unrelated "NOT sponsored by X" negations or
generic-word collisions from the same normalized key (e.g. "Box" catching both
real Box-AI disclosures and generic "first box"/"what's in the box" phrases;
"Twitch" catching creators' own channel plugs, zero of which were paid Twitch
sponsorships) — these need row-level surgery (keep the genuine rows, delete
the rest), not a blanket brand-level call. (2) Editorial/news mentions of a
real company (e.g. "GM", "Manchester United", "Volkswagen" showing up via
unrelated partnership news commentary) aren't sponsorships of *that* video —
erroneous despite being real brands. Recover queue (51, from erroneous-brand
deletions) and the review='pending' SponsorBlock queue (36, some untouched
since before the 07-06 pass) were both drained the same session: description/
caption text named a real sponsor for about 2/3 of them (inserted with
`manual: recovered from description (Claude review)`); the rest had no
recoverable signal (no description, explicit negation, or pure editorial) →
`review=NULL`. Backups: `cleanup_backup_*.csv` in the project root
(gitignored).

Third pass, 2026-08-12 to 2026-08-14 (the full transcript/description
review — see above): drained all 7,749 queued videos across ~26 batches of
~300, mostly using the free description-only phase (Supadata transcript
fetches reserved for genuinely ambiguous cases). DB now sits at ~17,900
sponsorship rows across ~2,300 distinct brand_keys (~1,550 marked
`erroneous`). Recurring failure modes confirmed/extended this pass, worth
knowing for any future cleanup: (1) **discount codes and affiliate links are
not sponsorships** even when phrased as a "disclosure" — this pass's
dominant rejection reason by far, since the affiliate-vs-paid distinction
(`tracker/detector.py`, 2026-08-13) is stricter than what earlier detector
versions captured; (2) **duplicate-key fragmentation** — one real
sponsorship sentence getting captured under two+ brand_keys because the
regex split mid-sentence (e.g. "sponsored by Anker Nebula" fragmenting into
`anker`+`soundcore`+`ankernebula` on the same video) — fix by deleting the
redundant key's row on that video only, keeping the clean capture; (3)
**channel-wide boilerplate blocks** some creators paste a near-identical
links section into every video description regardless of topic — genuinely
sponsored for the channel as a whole (keep, if it has real sponsor language
or a personalized code tied to the channel/creator name) vs. generic
recurring affiliate-shop filler with no sponsor word (reject); (4)
**personalized code/path matching the creator's own name or channel**
(`geni.us/JASON30`, `piavpn.com/HardwareHaven`, `brilliant.org/DataSlayer`)
counts as a paid disclosure even without the literal word "sponsor" —
established as a recurring KEEP category; (5) a `manual: recovered from
description` evidence tag from an *earlier* pass is not itself proof the
recovery was correct — several bad ones surfaced (a description's first
random product link grabbed with no actual sponsor language anywhere)
and were caught only by re-reading the full description, not trusting the
tag. Backups: `cleanup_backup_*_transcript_review.csv` in the project root
(gitignored).

Other backlog ideas discussed: rising-brands widget (30d vs prior 30d), lapsed
sponsors (brand sponsored creator before but not in 90d), CSV export of filtered
views, coverage gaps (closed creators with no recent sponsor).
