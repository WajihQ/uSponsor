"""Seed the database with fake data to preview the dashboard.

Usage:  python demo_seed.py     (wipes sponsors.db first)
"""
import datetime as dt
import os
import random

from tracker import db
from tracker.detector import brand_key

random.seed(7)

CREATORS = ["Aman Tech", "PixelForge", "GameDeck", "ByteSized", "FrameRate"]
BRANDS = ["Geekom", "NordVPN", "Ridge Wallet", "Squarespace", "Raycon", "dbrand", "Keychron", "Manscaped"]
# weight some pairs so the heatmap has structure (Geekom x Aman = 3, etc.)
PAIRS = [("Geekom", "Aman Tech")] * 3 + [("NordVPN", "Aman Tech")] * 2

if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

conn = db.connect()
for i, name in enumerate(CREATORS):
    conn.execute(
        "INSERT INTO channels (input_url, channel_id, name, last_scanned)"
        " VALUES (?, ?, ?, datetime('now'))",
        (f"https://www.youtube.com/@demo{i}", f"UCdemo{i:018d}", name),
    )

today = dt.date.today()
events = PAIRS + [(random.choice(BRANDS), random.choice(CREATORS)) for _ in range(40)]
for n, (brand, creator) in enumerate(events):
    date = (today - dt.timedelta(days=random.randint(0, 45))).isoformat()
    ch = conn.execute("SELECT id FROM channels WHERE name = ?", (creator,)).fetchone()["id"]
    cur = conn.execute(
        "INSERT INTO videos (video_id, channel_ref, title, url, upload_date)"
        " VALUES (?, ?, ?, ?, ?)",
        (f"demo{n:07d}", ch, f"Demo video #{n} — {brand} feature", "https://youtube.com/watch?v=dQw4w9WgXcQ", date),
    )
    conn.execute(
        "INSERT INTO sponsorships (video_ref, brand, brand_key, evidence) VALUES (?, ?, ?, ?)",
        (cur.lastrowid, brand, brand_key(brand), f"This video is sponsored by {brand}. Check them out"),
    )
conn.commit()
conn.close()
print(f"Seeded {len(CREATORS)} channels, {len(events)} sponsorships -> {db.DB_PATH}")
