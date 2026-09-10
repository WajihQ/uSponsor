"""Sponsorship-row admin: reassign/delete a single detection, alias
cleanup, the sponsor-segment review queue's resolve/dismiss actions, and
the manual segment-check/redetect triggers."""
from flask import flash, redirect, request, url_for

from app_core import app
from routes.helpers import _done
from tracker import db, scraper
from tracker.detector import brand_key


@app.route("/sponsorship/<int:sid>/reassign", methods=["POST"])
def sponsorship_reassign(sid):
    """Correct a single detection: point one video's sponsorship at the right
    brand (used to split bundled/mangled detections video by video)."""
    name = request.form.get("brand", "").strip()[:60]
    key = brand_key(name)
    if not name or len(key) < 2:
        return _done("Brand name too short.", "err", endpoint="brands")
    conn = db.connect()
    try:
        name, key = db.apply_alias(name, db.alias_map(conn))
        # UPDATE OR IGNORE: if the video already has this brand recorded,
        # the duplicate row is dropped instead
        conn.execute(
            "UPDATE OR IGNORE sponsorships SET brand = ?, brand_key = ? WHERE id = ?",
            (name, key, sid),
        )
        conn.execute("DELETE FROM sponsorships WHERE id = ? AND brand_key != ?", (sid, key))
        conn.commit()
    finally:
        conn.close()
    return _done(f"Reassigned to {name}.", endpoint="brands")


@app.route("/sponsorship/<int:sid>/delete", methods=["POST"])
def sponsorship_delete(sid):
    conn = db.connect()
    try:
        conn.execute("DELETE FROM sponsorships WHERE id = ?", (sid,))
        conn.commit()
    finally:
        conn.close()
    return _done("Detection removed.", endpoint="brands")


@app.route("/aliases/<alias_key>/delete", methods=["POST"])
def alias_delete(alias_key):
    conn = db.connect()
    try:
        conn.execute("DELETE FROM brand_aliases WHERE alias_key = ?", (alias_key,))
        conn.commit()
    finally:
        conn.close()
    return _done("Alias removed — future scans will record that name separately.", endpoint="brands")


@app.route("/review/<int:vid>/resolve", methods=["POST"])
def review_resolve(vid):
    name = request.form.get("brand", "").strip()[:60]
    if not name or len(brand_key(name)) < 2:
        return _done("Brand name too short.", "err", endpoint="brands")
    conn = db.connect()
    try:
        name, key = db.apply_alias(name, db.alias_map(conn))
        conn.execute(
            "INSERT OR IGNORE INTO sponsorships (video_ref, brand, brand_key, evidence)"
            " VALUES (?, ?, ?, 'manual: confirmed from sponsor segment')",
            (vid, name, key),
        )
        conn.execute("UPDATE videos SET review = 'resolved' WHERE id = ?", (vid,))
        conn.commit()
    finally:
        conn.close()
    return _done(f"Recorded {name} for that video.", endpoint="brands")


@app.route("/review/<int:vid>/dismiss", methods=["POST"])
def review_dismiss(vid):
    conn = db.connect()
    try:
        conn.execute("UPDATE videos SET review = 'dismissed' WHERE id = ?", (vid,))
        conn.commit()
    finally:
        conn.close()
    return _done("Dismissed — won't be shown again.", endpoint="brands")


@app.route("/segments", methods=["POST"])
def segments():
    started = scraper.start_segment_pass_in_background()
    flash("Sponsor-segment check started." if started else "A scan is already running.",
          "ok" if started else "err")
    return redirect(url_for("brands"))


@app.route("/redetect", methods=["POST"])
def redetect():
    videos, new = scraper.rerun_detection()
    flash(f"Re-ran detection over {videos} stored description(s) — found {new} new sponsorship(s).", "ok")
    return redirect(request.referrer or url_for("brands"))
