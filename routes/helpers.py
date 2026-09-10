"""Helpers shared across more than one routes/*.py module. Helpers used by
only a single route group live directly in their own file instead."""
from flask import flash, redirect, request, url_for


def _done(message, category="ok", endpoint="dashboard"):
    """Finish a row-action request: 204 for fetch() calls (the page updates
    itself in place), flash + redirect for plain form posts."""
    if request.headers.get("X-Requested-With") == "fetch":
        return "", 204
    flash(message, category)
    return redirect(request.referrer or url_for(endpoint))


def _status_options(db_values, defaults):
    """Real (sheet) statuses first, then any default not already present
    (case-insensitively) — so the dropdown never shows a cased duplicate."""
    seen = {s.lower() for s in db_values}
    return list(db_values) + [d for d in defaults if d.lower() not in seen]
