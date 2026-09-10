"""The Brands tab — sponsor-brand tracking (suggestions/known/erroneous/
boycott lists, aliases, the sponsor-segment review queue) plus the
per-brand detail page."""
import datetime as dt
import json

from flask import flash, redirect, render_template, request, url_for

from app_core import app
from routes.helpers import _done
from tracker import db, scraper
from tracker.detector import brand_key


def _pageof(rows, arg, per=50):
    """Slice a result list to the page named by query arg. -> (slice, page, pages)"""
    try:
        p = max(int(request.args.get(arg, 1)), 1)
    except ValueError:
        p = 1
    pages = max((len(rows) + per - 1) // per, 1)
    p = min(p, pages)
    return rows[(p - 1) * per : p * per], p, pages


def _search(rows, arg, field="name"):
    """Filter rows to those whose `field` contains the query-string `arg` (case-insensitive)."""
    term = request.args.get(arg, "").strip().lower()
    if not term:
        return rows
    return [r for r in rows if term in (r[field] or "").lower()]


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
        if q := request.args.get("q_alias", "").strip().lower():
            alias_rows = [
                a for a in alias_rows
                if q in a["alias_key"].lower() or q in a["canonical"].lower()
            ]
    finally:
        conn.close()
    return render_template(
        "brands.html",
        suggestions=_pageof(_search(suggestions, "q_sug"), "p_sug"),
        known=_pageof(_search(known, "q_known"), "p_known"),
        erroneous=_pageof(_search(erroneous, "q_err"), "p_err"),
        boycott=_pageof(_search(boycott, "q_boy"), "p_boy"),
        recent=_pageof(_search(recent, "q_rec"), "p_rec", per=25),
        monthly=_pageof(_search(monthly, "q_mon"), "p_mon", per=25),
        alias_rows=alias_rows, review=review,
        page_url=lambda arg, p: url_for("brands", **{**request.args.to_dict(), arg: p}),
        clear_url=lambda qarg, parg: url_for(
            "brands", **{k: v for k, v in request.args.to_dict().items() if k not in (qarg, parg)}
        ),
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


@app.route("/brands/<int:bid>/delete", methods=["POST"])
def brands_delete(bid):
    conn = db.connect()
    try:
        conn.execute("DELETE FROM brands WHERE id = ?", (bid,))
        conn.commit()
    finally:
        conn.close()
    return _done("Brand removed — it may reappear as a suggestion.", endpoint="brands")
