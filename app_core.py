"""Shared Flask app object + constants that must resolve relative to the
repo root. Every routes/*.py module does `from app_core import app` to
register its routes on this one shared instance — kept in its own module
(instead of in app.py) to avoid a circular import between app.py (which
needs to import the route modules) and the route modules (which need the
`app` object to register routes)."""
import os

from flask import Flask

from tracker import db

app = Flask(__name__)
app.secret_key = "usponsor-local"  # local single-user tool; only used for flash messages
db.init_db()

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
