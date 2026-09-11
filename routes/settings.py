"""Settings page + Gmail/Instantly sync trigger and status routes."""
import os

from flask import flash, jsonify, redirect, render_template, request, session, url_for

from app_core import app
from routes.helpers import _done
from tracker import gmail_sync, instantly, scraper, youtube_api


@app.route("/settings")
def settings():
    return render_template(
        "settings.html", scan=scraper.STATE,
        gmail=gmail_sync.status(), instantly=instantly.status(),
        youtube_api={
            "configured": youtube_api.configured(),
            "quota_exhausted_until": youtube_api.quota_exhausted_until(),
        },
    )


@app.route("/crm/gmail/sync", methods=["POST"])
def gmail_sync_now():
    started = gmail_sync.start_sync_in_background(authoritative=False)
    return _done("Gmail sync started." if started else "A sync is already running.",
                 "ok" if started else "err", endpoint="settings")


@app.route("/crm/gmail/resync", methods=["POST"])
def gmail_resync():
    started = gmail_sync.start_sync_in_background(authoritative=True)
    return _done("Full Gmail resync started — reads all sent mail." if started
                 else "A sync is already running.",
                 "ok" if started else "err", endpoint="settings")


@app.route("/crm/gmail/status")
def gmail_status():
    return jsonify(gmail_sync.status())


@app.route("/crm/gmail/connect")
def gmail_connect():
    """Start the in-app connect/reconnect flow — redirects to Google.
    ?account=<email> pre-selects that account in Google's picker, for
    reconnecting one specific dead token (the Settings page's per-account
    Reconnect link); omitted for connecting a brand-new account."""
    try:
        url, state = gmail_sync.build_auth_url(
            request.url_root, login_hint=request.args.get("account") or None)
    except RuntimeError as e:
        flash(str(e), "err")
        return redirect(url_for("settings"))
    session["gmail_oauth_state"] = state
    return redirect(url)


@app.route("/crm/gmail/oauth/callback")
def gmail_oauth_callback():
    expected = session.pop("gmail_oauth_state", None)
    got = request.args.get("state")
    if not expected or got != expected:
        flash("Gmail connect link expired or was already used — try again.", "err")
        return redirect(url_for("settings"))
    if request.args.get("error"):
        flash(f"Google sign-in was cancelled ({request.args['error']}).", "err")
        return redirect(url_for("settings"))
    try:
        address = gmail_sync.finish_oauth(request.url_root, request.url, expected)
    except Exception as e:
        flash(f"Couldn't finish connecting: {e}", "err")
        return redirect(url_for("settings"))
    flash(f"Connected {address}.", "ok")
    return redirect(url_for("settings"))


@app.route("/cron/gmail-sync", methods=["POST"])
def cron_gmail_sync():
    """For an external scheduler (GitHub Actions cron, cron-job.org, etc.) to
    hit on an interval once hosted -- see SETUP_HOSTING.md. Replaces the old
    in-process interval thread, which never ran under gunicorn anyway.
    Runs synchronously (not backgrounded) so the caller gets a real
    success/failure result, not just "started"."""
    secret = os.environ.get("CRON_SECRET")
    if not secret or request.headers.get("X-Cron-Secret") != secret:
        return jsonify({"error": "unauthorized"}), 403
    ok, msg = gmail_sync.sync(authoritative=False)
    return jsonify({"ok": ok, "message": msg})


@app.route("/crm/instantly/sync", methods=["POST"])
def instantly_sync_now():
    started = instantly.start_sync_in_background()
    return _done("Instantly sync started." if started else "A sync is already running.",
                 "ok" if started else "err", endpoint="settings")


@app.route("/crm/instantly/status")
def instantly_status():
    return jsonify(instantly.status())
