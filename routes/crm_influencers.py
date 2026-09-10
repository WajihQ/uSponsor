"""Influencer CRM — outreach tracking over the channels table."""
import datetime as dt

from flask import flash, redirect, render_template, request, url_for

from app_core import app
from routes.helpers import _done, _status_options
from tracker import db, scraper

# fields the inline row editor may write
_INFL_EDIT = {"name", "email", "instagram", "revisit_later", "date_found",
              "crm_status", "first_contacted", "last_contacted", "niche",
              "subniche", "agency", "notes"}
# fallback status vocab for an empty DB; real values (from the imported sheet)
# always take precedence and keep their own casing so rows match the dropdown
_INFL_STATUS_DEFAULTS = ["Wait", "Soft Rejection", "Hard rejection",
                         "Ghosted after Reply", "I ghosted them", "Closed"]

_INFL_SORTS = {
    "stale": "last_contacted IS NULL, last_contacted ASC",   # follow-ups first
    "recent": "last_contacted IS NULL, last_contacted DESC",
    "name": "COALESCE(name, input_url) COLLATE NOCASE",
    "found": "date_found DESC",
}


def _ago(ts):
    """A timestamp string -> friendly relative age ('today', '3d ago')."""
    if not ts:
        return None
    try:
        d = dt.datetime.strptime(str(ts)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    n = (dt.date.today() - d).days
    if n <= 0:
        return "today"
    if n == 1:
        return "1d ago"
    if n < 30:
        return f"{n}d ago"
    if n < 365:
        return f"{n // 30}mo ago"
    return f"{n // 365}y ago"


def _compact(n):
    """1_463_234 -> '1.5M', 32_607 -> '33K'."""
    n = float(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(int(n))


@app.route("/crm/influencers")
def crm_influencers():
    f_status = request.args.get("status", "")
    f_revisit = request.args.get("revisit", "")
    q = request.args.get("q", "").strip()
    sort = request.args.get("sort", "stale")
    if sort not in _INFL_SORTS:
        sort = "stale"
    conds, args = [], []
    if f_status:
        conds.append("COALESCE(crm_status, '') = ?"); args.append(f_status)
    if f_revisit:
        conds.append("COALESCE(revisit_later, '') = ?"); args.append(f_revisit)
    if q:
        conds.append("(name LIKE ? OR input_url LIKE ? OR email LIKE ? OR notes LIKE ?)")
        args += [f"%{q}%"] * 4
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM sponsorships s JOIN videos v"
            "  ON v.id = s.video_ref WHERE v.channel_ref = c.id) AS spons"
            " FROM channels c" + where + " ORDER BY " + _INFL_SORTS[sort],
            args,
        ).fetchall()
        statuses = [r[0] for r in conn.execute(
            "SELECT DISTINCT crm_status FROM channels WHERE crm_status IS NOT NULL"
            " AND crm_status != '' ORDER BY crm_status")]
        counts = {
            "total": len(rows),
            "emailed": sum(1 for r in rows if r["first_contacted"]),
            "no_email": sum(1 for r in rows if not r["email"]),
        }
        # adjusted average views per channel (same rule as the profile page:
        # newest 12 videos with view data, drop the single highest+lowest)
        by = {}
        for r in conn.execute(
            "SELECT channel_ref, view_count FROM videos"
            " WHERE view_count IS NOT NULL AND view_count > 0 AND COALESCE(is_short, 0) = 0"
            " ORDER BY channel_ref, upload_date DESC"
        ):
            by.setdefault(r["channel_ref"], []).append(r["view_count"])
        avg_views = {}
        for ch_id, vals in by.items():
            vals = vals[:12]
            trimmed = sorted(vals)[1:-1] if len(vals) >= 3 else vals
            if trimmed:
                avg_views[ch_id] = _compact(sum(trimmed) / len(trimmed))
        scanned = {r["id"]: _ago(r["last_scanned"]) for r in rows}
    finally:
        conn.close()
    return render_template(
        "crm_influencers.html", rows=rows, statuses=statuses, counts=counts,
        avg_views=avg_views, scanned=scanned, cookies=scraper.cookies_active(),
        cookies_broken=scraper.cookies_broken(),
        status_options=_status_options(statuses, _INFL_STATUS_DEFAULTS),
        f_status=f_status, f_revisit=f_revisit, q=q, sort=sort, scan=scraper.STATE,
    )


@app.route("/crm/influencers/import", methods=["POST"])
def crm_influencers_import():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("No file selected.", "err")
        return redirect(url_for("crm_influencers"))
    added, updated, skipped = db.import_influencer_csv(f.read().decode("utf-8", errors="replace"))
    flash(f"Imported {added} new influencer(s), enriched {updated} existing,"
          f" skipped {skipped} (no link / nothing new).", "ok")
    return redirect(url_for("crm_influencers"))


@app.route("/crm/influencers/add", methods=["POST"])
def crm_influencers_add():
    link = request.form.get("link", "").strip()
    yt = db.normalize_channel_url(link)
    ig = None if yt else db.normalize_instagram_url(link)   # allow Instagram-only creators
    if not yt and not ig:
        flash("Enter a YouTube channel URL / @handle, or an Instagram profile link.", "err")
        return redirect(url_for("crm_influencers"))
    conn = db.connect()
    try:
        if yt:
            db.add_channel(link)                            # normalizes to YouTube, dedups
            input_url = yt
        else:
            input_url = ig                                  # IG-only: not scanned, stored as the key
            if not conn.execute("SELECT 1 FROM channels WHERE input_url = ?", (input_url,)).fetchone():
                conn.execute("INSERT INTO channels (input_url, instagram) VALUES (?, ?)",
                             (input_url, input_url))
                conn.commit()
        cid = conn.execute("SELECT id FROM channels WHERE input_url = ?", (input_url,)).fetchone()["id"]
        sets, args = [], []
        for col in _INFL_EDIT:                              # name/email/etc from the add row
            if request.form.get(col, "").strip():
                sets.append(f"{col} = ?"); args.append(request.form.get(col).strip()[:400])
        if sets:
            conn.execute(f"UPDATE channels SET {', '.join(sets)} WHERE id = ?", (*args, cid))
        conn.commit()
    finally:
        conn.close()
    flash("Influencer added.", "ok")
    return redirect(url_for("crm_influencers"))


@app.route("/crm/influencers/<int:cid>/edit", methods=["POST"])
def crm_influencers_edit(cid):
    sets, args = [], []
    for col in _INFL_EDIT:
        if col in request.form:
            val = request.form.get(col, "").strip()[:400] or None
            sets.append(f"{col} = ?"); args.append(val)
    if not sets:
        return _done("Nothing to save.", endpoint="crm_influencers")
    conn = db.connect()
    try:
        conn.execute(f"UPDATE channels SET {', '.join(sets)} WHERE id = ?", (*args, cid))
        conn.commit()
    finally:
        conn.close()
    return _done("Saved.", endpoint="crm_influencers")
