# Turso (hosted DB) — setup

Optional. Without this, uSponsor uses a plain local SQLite file exactly like
before — nothing changes for local/dev use. Set it up when you're ready to
host the app somewhere other than your own PC: Turso gives you an always-on,
SQLite-compatible database that isn't tied to any one machine's disk.

uSponsor connects to it via an **embedded replica**: a local SQLite file
(same `sponsors.db` path as always, or wherever `USPONSOR_DB` points) stays
synced with the remote Turso database. Reads hit that local file at normal
SQLite speed; only writes and the sync itself go over the network.

## 1. Create a database

1. Sign up at [turso.tech](https://turso.tech) (free tier, no card required).
2. Create a database — via the dashboard ("Create Database"), or the CLI:
   ```bash
   turso db create usponsor
   ```
3. Get the two values the app needs:
   ```bash
   turso db show usponsor --url
   turso db tokens create usponsor
   ```
   (Both are also visible in the dashboard.)

## 2. Give them to the app

Set two environment variables — don't put these in a committed file:

```bash
export TURSO_DATABASE_URL="libsql://your-db-name.turso.io"
export TURSO_AUTH_TOKEN="your-token-here"
```

That's it — `tracker/db.py` picks these up automatically. If they're unset,
the app behaves exactly as before (plain local SQLite, no Turso involved at
all).

## 3. First run — create the schema on Turso

The very first time you point the app at a fresh Turso database, run it once
to create the tables (same idempotent `init_db()` that runs on every start
locally):

```bash
python app.py
```

## 4. Confirm it works

```bash
python scripts/turso/diagnose_turso.py
```

This runs a set of read/write checks against your Turso database (insert,
read back by name and by position, `rowcount`/`lastrowid` behavior) and
prints the results — useful any time you want to sanity-check the connection
independent of the running app.

## Notes

- **Windows + Python 3.14**: the `libsql` package doesn't ship a prebuilt
  Windows wheel for 3.14 yet (only through 3.13; Linux has a 3.14 wheel
  already). If `pip install libsql` tries to compile from source and fails,
  install Python 3.12 or 3.13 side by side and use that for local Turso
  testing — it doesn't affect your existing Python install. This won't be an
  issue on the actual host (Render/Railway/etc. run Linux).
- **Every request re-syncs.** Each `db.connect()` call syncs once so you
  never see stale data — this trades a bit of per-request latency (one
  network round trip) for correctness, which is the right call for a
  single-user, low-traffic tool. If this ever feels slow, the next lever is
  making connections long-lived across requests instead of per-request, but
  that's a bigger change than this trade-off currently justifies.
- **Row access is normalized for you.** libsql's Python client returns plain
  tuples (no `row["col"]` support natively) — `tracker/db.py` wraps every
  query result so the rest of the app (and every Jinja template using
  `{{ row.field }}`) keeps working identically to the local-SQLite path,
  with zero changes needed anywhere else.
- This covers the database only. The background sync loops (Gmail interval,
  scan), file-based state (`gmail_tokens/`, `cookies.txt`,
  `.throttle_state.json`), and actually picking/configuring a host are
  separate, not-yet-done pieces of the hosting migration.
