"""Route blueprints for VetClinicSystem JO.

Each module here owns one area of the app and exposes `bp`, a Flask
Blueprint that app.py registers. They import shared request-layer
pieces from core.py, never from app.py -- app.py registers them, so the
other direction would be circular.
"""
