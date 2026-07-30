"""Shared fixtures for the browser-driven CRM table tests.

Runs the real Flask app against a throwaway SQLite DB (never sponsors.db) on
a background thread, seeds enough rows to force pagination (50/page), and
hands tests a `base_url` + Playwright's `page` fixture to drive it for real.
"""
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# USPONSOR_DB must be set before `tracker.db` (and therefore `app`) is
# imported, since db.DB_PATH is read once at import time.
TEST_DB = os.path.join(tempfile.gettempdir(), "usponsor_test.db")
for suffix in ("", "-wal", "-shm"):
    try:
        os.remove(TEST_DB + suffix)
    except OSError:
        pass
os.environ["USPONSOR_DB"] = TEST_DB

import app as flask_app_module  # noqa: E402  (import after USPONSOR_DB is set)
from tracker import db  # noqa: E402


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="session")
def base_url():
    port = _free_port()
    thread = threading.Thread(
        target=lambda: flask_app_module.app.run(port=port, use_reloader=False, threaded=True),
        daemon=True,
    )
    thread.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(url + "/", timeout=0.5)
            break
        except Exception:
            time.sleep(0.1)
    else:
        raise RuntimeError("Flask test server never came up")
    return url


@pytest.fixture(scope="session", autouse=True)
def seed(base_url):
    """71 influencer rows and 71 brand-lead rows -- enough to force a second
    page at 50/page, so tests can prove search/sort/delete act on the whole
    dataset and not just whatever page happens to be rendered.

    One row per table is named/branded "...Zzyzx" so it deterministically
    sorts last once a test forces an alphabetical (client-side) sort.
    """
    conn = db.connect()
    for i in range(70):
        conn.execute(
            "INSERT INTO channels (input_url, channel_id, name, crm_status) VALUES (?, ?, ?, ?)",
            (f"https://youtube.com/@ctest{i}", f"UCctest{i:015d}", f"Creator {i:03d}",
             "Contacted" if i % 2 else "New"),
        )
        conn.execute(
            "INSERT INTO brand_leads (person, brand) VALUES (?, ?)",
            (f"Person {i:03d}", f"Brand {i:03d}"),
        )
    conn.execute(
        "INSERT INTO channels (input_url, channel_id, name, crm_status) VALUES (?, ?, ?, ?)",
        ("https://youtube.com/@needlezyx", "UCneedle0000000000000", "Needle Zzyzx", "New"),
    )
    conn.execute(
        "INSERT INTO brand_leads (person, brand) VALUES (?, ?)",
        ("Needle Zzyzx", "Needle Brand Zzyzx"),
    )
    conn.commit()
    conn.close()
