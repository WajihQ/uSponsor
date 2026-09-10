"""Dashboard — the `/` landing page. One shared filter condition drives every
widget on the page (table, heatmap, week grid, stat counters)."""
import datetime as dt

from flask import render_template, request, url_for

from app_core import app
from tracker import db, scraper

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
