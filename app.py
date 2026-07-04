"""uSponsor — track which brands sponsor which YouTube creators.

Run:  python app.py   then open http://127.0.0.1:5000
"""
import datetime as dt

from flask import Flask, flash, jsonify, redirect, render_template, request, url_for

from tracker import db, scraper

app = Flask(__name__)
app.secret_key = "usponsor-local"  # local single-user tool; only used for flash messages
db.init_db()

RANGES = {"7": "Last 7 days", "30": "Last 30 days", "90": "Last 90 days", "all": "All time"}


def _since(days_param):
    if days_param == "all":
        return "0000-00-00"
    days = int(days_param)
    return (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()


@app.route("/")
def dashboard():
    days = request.args.get("days", "30")
    if days not in RANGES:
        days = "30"
    f_brand = request.args.get("brand", "")
    f_creator = request.args.get("creator", "")
    f_status = request.args.get("status", "")
    if f_status not in ("", "closed"):
        f_status = ""
    f_niche = request.args.get("niche", "")
    f_subniche = request.args.get("subniche", "")
    f_agency = request.args.get("agency", "")
    since = _since(days)

    conn = db.connect()
    try:
        # One set of filter conditions drives every widget on the page.
        # The time floor is parameterized so the week grid can use its own window.
        cond = " AND ".join(
            ["v.upload_date >= ?"]
            + (["s.brand_key = ?"] if f_brand else [])
            + (["c.id = ?"] if f_creator else [])
            + (["c.status = ?"] if f_status else [])
            + (["c.niche = ?"] if f_niche else [])
            + (["c.subniche = ?"] if f_subniche else [])
            + (["c.agency = ?"] if f_agency else [])
        )

        def cargs(time_floor):
            out = [time_floor]
            if f_brand: out.append(f_brand)
            if f_creator: out.append(int(f_creator))
            if f_status: out.append(f_status)
            if f_niche: out.append(f_niche)
            if f_subniche: out.append(f_subniche)
            if f_agency: out.append(f_agency)
            return out

        base = (
            " FROM sponsorships s JOIN videos v ON v.id = s.video_ref"
            " JOIN channels c ON c.id = v.channel_ref WHERE " + cond
        )

        rows = conn.execute(
            "SELECT s.brand, s.brand_key, s.evidence, v.title, v.url, v.upload_date,"
            " c.name AS creator, c.id AS creator_id "
            + base
            + " ORDER BY v.upload_date DESC, s.id DESC LIMIT 500",
            cargs(since),
        ).fetchall()

        # brand x creator counts for the heatmap (all active filters apply)
        pairs = conn.execute(
            "SELECT s.brand_key, MIN(s.brand) AS brand, c.name AS creator, c.id AS creator_id,"
            " COUNT(*) AS n " + base + " GROUP BY s.brand_key, c.id ORDER BY n DESC",
            cargs(since),
        ).fetchall()

        # last-7-days grid: trailing week window, same non-time filters
        week_start = (dt.date.today() - dt.timedelta(days=6)).isoformat()
        week = conn.execute(
            "SELECT s.brand, v.title, v.url, v.upload_date, c.name AS creator, c.status "
            + base + " ORDER BY c.name, v.upload_date",
            cargs(week_start),
        ).fetchall()

        stats = {
            "week": conn.execute("SELECT COUNT(*)" + base, cargs(week_start)).fetchone()[0],
            "brands": conn.execute(
                "SELECT COUNT(DISTINCT s.brand_key)" + base, cargs(since)
            ).fetchone()[0],
            "creators": conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0],
            "videos": conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0],
        }

        all_brands = conn.execute(
            "SELECT brand_key, MIN(brand) AS brand FROM sponsorships GROUP BY brand_key ORDER BY brand"
        ).fetchall()
        all_creators = conn.execute(
            "SELECT id, COALESCE(name, input_url) AS name, status FROM channels ORDER BY name"
        ).fetchall()
        closed_names = {r["name"] for r in all_creators if r["status"] == "closed"}
        all_niches = [
            r["niche"] for r in conn.execute(
                "SELECT DISTINCT niche FROM channels WHERE niche IS NOT NULL AND niche != '' ORDER BY niche"
            )
        ]
        all_subniches = [
            r["subniche"] for r in conn.execute(
                "SELECT DISTINCT subniche FROM channels WHERE subniche IS NOT NULL AND subniche != ''"
                + (" AND niche = ?" if f_niche else "") + " ORDER BY subniche",
                (f_niche,) if f_niche else (),
            )
        ]
        all_agencies = [
            r["agency"] for r in conn.execute(
                "SELECT DISTINCT agency FROM channels WHERE agency IS NOT NULL AND agency != '' ORDER BY agency"
            )
        ]
    finally:
        conn.close()

    # Build heatmap axes: top 12 brands by total, creators that appear.
    brand_totals = {}
    for p in pairs:
        brand_totals.setdefault(p["brand_key"], [p["brand"], 0])
        brand_totals[p["brand_key"]][1] += p["n"]
    top_brands = sorted(brand_totals.items(), key=lambda kv: -kv[1][1])[:12]
    top_keys = [k for k, _ in top_brands]
    creators_in_grid = sorted({p["creator"] for p in pairs if p["brand_key"] in top_keys})
    cell = {(p["brand_key"], p["creator"]): p["n"] for p in pairs}
    heatmap = {
        "creators": creators_in_grid,
        "rows": [
            {
                "brand": disp,
                "cells": [cell.get((key, cr), 0) for cr in creators_in_grid],
                "total": total,
            }
            for key, (disp, total) in top_brands
        ],
        "max": max((p["n"] for p in pairs if p["brand_key"] in top_keys), default=0),
    }

    # Week grid: creator rows x 7 day columns of brand chips, today first.
    day_list = [(dt.date.today() - dt.timedelta(days=i)) for i in range(7)]
    week_grid = {}
    for r in week:
        week_grid.setdefault(r["creator"], {d.isoformat(): [] for d in day_list})
        if r["upload_date"] in week_grid[r["creator"]]:
            week_grid[r["creator"]][r["upload_date"]].append(r)

    return render_template(
        "dashboard.html",
        rows=rows, stats=stats, heatmap=heatmap,
        week_grid=week_grid, day_list=day_list,
        ranges=RANGES, days=days, f_brand=f_brand, f_creator=f_creator, f_status=f_status,
        f_niche=f_niche, f_subniche=f_subniche, all_niches=all_niches, all_subniches=all_subniches,
        f_agency=f_agency, all_agencies=all_agencies,
        all_brands=all_brands, all_creators=all_creators, closed_names=closed_names,
        scan=scraper.STATE,
    )


@app.route("/channels")
def channels():
    conn = db.connect()
    try:
        chans = conn.execute(
            "SELECT c.*, COUNT(DISTINCT v.id) AS videos, COUNT(s.id) AS spons"
            " FROM channels c LEFT JOIN videos v ON v.channel_ref = c.id"
            " LEFT JOIN sponsorships s ON s.video_ref = v.id"
            " GROUP BY c.id ORDER BY COALESCE(c.name, c.input_url)"
        ).fetchall()
        niches = [
            r["niche"] for r in conn.execute(
                "SELECT DISTINCT niche FROM channels WHERE niche IS NOT NULL AND niche != '' ORDER BY niche"
            )
        ]
        agencies = [
            r["agency"] for r in conn.execute(
                "SELECT DISTINCT agency FROM channels WHERE agency IS NOT NULL AND agency != '' ORDER BY agency"
            )
        ]
    finally:
        conn.close()
    return render_template("channels.html", chans=chans, niches=niches, agencies=agencies, scan=scraper.STATE)


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
    flash("Creator details updated.", "ok")
    return redirect(request.referrer or url_for("channels"))


@app.route("/brands")
def brands():
    conn = db.connect()
    try:
        known = conn.execute(
            "SELECT b.*, "
            " (SELECT COUNT(*) FROM sponsorships s WHERE s.brand_key = b.brand_key) AS hits"
            " FROM brands b ORDER BY b.name COLLATE NOCASE"
        ).fetchall()
        suggestions = conn.execute(
            "SELECT s.brand_key, MIN(s.brand) AS name, COUNT(*) AS n,"
            " COUNT(DISTINCT c.id) AS creators, MAX(v.upload_date) AS last_seen"
            " FROM sponsorships s JOIN videos v ON v.id = s.video_ref"
            " JOIN channels c ON c.id = v.channel_ref"
            " WHERE s.brand_key NOT IN (SELECT brand_key FROM brands)"
            " GROUP BY s.brand_key ORDER BY n DESC, last_seen DESC"
        ).fetchall()
    finally:
        conn.close()
    return render_template("brands.html", known=known, suggestions=suggestions, scan=scraper.STATE)


@app.route("/brands/import", methods=["POST"])
def brands_import():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("No file selected.", "err")
        return redirect(url_for("brands"))
    added, skipped = db.import_brand_lines(f.read().decode("utf-8", errors="replace"))
    flash(f"Imported {len(added)} brand(s); skipped {len(skipped)} (already known/invalid).", "ok")
    return redirect(url_for("brands"))


@app.route("/brands/mark", methods=["POST"])
def brands_mark():
    name = request.form.get("name", "").strip()
    if name:
        db.import_brand_lines(name)
        flash(f"“{name}” marked as known — it won't be suggested again.", "ok")
    return redirect(url_for("brands"))


@app.route("/brands/<int:bid>/delete", methods=["POST"])
def brands_delete(bid):
    conn = db.connect()
    try:
        conn.execute("DELETE FROM brands WHERE id = ?", (bid,))
        conn.commit()
    finally:
        conn.close()
    flash("Brand removed from the known list — it may reappear as a suggestion.", "ok")
    return redirect(url_for("brands"))


@app.route("/channels/<int:cid>/delete", methods=["POST"])
def channels_delete(cid):
    conn = db.connect()
    try:
        conn.execute("DELETE FROM channels WHERE id = ?", (cid,))
        conn.commit()
    finally:
        conn.close()
    flash("Channel removed (its videos and sponsorships too).", "ok")
    return redirect(url_for("channels"))


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
    flash("Marked as closed — you're working together now." if new == "closed" else "Moved back to prospects.", "ok")
    return redirect(request.referrer or url_for("channels"))


@app.route("/scan/status")
def scan_status():
    return jsonify(scraper.STATE)


if __name__ == "__main__":
    app.run(debug=False, port=5000)
