"""
The few pieces of VetClinicSystem JO that both `app.py` and the route blueprints
under `routes/` need.

This module exists to break what would otherwise be a circular import: app.py
creates the Flask app and registers the blueprints, so a blueprint cannot
import from app.py. Everything here is deliberately small and dependency-free
in that direction — it imports Flask and `db`, and nothing from this
application's own request layer.

Nothing here changed behaviour when it moved out of app.py; these are the same
definitions, in a place both sides can reach.
"""
import os
import socket

from flask import g

import db as dbmod

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# On the versioned-release layout (VETCLINICSYSTEMJO_DATA_DIR set by the
# launcher script — see updater.py / setup.py --enable-updates), the .env, the
# logs and the uploads live in the persistent data dir rather than beside this
# file, whose folder an in-app update replaces and later prunes.
DATA_DIR = os.environ.get("VETCLINICSYSTEMJO_DATA_DIR")

_version_path = os.path.join(BASE_DIR, "VERSION")
VERSION = open(_version_path).read().strip() if os.path.exists(_version_path) else "unknown"

# Separate from (and shorter than) DB_POOL_TIMEOUT_SECONDS, which the pool
# itself still uses for background/maintenance callers (dbmod.connect()).
# During a full DB outage every request that reaches get_db() would otherwise
# block for the pool's full default wait before failing — tying up one of
# Waitress's worker threads that whole time and making the app look hung rather
# than degraded.
DB_REQUEST_TIMEOUT_SECONDS = float(os.environ.get("DB_REQUEST_TIMEOUT_SECONDS", "4"))


def get_db():
    """The request-scoped connection. Borrowed from the pool on first use and
    returned by app.py's close_db() teardown."""
    if "db" not in g:
        g.db = dbmod.getconn(timeout=DB_REQUEST_TIMEOUT_SECONDS)
    return g.db


def lan_address():
    """This machine's address on the clinic LAN, for the Settings page's
    "reach it from another device at ..." line. Falls back to loopback rather
    than raising when there is no route out."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"
