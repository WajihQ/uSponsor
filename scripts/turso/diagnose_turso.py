"""One-off spike: verify libsql's Python client behaves the way tracker/db.py
needs before rewriting connect() to support it. Run this against a real free
Turso database (see SETUP_TURSO.md) and read the printed answers — nothing
here is committed to the real schema/DB, it uses its own throwaway table.

Usage:
    set TURSO_DATABASE_URL=libsql://your-db-name.turso.io
    set TURSO_AUTH_TOKEN=your-token
    python scripts/turso/diagnose_turso.py
"""
import os
import sys
import tempfile

try:
    import libsql
except ImportError:
    print("libsql not installed — run: pip install libsql")
    sys.exit(1)

URL = os.environ.get("TURSO_DATABASE_URL")
TOKEN = os.environ.get("TURSO_AUTH_TOKEN")
if not URL or not TOKEN:
    print("Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN first (see SETUP_TURSO.md).")
    sys.exit(1)

replica_path = os.path.join(tempfile.gettempdir(), "usponsor_turso_spike.db")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(replica_path + ext)
    except FileNotFoundError:
        pass

print(f"Connecting (embedded replica at {replica_path}) ...")
conn = libsql.connect(replica_path, sync_url=URL, auth_token=TOKEN)
conn.sync()
print("Connected + synced OK.\n")

print("=== Q1: does executescript() work? ===")
try:
    conn.executescript(
        "DROP TABLE IF EXISTS spike_test;"
        "CREATE TABLE spike_test (id INTEGER PRIMARY KEY, name TEXT, brand_key TEXT UNIQUE);"
    )
    print("executescript: WORKS\n")
except Exception as e:
    print(f"executescript: FAILS -> {e!r}")
    print("  falling back to split-and-execute for this spike")
    conn.execute("DROP TABLE IF EXISTS spike_test")
    conn.execute(
        "CREATE TABLE spike_test (id INTEGER PRIMARY KEY, name TEXT, brand_key TEXT UNIQUE)"
    )
    print()

print("=== Q2: does `with conn:` commit like sqlite3? ===")
try:
    with conn:
        conn.execute("INSERT INTO spike_test (name, brand_key) VALUES ('Alpha', 'alpha')")
    print("context-manager insert: no exception raised\n")
except Exception as e:
    print(f"context-manager insert: FAILS -> {e!r}\n")

print("=== Q3: cur.lastrowid after a plain INSERT? ===")
cur = conn.execute("INSERT INTO spike_test (name, brand_key) VALUES ('Beta', 'beta')")
conn.commit()
print(f"lastrowid = {cur.lastrowid!r} (expect an int, e.g. 2)\n")

print("=== Q4: cur.rowcount on INSERT OR IGNORE (duplicate key -> should be 0) ===")
cur = conn.execute(
    "INSERT OR IGNORE INTO spike_test (name, brand_key) VALUES ('Beta again', 'beta')"
)
conn.commit()
print(f"rowcount on a skipped duplicate = {cur.rowcount!r} (expect 0)\n")

print("=== Q4b: cur.rowcount on INSERT OR IGNORE (new key -> should be 1) ===")
cur = conn.execute(
    "INSERT OR IGNORE INTO spike_test (name, brand_key) VALUES ('Gamma', 'gamma')"
)
conn.commit()
print(f"rowcount on a real insert = {cur.rowcount!r} (expect 1)\n")

print("=== Q5: row access — does it support both name and positional indexing? ===")
row = conn.execute("SELECT * FROM spike_test WHERE brand_key = 'alpha'").fetchone()
print(f"raw row repr: {row!r}  (type: {type(row)})")
try:
    print(f"  row['name'] = {row['name']!r}")
except Exception as e:
    print(f"  row['name'] FAILS -> {e!r}")
try:
    print(f"  row.name (attribute) = {row.name!r}")
except Exception as e:
    print(f"  row.name (attribute) FAILS -> {e!r} <- expected to fail, sqlite3.Row also lacks real attrs")
try:
    print(f"  row[0] = {row[0]!r}")
except Exception as e:
    print(f"  row[0] FAILS -> {e!r}")
try:
    print(f"  list(row.keys()) = {list(row.keys())!r}")
except Exception as e:
    print(f"  row.keys() FAILS -> {e!r}")

print("\n=== Q6: fetchall() on a multi-row SELECT ===")
rows = conn.execute("SELECT id, name FROM spike_test ORDER BY id").fetchall()
print(f"got {len(rows)} rows: {[dict(zip(('id', 'name'), r)) if not hasattr(r, 'keys') else (r['id'], r['name']) for r in rows]}")

print("\n=== Q7: does the local embedded-replica file actually get written to disk? ===")
print(f"replica file exists: {os.path.isfile(replica_path)}, size: {os.path.getsize(replica_path) if os.path.isfile(replica_path) else 0} bytes")

conn.execute("DROP TABLE spike_test")
conn.commit()
print("\nCleaned up spike_test table. Done — read the answers above before touching tracker/db.py.")
