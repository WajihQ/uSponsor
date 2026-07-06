"""uSponsor — track which brands sponsor which YouTube creators.

Run:  python app.py   then open http://127.0.0.1:5000
"""
import datetime as dt
import json
import os

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_from_directory, url_for
from werkzeug.utils import secure_filename

from tracker import db, scraper
from tracker.detector import brand_key

app = Flask(__name__)
app.secret_key = "usponsor-local"  # local single-user tool; only used for flash messages
db.init_db()

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _creator_dir(cid):
    return os.path.join(UPLOAD_DIR, str(int(cid)))


def _creator_images(cid):
    d = _creator_dir(cid)
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d) if os.path.splitext(f)[1].lower() in IMAGE_EXTS)

RANGES = {"7": "Last 7 days", "30": "Last 30 days", "90": "Last 90 days", "all": "All time"}


def _done(message, category="ok", endpoint="dashboard"):
    """Finish a row-action request: 204 for fetch() calls (the page updates
    itself in place), flash + redirect for plain form posts."""
    if request.headers.get("X-Requested-With") == "fetch":
        return "", 204
    flash(message, category)
    return redirect(request.referrer or url_for(endpoint))


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
    limit = request.args.get("limit", "50")
    if limit not in ("50", "100", "200"):
        limit = "50"
    limit = int(limit)
    try:
        page = max(int(request.args.get("page", 1)), 1)
    except ValueError:
        page = 1
    since = _since(days)

    conn = db.connect()
    try:
        # One set of filter conditions drives every widget on the page.
        # The time floor is parameterized so the week grid can use its own window.
        cond = " AND ".join(
            ["v.upload_date >= ?",
             "s.brand_key NOT IN (SELECT brand_key FROM brands WHERE kind = 'erroneous')"]
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

        total = conn.execute("SELECT COUNT(*)" + base, cargs(since)).fetchone()[0]
        pages = max((total + limit - 1) // limit, 1)
        page = min(page, pages)
        rows = conn.execute(
            "SELECT s.brand, s.brand_key, s.evidence, v.title, v.url, v.upload_date,"
            " c.name AS creator, c.id AS creator_id "
            + base
            + " ORDER BY v.upload_date DESC, s.id DESC LIMIT ? OFFSET ?",
            cargs(since) + [limit, (page - 1) * limit],
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
            "SELECT brand_key, MIN(brand) AS brand FROM sponsorships"
            " WHERE brand_key NOT IN (SELECT brand_key FROM brands WHERE kind = 'erroneous')"
            " GROUP BY brand_key ORDER BY brand"
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
        boycott_keys = {
            r["brand_key"]
            for r in conn.execute("SELECT brand_key FROM brands WHERE kind = 'boycott'")
        }
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
                "key": key,
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
        limit=limit, page=page, pages=pages, total=total, boycott_keys=boycott_keys,
        page_url=lambda p: url_for("dashboard", **{**request.args.to_dict(), "page": p}),
        filt_url=lambda k, v: url_for(
            "dashboard", **{**{kk: vv for kk, vv in request.args.to_dict().items() if kk != "page"}, k: v}
        ),
        creator_ids={r["name"]: r["id"] for r in all_creators},
        clear_url=lambda param: url_for(
            "dashboard", **{k: v for k, v in request.args.to_dict().items() if k not in (param, "page")}
        ),
        scan=scraper.STATE,
    )


@app.route("/channels")
def channels():
    f_status = request.args.get("status", "")
    if f_status not in ("", "closed", "prospect"):
        f_status = ""
    conn = db.connect()
    try:
        chans = conn.execute(
            "SELECT c.*, COUNT(DISTINCT v.id) AS videos, COUNT(s.id) AS spons"
            " FROM channels c LEFT JOIN videos v ON v.channel_ref = c.id"
            " LEFT JOIN sponsorships s ON s.video_ref = v.id"
            + (" WHERE c.status = ?" if f_status else "")
            + " GROUP BY c.id ORDER BY COALESCE(c.name, c.input_url)",
            (f_status,) if f_status else (),
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
    return render_template(
        "channels.html", chans=chans, niches=niches, agencies=agencies,
        f_status=f_status, scan=scraper.STATE,
    )


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


def _pageof(rows, arg, per=50):
    """Slice a result list to the page named by query arg. -> (slice, page, pages)"""
    try:
        p = max(int(request.args.get(arg, 1)), 1)
    except ValueError:
        p = 1
    pages = max((len(rows) + per - 1) // per, 1)
    p = min(p, pages)
    return rows[(p - 1) * per : p * per], p, pages


@app.route("/brands")
def brands():
    conn = db.connect()
    try:
        def brand_list(kind):
            return conn.execute(
                "SELECT b.*, "
                " (SELECT COUNT(*) FROM sponsorships s WHERE s.brand_key = b.brand_key) AS hits"
                " FROM brands b WHERE b.kind = ? ORDER BY b.name COLLATE NOCASE",
                (kind,),
            ).fetchall()

        known = brand_list("known")
        erroneous = brand_list("erroneous")
        boycott = brand_list("boycott")
        suggestions = conn.execute(
            "SELECT s.brand_key, MIN(s.brand) AS name, COUNT(*) AS n,"
            " COUNT(DISTINCT c.id) AS creators, MAX(v.upload_date) AS last_seen"
            " FROM sponsorships s JOIN videos v ON v.id = s.video_ref"
            " JOIN channels c ON c.id = v.channel_ref"
            " WHERE s.brand_key NOT IN (SELECT brand_key FROM brands)"
            " GROUP BY s.brand_key ORDER BY n DESC, last_seen DESC"
        ).fetchall()
        week_start = (dt.date.today() - dt.timedelta(days=6)).isoformat()
        recent = conn.execute(
            "SELECT s.brand_key, MIN(s.brand) AS name, COUNT(*) AS n,"
            " COUNT(DISTINCT c.id) AS creators, MAX(v.upload_date) AS last_seen"
            " FROM sponsorships s JOIN videos v ON v.id = s.video_ref"
            " JOIN channels c ON c.id = v.channel_ref"
            " WHERE s.brand_key NOT IN (SELECT brand_key FROM brands)"
            " AND v.upload_date >= ?"
            " GROUP BY s.brand_key ORDER BY last_seen DESC, n DESC",
            (week_start,),
        ).fetchall()
        # most active brands over the past month — includes known/boycott
        # (badged), excludes only erroneous junk
        month_start = (dt.date.today() - dt.timedelta(days=29)).isoformat()
        monthly = conn.execute(
            "SELECT s.brand_key, MIN(s.brand) AS name, COUNT(*) AS n,"
            " COUNT(DISTINCT c.id) AS creators, MAX(v.upload_date) AS last_seen,"
            " (SELECT kind FROM brands b WHERE b.brand_key = s.brand_key) AS kind"
            " FROM sponsorships s JOIN videos v ON v.id = s.video_ref"
            " JOIN channels c ON c.id = v.channel_ref"
            " WHERE s.brand_key NOT IN (SELECT brand_key FROM brands WHERE kind = 'erroneous')"
            " AND v.upload_date >= ?"
            " GROUP BY s.brand_key ORDER BY n DESC, last_seen DESC",
            (month_start,),
        ).fetchall()
        alias_rows = conn.execute(
            "SELECT alias_key, canonical FROM brand_aliases ORDER BY canonical COLLATE NOCASE"
        ).fetchall()
        review = conn.execute(
            "SELECT v.id, v.video_id, v.title, v.url, v.upload_date, v.sb_segments,"
            " v.review_note, c.name AS creator"
            " FROM videos v JOIN channels c ON c.id = v.channel_ref"
            " WHERE v.review = 'pending' ORDER BY v.upload_date DESC LIMIT 100"
        ).fetchall()
        review = [
            {**dict(r), "t": int(json.loads(r["sb_segments"] or "[[0,0]]")[0][0])}
            for r in review
        ]
    finally:
        conn.close()
    return render_template(
        "brands.html",
        suggestions=_pageof(suggestions, "p_sug"),
        known=_pageof(known, "p_known"),
        erroneous=_pageof(erroneous, "p_err"),
        boycott=_pageof(boycott, "p_boy"),
        recent=_pageof(recent, "p_rec", per=25),
        monthly=_pageof(monthly, "p_mon", per=25),
        alias_rows=alias_rows, review=review,
        page_url=lambda arg, p: url_for("brands", **{**request.args.to_dict(), arg: p}),
        scan=scraper.STATE,
    )


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
    kind = request.form.get("kind", "known")
    if kind not in ("known", "erroneous", "boycott"):
        kind = "known"
    if name:
        db.import_brand_lines(name, kind=kind)
        return _done(f"“{name}” marked as {kind}.", endpoint="brands")
    return redirect(url_for("brands"))


@app.route("/brands/rename", methods=["POST"])
def brands_rename():
    """Rename a detected/known brand everywhere. Renaming onto an existing
    brand's name consolidates the two (e.g. 'Opera Air' -> 'Opera')."""
    from tracker.detector import brand_key
    old_key = request.form.get("old_key", "")
    new_name = request.form.get("new_name", "").strip()[:60]
    new_key = brand_key(new_name)
    if not old_key or len(new_key) < 2:
        flash("That name is too short.", "err")
        return redirect(url_for("brands"))
    conn = db.connect()
    try:
        if new_key != old_key:
            # move sponsorship rows; drop ones that would duplicate an existing
            # (video, new brand) pair, then normalize the display name
            conn.execute(
                "UPDATE OR IGNORE sponsorships SET brand = ?, brand_key = ? WHERE brand_key = ?",
                (new_name, new_key, old_key),
            )
            conn.execute("DELETE FROM sponsorships WHERE brand_key = ?", (old_key,))
            # unify the display name on rows that already carried the target key
            conn.execute("UPDATE sponsorships SET brand = ? WHERE brand_key = ?", (new_name, new_key))
            conn.execute(
                "UPDATE OR IGNORE brands SET name = ?, brand_key = ? WHERE brand_key = ?",
                (new_name, new_key, old_key),
            )
            conn.execute("DELETE FROM brands WHERE brand_key = ?", (old_key,))
            # remember the consolidation so future scans map the variant
            # straight to the canonical name
            conn.execute(
                "INSERT INTO brand_aliases (alias_key, canonical) VALUES (?, ?)"
                " ON CONFLICT(alias_key) DO UPDATE SET canonical = excluded.canonical",
                (old_key, new_name),
            )
            # re-point aliases that previously resolved to the old name
            # (A→B then B→C should leave A→C, not a dangling chain)
            for r in conn.execute("SELECT alias_key, canonical FROM brand_aliases").fetchall():
                if r["alias_key"] != old_key and brand_key(r["canonical"]) == old_key:
                    conn.execute(
                        "UPDATE brand_aliases SET canonical = ? WHERE alias_key = ?",
                        (new_name, r["alias_key"]),
                    )
        else:
            conn.execute("UPDATE sponsorships SET brand = ? WHERE brand_key = ?", (new_name, old_key))
            conn.execute("UPDATE brands SET name = ? WHERE brand_key = ?", (new_name, old_key))
        conn.commit()
    finally:
        conn.close()
    return _done(f"Renamed to “{new_name}” — matching entries were consolidated.", endpoint="brands")


@app.route("/creator/<int:cid>")
def creator_detail(cid):
    conn = db.connect()
    try:
        ch = conn.execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
        if not ch:
            flash("Unknown creator.", "err")
            return redirect(url_for("channels"))
        # adjusted average: last 12 videos with view data, drop the single
        # highest- and lowest-viewed (viral spikes / flops), average the rest;
        # engagement rate uses the same trimmed set
        recent = conn.execute(
            "SELECT view_count, COALESCE(like_count, 0) AS likes,"
            " COALESCE(comment_count, 0) AS comments FROM videos"
            " WHERE channel_ref = ? AND view_count IS NOT NULL AND view_count > 0"
            " ORDER BY upload_date DESC LIMIT 12",
            (cid,),
        ).fetchall()
        trimmed = sorted(recent, key=lambda r: r["view_count"])[1:-1] if len(recent) >= 3 else recent
        views_sum = sum(r["view_count"] for r in trimmed)
        stats = {
            "n": len(trimmed),
            "avg_views": views_sum / len(trimmed) if trimmed else None,
            "engagement": (
                sum(r["likes"] + r["comments"] for r in trimmed) * 100.0 / views_sum
                if views_sum else None
            ),
        }
        cadence = conn.execute(
            "SELECT COUNT(*) / 3.0 FROM videos WHERE channel_ref = ? AND upload_date >= ?",
            (cid, (dt.date.today() - dt.timedelta(days=90)).isoformat()),
        ).fetchone()[0]
        brands = conn.execute(
            "SELECT s.brand_key, MIN(s.brand) AS name, COUNT(*) AS n, MAX(v.upload_date) AS last_seen"
            " FROM sponsorships s JOIN videos v ON v.id = s.video_ref"
            " WHERE v.channel_ref = ? GROUP BY s.brand_key ORDER BY n DESC, last_seen DESC",
            (cid,),
        ).fetchall()
        months = conn.execute(
            "SELECT substr(v.upload_date, 1, 7) AS month, COUNT(*) AS n"
            " FROM sponsorships s JOIN videos v ON v.id = s.video_ref"
            " WHERE v.channel_ref = ? AND v.upload_date IS NOT NULL"
            " GROUP BY month ORDER BY month DESC LIMIT 12",
            (cid,),
        ).fetchall()[::-1]
        videos = conn.execute(
            "SELECT v.*, (SELECT GROUP_CONCAT(s.brand, ', ') FROM sponsorships s"
            "  WHERE s.video_ref = v.id) AS sponsors"
            " FROM videos v WHERE v.channel_ref = ? ORDER BY v.upload_date DESC LIMIT 25",
            (cid,),
        ).fetchall()
    finally:
        conn.close()

    # plain-text media kit for copy-paste into emails
    fmt = lambda x: f"{int(x):,}" if x else "—"
    eng = stats["engagement"]
    lines = [
        f"{ch['name'] or ch['input_url']}",
        f"Channel: {ch['input_url']}",
        f"Niche: {ch['niche'] or '—'}" + (f" / {ch['subniche']}" if ch["subniche"] else ""),
        f"Subscribers: {fmt(ch['subscribers'])}",
        f"Average views (adjusted, {stats['n'] or 0} recent videos): {fmt(stats['avg_views'])}",
        "Engagement rate: " + (f"{eng:.1f}%" if eng else "—"),
        "Uploads per month: " + (f"{cadence:.1f}" if cadence else "—"),
        f"Integration rate: {ch['rate_integration'] or '—'}",
        f"Dedicated video rate: {ch['rate_dedicated'] or '—'}",
    ]
    if ch["demo_gender"]: lines.append(f"Audience gender: {ch['demo_gender']}")
    if ch["demo_geo"]: lines.append(f"Top geographies: {ch['demo_geo']}")
    if ch["demo_age"]: lines.append(f"Audience age: {ch['demo_age']}")
    if brands:
        lines.append("Recent sponsors: " + ", ".join(b["name"] for b in brands[:8]))
    email_text = "\n".join(lines)

    return render_template(
        "creator.html", ch=ch, stats=stats, cadence=cadence, brands=brands,
        months=months, month_max=max((m["n"] for m in months), default=0),
        videos=videos, email_text=email_text, images=_creator_images(cid),
        scan=scraper.STATE,
    )


@app.route("/creator/<int:cid>/images", methods=["POST"])
def creator_images_upload(cid):
    files = request.files.getlist("images")
    saved = skipped = 0
    os.makedirs(_creator_dir(cid), exist_ok=True)
    for f in files:
        if not f or not f.filename:
            continue
        name = secure_filename(f.filename)
        if os.path.splitext(name)[1].lower() not in IMAGE_EXTS:
            skipped += 1
            continue
        path = os.path.join(_creator_dir(cid), name)
        base, ext = os.path.splitext(name)
        n = 1
        while os.path.exists(path):  # keep both copies on name collisions
            path = os.path.join(_creator_dir(cid), f"{base}-{n}{ext}")
            n += 1
        f.save(path)
        saved += 1
    flash(f"Uploaded {saved} image(s)" + (f", skipped {skipped} (not an image)" if skipped else "") + ".",
          "ok" if saved else "err")
    return redirect(url_for("creator_detail", cid=cid))


@app.route("/uploads/<int:cid>/<path:filename>")
def creator_image(cid, filename):
    name = secure_filename(filename)
    if os.path.splitext(name)[1].lower() not in IMAGE_EXTS:
        abort(404)
    return send_from_directory(_creator_dir(cid), name)


@app.route("/creator/<int:cid>/images/<path:filename>/delete", methods=["POST"])
def creator_image_delete(cid, filename):
    name = secure_filename(filename)
    path = os.path.join(_creator_dir(cid), name)
    if os.path.isfile(path):
        os.remove(path)
    return _done("Image removed.", endpoint="channels")


@app.route("/creator/<int:cid>/kit", methods=["POST"])
def creator_kit(cid):
    fields = ("rate_integration", "rate_dedicated", "demo_gender", "demo_geo", "demo_age", "notes")
    vals = [request.form.get(f, "").strip()[:200] or None for f in fields]
    conn = db.connect()
    try:
        conn.execute(
            f"UPDATE channels SET {', '.join(f'{f} = ?' for f in fields)} WHERE id = ?",
            (*vals, cid),
        )
        conn.commit()
    finally:
        conn.close()
    return _done("Media kit saved.", endpoint="channels")


@app.route("/brand/<key>")
def brand_detail(key):
    conn = db.connect()
    try:
        head = conn.execute(
            "SELECT MIN(s.brand) AS name, COUNT(*) AS total, COUNT(DISTINCT c.id) AS creators,"
            " MIN(v.upload_date) AS first_seen, MAX(v.upload_date) AS last_seen"
            " FROM sponsorships s JOIN videos v ON v.id = s.video_ref"
            " JOIN channels c ON c.id = v.channel_ref WHERE s.brand_key = ?",
            (key,),
        ).fetchone()
        if not head["name"]:
            flash("No sponsorships recorded for that brand.", "err")
            return redirect(url_for("brands"))
        kind_row = conn.execute("SELECT kind FROM brands WHERE brand_key = ?", (key,)).fetchone()
        months = conn.execute(
            "SELECT substr(v.upload_date, 1, 7) AS month, COUNT(*) AS n"
            " FROM sponsorships s JOIN videos v ON v.id = s.video_ref"
            " WHERE s.brand_key = ? AND v.upload_date IS NOT NULL"
            " GROUP BY month ORDER BY month DESC LIMIT 12",
            (key,),
        ).fetchall()[::-1]
        creators = conn.execute(
            "SELECT c.id, c.name, c.status, c.agency, COUNT(*) AS n, MAX(v.upload_date) AS last_seen"
            " FROM sponsorships s JOIN videos v ON v.id = s.video_ref"
            " JOIN channels c ON c.id = v.channel_ref WHERE s.brand_key = ?"
            " GROUP BY c.id ORDER BY n DESC, last_seen DESC",
            (key,),
        ).fetchall()
        videos = conn.execute(
            "SELECT s.id AS sid, v.title, v.url, v.upload_date, c.name AS creator, s.evidence"
            " FROM sponsorships s JOIN videos v ON v.id = s.video_ref"
            " JOIN channels c ON c.id = v.channel_ref WHERE s.brand_key = ?"
            " ORDER BY v.upload_date DESC LIMIT 50",
            (key,),
        ).fetchall()
        aliases = [
            r["alias_key"] for r in conn.execute("SELECT alias_key, canonical FROM brand_aliases")
            if brand_key(r["canonical"]) == key
        ]
    finally:
        conn.close()
    return render_template(
        "brand.html", key=key, head=head, kind=kind_row["kind"] if kind_row else None,
        months=months, month_max=max((m["n"] for m in months), default=0),
        creators=creators, videos=videos, aliases=aliases, scan=scraper.STATE,
    )


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


@app.route("/brands/<int:bid>/delete", methods=["POST"])
def brands_delete(bid):
    conn = db.connect()
    try:
        conn.execute("DELETE FROM brands WHERE id = ?", (bid,))
        conn.commit()
    finally:
        conn.close()
    return _done("Brand removed — it may reappear as a suggestion.", endpoint="brands")


@app.route("/channels/<int:cid>/delete", methods=["POST"])
def channels_delete(cid):
    conn = db.connect()
    try:
        conn.execute("DELETE FROM channels WHERE id = ?", (cid,))
        conn.commit()
    finally:
        conn.close()
    return _done("Channel removed (its videos and sponsorships too).", endpoint="channels")


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
    return _done("Status updated.", endpoint="channels")


@app.route("/scan/status")
def scan_status():
    return jsonify(scraper.STATE)


if __name__ == "__main__":
    app.run(debug=False, port=5000)
