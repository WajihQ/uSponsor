"""uSponsor — track which brands sponsor which YouTube creators.

Run:  python app.py   then open http://127.0.0.1:5000

Routes live in routes/*.py, grouped by feature area, all registering on the
one shared Flask app object from app_core.py. See CLAUDE.md for the full
per-file breakdown. wsgi.py does the same route imports for gunicorn/hosted
use; this file is the local-dev entry point.

Gmail sync has no automatic interval anymore -- use the Settings page's
"Sync now" button locally, or POST /cron/gmail-sync on a schedule once
hosted (see SETUP_HOSTING.md).
"""
from app_core import app  # noqa: F401  (re-exported: tests/conftest.py does `import app` then `app.app.run(...)`)

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
    app.run(debug=False, port=5000)
