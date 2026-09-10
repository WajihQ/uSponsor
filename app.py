"""uSponsor — track which brands sponsor which YouTube creators.

Run:  python app.py   then open http://127.0.0.1:5000

Routes live in routes/*.py, grouped by feature area, all registering on the
one shared Flask app object from app_core.py. See CLAUDE.md for the full
per-file breakdown.
"""
import os

from app_core import app  # noqa: F401  (re-exported: tests/conftest.py does `import app` then `app.app.run(...)`)
from tracker import gmail_sync

import routes.dashboard  # noqa: F401
import routes.channels  # noqa: F401
import routes.crm_influencers  # noqa: F401
import routes.crm_brands  # noqa: F401
import routes.scan  # noqa: F401
import routes.settings  # noqa: F401
import routes.campaigns  # noqa: F401
import routes.brands  # noqa: F401
import routes.creator  # noqa: F401
import routes.sponsorship  # noqa: F401

if __name__ == "__main__":
    # background heartbeat that folds new Sent mail into the CRM; set
    # USPONSOR_GMAIL_INTERVAL=0 to disable, or a minute count to change cadence
    try:
        _mins = int(os.environ.get("USPONSOR_GMAIL_INTERVAL", "30"))
    except ValueError:
        _mins = 30
    if _mins > 0:
        gmail_sync.start_interval(_mins)
    app.run(debug=False, port=5000)
