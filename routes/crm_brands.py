"""Brand CRM — outreach tracking over the brand_leads table."""
from flask import flash, jsonify, redirect, render_template, request, url_for

from app_core import app
from routes.helpers import _done, _status_options
from tracker import db, scraper

# fields the inline row editor may write
_BRAND_EDIT = {"person", "brand", "niche", "linkedin", "role", "email", "country",
               "location", "comments", "status", "influencers", "first_contacted",
               "last_contacted"}
# fallback status vocab for an empty DB; real values (from the imported sheet)
# always take precedence and keep their own casing so rows match the dropdown
_BRAND_STATUS_DEFAULTS = ["In Talks", "Interested", "Hard rejection", "Not interested", "Closed"]

_BRAND_SORTS = {
    "stale": "last_contacted IS NULL, last_contacted ASC",
    "recent": "last_contacted IS NULL, last_contacted DESC",
    "brand": "brand COLLATE NOCASE",
    "person": "person COLLATE NOCASE",
}


@app.route("/crm/brands")
def crm_brands():
    f_status = request.args.get("status", "")
    f_niche = request.args.get("niche", "")
    q = request.args.get("q", "").strip()
    sort = request.args.get("sort", "stale")
    if sort not in _BRAND_SORTS:
        sort = "stale"
    conds, args = [], []
    if f_status:
        conds.append("COALESCE(status, '') = ?"); args.append(f_status)
    if f_niche:
        conds.append("COALESCE(niche, '') = ?"); args.append(f_niche)
    if q:
        conds.append("(person LIKE ? OR brand LIKE ? OR email LIKE ? OR comments LIKE ?)")
        args += [f"%{q}%"] * 4
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM brand_leads" + where + " ORDER BY " + _BRAND_SORTS[sort], args
        ).fetchall()
        statuses = [r[0] for r in conn.execute(
            "SELECT DISTINCT status FROM brand_leads WHERE status IS NOT NULL"
            " AND status != '' ORDER BY status")]
        niches = [r[0] for r in conn.execute(
            "SELECT DISTINCT niche FROM brand_leads WHERE niche IS NOT NULL"
            " AND niche != '' ORDER BY niche")]
        counts = {
            "total": len(rows),
            "emailed": sum(1 for r in rows if r["first_contacted"]),
            "no_email": sum(1 for r in rows if not r["email"]),
        }
        cgmap = db.country_group_map(conn)
        regions = [r["region"] for r in conn.execute(
            "SELECT DISTINCT group_name AS region FROM country_groups ORDER BY group_name")]
        groups = conn.execute(
            "SELECT * FROM country_groups ORDER BY group_name COLLATE NOCASE, country COLLATE NOCASE"
        ).fetchall()
    finally:
        conn.close()
    row_region = {b["id"]: ", ".join(cgmap.get(b["country"] or "", [])) for b in rows}
    region_countries = {}
    for g in groups:
        region_countries.setdefault(g["group_name"], []).append(g["country"])
    return render_template(
        "crm_brands.html", rows=rows, statuses=statuses, niches=niches, counts=counts,
        status_options=_status_options(statuses, _BRAND_STATUS_DEFAULTS),
        region_countries=region_countries,
        f_status=f_status, f_niche=f_niche, q=q, sort=sort, scan=scraper.STATE,
        row_region=row_region, regions=regions, groups=groups,
    )


@app.route("/crm/brands/groups/add", methods=["POST"])
def crm_brand_groups_add():
    group_name = request.form.get("group_name", "").strip()[:80]
    country = request.form.get("country", "").strip()[:80]
    is_fetch = request.headers.get("X-Requested-With") == "fetch"
    if not group_name or not country:
        if is_fetch:
            return jsonify({"error": "Give both a group name and a country."}), 400
        flash("Give both a group name and a country.", "err")
        return redirect(url_for("crm_brands"))
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO country_groups (group_name, country) VALUES (?, ?)"
            " ON CONFLICT(group_name, country) DO NOTHING",
            (group_name, country),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM country_groups WHERE group_name = ? AND country = ?",
            (group_name, country),
        ).fetchone()
    finally:
        conn.close()
    if is_fetch:
        # the page updates itself in place (a chip, not a reload) -- needs
        # the row id and the (possibly re-cased-by-someone-else) names back
        return jsonify({"id": row["id"], "group_name": group_name, "country": country})
    flash(f"Added {country} to {group_name}.", "ok")
    return redirect(url_for("crm_brands"))


@app.route("/crm/brands/groups/<int:gid>/delete", methods=["POST"])
def crm_brand_groups_delete(gid):
    conn = db.connect()
    try:
        conn.execute("DELETE FROM country_groups WHERE id = ?", (gid,))
        conn.commit()
    finally:
        conn.close()
    return _done("Removed.", endpoint="crm_brands")


@app.route("/crm/brands/import", methods=["POST"])
def crm_brands_import():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("No file selected.", "err")
        return redirect(url_for("crm_brands"))
    added, updated, skipped = db.import_brand_leads_csv(f.read().decode("utf-8", errors="replace"))
    flash(f"Imported {added} new brand lead(s), enriched {updated} existing,"
          f" skipped {skipped} (blank / nothing new).", "ok")
    return redirect(url_for("crm_brands"))


@app.route("/crm/brands/add", methods=["POST"])
def crm_brands_add():
    if not (request.form.get("person", "").strip() or request.form.get("brand", "").strip()):
        flash("Give at least a person or brand.", "err")
        return redirect(url_for("crm_brands"))
    cols, vals = [], []
    for col in _BRAND_EDIT:
        v = request.form.get(col, "").strip()[:400]
        if v:
            cols.append(col); vals.append(v)
    conn = db.connect()
    try:
        placeholders = ", ".join("?" for _ in cols)
        conn.execute(
            f"INSERT INTO brand_leads ({', '.join(cols)}) VALUES ({placeholders})", vals
        )
        brand = request.form.get("brand", "").strip()
        if brand:
            db.ensure_known_brand(conn, brand)
        conn.commit()
    finally:
        conn.close()
    flash("Brand lead added.", "ok")
    return redirect(url_for("crm_brands"))


@app.route("/crm/brands/<int:bid>/edit", methods=["POST"])
def crm_brands_edit(bid):
    sets, args = [], []
    for col in _BRAND_EDIT:
        if col in request.form:
            val = request.form.get(col, "").strip()[:400] or None
            sets.append(f"{col} = ?"); args.append(val)
    if not sets:
        return _done("Nothing to save.", endpoint="crm_brands")
    conn = db.connect()
    try:
        conn.execute(f"UPDATE brand_leads SET {', '.join(sets)} WHERE id = ?", (*args, bid))
        if request.form.get("brand", "").strip():
            db.ensure_known_brand(conn, request.form["brand"].strip())
        conn.commit()
    finally:
        conn.close()
    return _done("Saved.", endpoint="crm_brands")


@app.route("/crm/brands/<int:bid>/delete", methods=["POST"])
def crm_brands_delete(bid):
    conn = db.connect()
    try:
        conn.execute("DELETE FROM brand_leads WHERE id = ?", (bid,))
        conn.commit()
    finally:
        conn.close()
    return _done("Brand lead removed.", endpoint="crm_brands")
