"""Channels — add/import channels and per-channel admin actions (niche edit,
reset, delete, status). The `/channels` list view itself now redirects to
the Influencer CRM (kept as an endpoint so old links/`_done()` fallbacks
still resolve)."""
from flask import flash, redirect, request, url_for

from app_core import app
from routes.helpers import _done
from tracker import db


@app.route("/channels")
def channels():
    # The Channels page has been folded into the Influencer CRM; keep the
    # endpoint so old links and _done() fallbacks still resolve.
    return redirect(url_for("crm_influencers"))


@app.route("/channels/add", methods=["POST"])
def channels_add():
    status, info = db.add_channel(request.form.get("url", ""))
    if status == "added":
        flash("Added " + info, "ok")
    elif status == "updated":
        flash("Already tracked — filled in its niche from your input: " + info, "ok")
    else:
        flash("Not added: " + info, "err")
    return redirect(url_for("channels"))


@app.route("/channels/import", methods=["POST"])
def channels_import():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("No file selected.", "err")
        return redirect(url_for("channels"))
    text = f.read().decode("utf-8", errors="replace")
    added, updated, skipped = db.import_channel_lines(text)
    flash(
        f"Imported {len(added)} new channel(s), filled niches on {len(updated)} existing,"
        f" skipped {len(skipped)} (unchanged duplicates/invalid).",
        "ok",
    )
    return redirect(url_for("channels"))


@app.route("/channels/<int:cid>/niche", methods=["POST"])
def channels_niche(cid):
    niche = request.form.get("niche", "").strip()[:40]
    subniche = request.form.get("subniche", "").strip()[:40]
    agency = request.form.get("agency", "").strip()[:60]
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE channels SET niche = ?, subniche = ?, agency = ? WHERE id = ?",
            (niche or None, subniche or None, agency or None, cid),
        )
        conn.commit()
    finally:
        conn.close()
    return _done("Creator details updated.", endpoint="channels")


@app.route("/channels/<int:cid>/reset", methods=["POST"])
def channels_reset(cid):
    conn = db.connect()
    try:
        conn.execute("DELETE FROM videos WHERE channel_ref = ?", (cid,))
        conn.execute("UPDATE channels SET last_scanned = NULL, backfilled_to = NULL WHERE id = ?", (cid,))
        conn.commit()
    finally:
        conn.close()
    return _done("Channel videos cleared — the next scan re-fetches them fresh.", endpoint="channels")


@app.route("/channels/<int:cid>/delete", methods=["POST"])
def channels_delete(cid):
    conn = db.connect()
    try:
        conn.execute("DELETE FROM channels WHERE id = ?", (cid,))
        conn.commit()
    finally:
        conn.close()
    return _done("Channel removed (its videos and sponsorships too).", endpoint="channels")


@app.route("/channels/<int:cid>/status", methods=["POST"])
def channels_status(cid):
    new = request.form.get("status", "prospect")
    if new not in ("prospect", "closed"):
        new = "prospect"
    conn = db.connect()
    try:
        conn.execute("UPDATE channels SET status = ? WHERE id = ?", (new, cid))
        conn.commit()
    finally:
        conn.close()
    return _done("Status updated.", endpoint="channels")
