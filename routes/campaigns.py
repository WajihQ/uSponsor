"""Campaigns — Instantly sending-forecast view."""
from flask import jsonify, render_template

from app_core import app
from tracker import instantly, scraper


@app.route("/campaigns")
def campaigns_page():
    data, error = None, None
    schedule, schedule_error = None, None
    if instantly.configured():
        try:
            data = instantly.sending_forecast(6)
        except Exception as e:
            error = str(e)
        try:
            schedule = instantly.today_schedule_by_account()
        except Exception as e:
            schedule_error = str(e)
    return render_template("campaigns.html", data=data, error=error,
                           schedule=schedule, schedule_error=schedule_error,
                           configured=instantly.configured(), scan=scraper.STATE)


@app.route("/campaigns/schedule")
def campaigns_schedule_json():
    """AJAX refresh for the today's-schedule cards — re-pulls Instantly live
    so a campaign that went inactive or fell off today's days just drops out."""
    if not instantly.configured():
        return jsonify({"error": "Not connected to Instantly."}), 400
    try:
        schedule = instantly.today_schedule_by_account()
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"schedule": schedule})
