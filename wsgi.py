"""WSGI entry point for hosted deployment (gunicorn wsgi:app).

Same route imports as app.py, minus the local-dev __main__ block -- gunicorn
imports this module and serves the `app` object directly, it never runs
app.run(). See SETUP_HOSTING.md for the actual deploy config (Procfile,
required env vars, the cron-triggered Gmail sync).
"""
from app_core import app  # noqa: F401

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
