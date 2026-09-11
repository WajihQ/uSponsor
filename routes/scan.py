"""Scan-trigger and status routes, plus the YouTube-cookies upload used to
run scans authenticated. Lives inside the `routes` package (as
`routes.scan`) so its module name doesn't collide with the top-level CLI
`scan.py`."""
import os

from flask import flash, jsonify, redirect, request, url_for

from app_core import app
from routes.helpers import _done
from tracker import db, scraper

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@app.route("/scan/cookies", methods=["POST"])
def scan_cookies_upload():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("No cookies file selected.", "err")
        return redirect(request.referrer or url_for("crm_influencers"))
    content = f.read()
    dest = os.path.join(_REPO_ROOT, "cookies.txt")
    with open(dest, "wb") as out:
        out.write(content)
    # also persist to the DB -- the durable copy once hosted, where local
    # disk doesn't survive a redeploy (see tracker/scraper.py::_cookiefile_path)
    conn = db.connect()
    try:
        db.set_config(conn, "cookies_txt", content.decode("utf-8", errors="replace"))
        conn.commit()
    finally:
        conn.close()
    flash("YouTube cookies saved — scans now run authenticated (much higher rate limits).", "ok")
    return redirect(request.referrer or url_for("crm_influencers"))


@app.route("/scan/cookies/clear", methods=["POST"])
def scan_cookies_clear():
    dest = os.path.join(_REPO_ROOT, "cookies.txt")
    if os.path.isfile(dest):
        os.remove(dest)
    conn = db.connect()
    try:
        conn.execute("DELETE FROM app_config WHERE key = 'cookies_txt'")
        conn.commit()
    finally:
        conn.close()
    return _done("YouTube cookies removed.", endpoint="crm_influencers")


@app.route("/scan", methods=["POST"])
def scan():
    mode = request.form.get("mode", "base")
    if mode not in ("base", "backfill"):
        mode = "base"
    # target dropdown: "all" | "closed" | "all_force" (rescan even if fresh)
    target = request.form.get("target", "all")
    force = target == "all_force"
    if target not in ("all", "closed"):
        target = "all"
    try:
        years = min(max(int(request.form.get("years", 1)), 1), 10)
    except ValueError:
        years = 1
    started = scraper.start_scan_in_background(mode=mode, years=years, target=target, force=force)
    if not started:
        flash("A scan is already running.", "err")
    elif mode == "backfill":
        flash(f"Backfill scan started — going back {years} year(s). This can take a while.", "ok")
    else:
        what = {"closed": "closed influencers", "all": "channels not scanned in 24h"}[target] if not force else "all channels (forced)"
        flash(f"Scan started: {what}.", "ok")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/scan/status")
def scan_status():
    return jsonify(scraper.STATE)
