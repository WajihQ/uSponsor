"""Creator detail page — stats, video tables (long-form/Shorts split), media
kit, and the profile image gallery."""
import datetime as dt
import os

from flask import abort, flash, redirect, render_template, request, send_from_directory, url_for
from werkzeug.utils import secure_filename

from app_core import IMAGE_EXTS, UPLOAD_DIR, app
from routes.helpers import _done
from tracker import db, scraper


def _creator_dir(cid):
    return os.path.join(UPLOAD_DIR, str(int(cid)))


def _creator_images(cid):
    d = _creator_dir(cid)
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d) if os.path.splitext(f)[1].lower() in IMAGE_EXTS)


def _video_page(conn, cid, cond, page_arg):
    """One page of a channel's videos matching `cond` (a raw SQL fragment on
    the videos table — used to split long-form and Shorts into separate,
    independently paginated tables on the creator page). Returns
    (rows, page, pages, total)."""
    per = 25
    try:
        page = max(int(request.args.get(page_arg, 1)), 1)
    except ValueError:
        page = 1
    NOT_ERR = " AND s.brand_key NOT IN (SELECT brand_key FROM brands WHERE kind = 'erroneous')"
    total = conn.execute(
        f"SELECT COUNT(*) FROM videos WHERE channel_ref = ? AND {cond}", (cid,)
    ).fetchone()[0]
    pages = max((total + per - 1) // per, 1)
    page = min(page, pages)
    rows = conn.execute(
        "SELECT v.*, (SELECT GROUP_CONCAT(s.brand, ', ') FROM sponsorships s"
        "  WHERE s.video_ref = v.id" + NOT_ERR + ") AS sponsors"
        f" FROM videos v WHERE v.channel_ref = ? AND {cond}"
        " ORDER BY v.upload_date DESC LIMIT ? OFFSET ?",
        (cid, per, (page - 1) * per),
    ).fetchall()
    return rows, page, pages, total


def _channel_stats(conn, cid, short):
    """Adjusted average views + engagement rate for one channel's long-form
    videos (short=False) or Shorts (short=True), computed independently so a
    creator's Shorts volume/virality never dilutes their long-form numbers
    or vice versa. Same trimmed-mean rule as before: last 12 videos with view
    data for that bucket, drop the single highest- and lowest-viewed (viral
    spikes / flops), average the rest; engagement rate uses the same set.
    """
    cond = "is_short = 1" if short else "COALESCE(is_short, 0) = 0"
    recent = conn.execute(
        "SELECT view_count, COALESCE(like_count, 0) AS likes,"
        " COALESCE(comment_count, 0) AS comments FROM videos"
        " WHERE channel_ref = ? AND view_count IS NOT NULL AND view_count > 0"
        f" AND {cond} ORDER BY upload_date DESC LIMIT 12",
        (cid,),
    ).fetchall()
    trimmed = sorted(recent, key=lambda r: r["view_count"])[1:-1] if len(recent) >= 3 else recent
    views_sum = sum(r["view_count"] for r in trimmed)
    return {
        "n": len(trimmed),
        "avg_views": views_sum / len(trimmed) if trimmed else None,
        "engagement": (
            sum(r["likes"] + r["comments"] for r in trimmed) * 100.0 / views_sum
            if views_sum else None
        ),
    }


@app.route("/creator/<int:cid>")
def creator_detail(cid):
    conn = db.connect()
    try:
        ch = conn.execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
        if not ch:
            flash("Unknown creator.", "err")
            return redirect(url_for("channels"))
        stats = _channel_stats(conn, cid, short=False)
        stats_shorts = _channel_stats(conn, cid, short=True)
        has_shorts = conn.execute(
            "SELECT 1 FROM videos WHERE channel_ref = ? AND is_short = 1 LIMIT 1", (cid,)
        ).fetchone() is not None
        cadence = conn.execute(
            "SELECT COUNT(*) / 3.0 FROM videos WHERE channel_ref = ? AND upload_date >= ?",
            (cid, (dt.date.today() - dt.timedelta(days=90)).isoformat()),
        ).fetchone()[0]
        # everywhere here, hide junk detections flagged 'erroneous' in the CRM
        NOT_ERR = " AND s.brand_key NOT IN (SELECT brand_key FROM brands WHERE kind = 'erroneous')"
        brands = conn.execute(
            "SELECT s.brand_key, MIN(s.brand) AS name, COUNT(*) AS n, MAX(v.upload_date) AS last_seen"
            " FROM sponsorships s JOIN videos v ON v.id = s.video_ref"
            " WHERE v.channel_ref = ?" + NOT_ERR + " GROUP BY s.brand_key ORDER BY n DESC, last_seen DESC",
            (cid,),
        ).fetchall()
        months = conn.execute(
            "SELECT substr(v.upload_date, 1, 7) AS month, COUNT(*) AS n"
            " FROM sponsorships s JOIN videos v ON v.id = s.video_ref"
            " WHERE v.channel_ref = ? AND v.upload_date IS NOT NULL" + NOT_ERR +
            " GROUP BY month ORDER BY month DESC LIMIT 12",
            (cid,),
        ).fetchall()[::-1]
        videos, vpage, vpages, vtotal = _video_page(conn, cid, "COALESCE(is_short, 0) = 0", "vpage")
        videos_shorts, spage, spages, stotal = _video_page(conn, cid, "is_short = 1", "spage")
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
        "creator.html", ch=ch, stats=stats, stats_shorts=stats_shorts, has_shorts=has_shorts,
        cadence=cadence, brands=brands,
        months=months, month_max=max((m["n"] for m in months), default=0),
        videos=videos, videos_shorts=videos_shorts, email_text=email_text, images=_creator_images(cid),
        vpage=vpage, vpages=vpages, vtotal=vtotal,
        spage=spage, spages=spages, stotal=stotal, scan=scraper.STATE,
    )


@app.route("/creator/<int:cid>/video/<int:vid>/sponsors", methods=["POST"])
def creator_video_sponsors(cid, vid):
    """Set a video's sponsors from a comma-separated list (profile edit). Replaces
    that video's sponsorships with the entered brands (alias-normalized)."""
    names = [n.strip() for n in request.form.get("sponsors", "").replace(";", ",").split(",") if n.strip()]
    conn = db.connect()
    try:
        amap = db.alias_map(conn)
        conn.execute("DELETE FROM sponsorships WHERE video_ref = ?", (vid,))
        for name in names:
            nm, key = db.apply_alias(name, amap)
            if len(key) < 2:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO sponsorships (video_ref, brand, brand_key, evidence)"
                " VALUES (?, ?, ?, 'manual: set on creator profile')",
                (vid, nm, key),
            )
        conn.commit()
    finally:
        conn.close()
    return _done("Sponsors updated.", endpoint="creator_detail")


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
