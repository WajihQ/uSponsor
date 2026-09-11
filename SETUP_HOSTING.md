# Hosting — setup

This covers running uSponsor on a real host (Render/Railway/etc.) instead of
your own PC. Do the DB setup first ([SETUP_TURSO.md](SETUP_TURSO.md)) — this
builds on top of that.

## What changed for hosting

- **`wsgi.py`** is the production entry point (`gunicorn wsgi:app`), not
  `app.py` — same routes, no `__main__` dev-server block.
- **Gmail sync has no automatic interval anymore.** It used to run as a
  background thread, but that thread only ever started when you ran
  `python app.py` directly — it never worked under a real WSGI server
  anyway. It's replaced by `POST /cron/gmail-sync`, meant to be hit on a
  schedule by something outside the app (see below). The manual **Sync now**
  button in Settings still works exactly as before, locally or hosted.
- **Local-disk state now lives in the DB**, since a typical host's
  filesystem doesn't survive a redeploy: Gmail OAuth tokens (`gmail_tokens`
  table), the YouTube cookies file (`app_config['cookies_txt']`), and the
  scrape-throttle/quota cooldowns (`app_config`). Nothing to configure here —
  it's automatic once `TURSO_DATABASE_URL` is set.

## 1. Deploy

1. Push this repo to GitHub (already done).
2. On Render or Railway, create a new **Web Service** from the repo. Both
   read the `Procfile` (`web: gunicorn wsgi:app --workers 1 --bind
   0.0.0.0:$PORT`) automatically.
3. **Use exactly one worker** (already set in the `Procfile`) — the scan/
   Gmail/Instantly sync progress indicators (`STATE` dicts) are per-process,
   in-memory. With more than one worker, progress shown to you could belong
   to a different worker than the one actually running the job. Since this
   is a single-user tool, one worker is also all you need.
4. Set these environment variables on the host:
   - `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` — from SETUP_TURSO.md.
   - `CRON_SECRET` — any long random string you generate yourself (e.g.
     `openssl rand -hex 32`). Protects `/cron/gmail-sync` from being
     triggered by anyone who finds the URL.
   - Whatever you already use locally that's env-var-based: `INSTANTLY_API_KEY`,
     `YOUTUBE_API_KEY`, `USPONSOR_COOKIES_BROWSER` (skip this one — cookies
     come from the DB now, see below).
5. First deploy: the app calls `db.init_db()` on startup (same as local), so
   the schema gets created on Turso automatically the first time it boots.

## 2. Reconnect Gmail + re-upload cookies once, on the hosted instance

Local `gmail_tokens/*.json` and `cookies.txt` files don't automatically move
to the DB — they're per-machine local files. Once the hosted instance is up:

- **Gmail**: use the Connect/Reconnect links on the Settings page as normal —
  finishing OAuth writes straight to the `gmail_tokens` DB table now, no
  local file involved.
- **Cookies**: re-upload your `cookies.txt` via the Influencer CRM page's
  upload form — it now writes to both a local temp copy *and* the DB, so
  re-uploading once on the hosted instance persists it there permanently.

## 3. Wire up the Gmail sync schedule (free, via GitHub Actions)

Add `.github/workflows/gmail-sync.yml` to the repo:

```yaml
name: Gmail sync
on:
  schedule:
    - cron: "*/30 * * * *"
  workflow_dispatch: {}
jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - name: Trigger sync
        run: |
          curl -sf -X POST "https://YOUR-HOSTED-URL/cron/gmail-sync" \
            -H "X-Cron-Secret: ${{ secrets.CRON_SECRET }}"
```

Add `CRON_SECRET` (the same value you set on the host) as a GitHub Actions
repo secret: **Settings → Secrets and variables → Actions → New repository
secret**. `workflow_dispatch` lets you trigger it manually from the Actions
tab too, for testing.

(Any other free scheduler — cron-job.org, etc. — works the same way: it just
needs to `POST` to `/cron/gmail-sync` with the right header on a schedule.)

## Notes

- Scans (`POST /scan`) stay a manual button click, same as today — they
  don't need a cron trigger, just the state (cookies, throttle) to survive a
  redeploy, which is now handled.
- Running yt-dlp from a shared/free-tier host means its IP reputation with
  YouTube is unproven, unlike your home IP today — if scans start hitting
  the bot-detection wall more than they used to locally, that's the likely
  cause, not a regression in this code.
- Access control on the app itself (so it's not just "whoever finds the URL
  can use it") is a deliberately separate, not-yet-done piece.
