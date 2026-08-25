"""
Shared test setup.

app.py refuses to import without a real SECRET_KEY (a deliberate fail-fast
so nobody boots the app with the placeholder from .env.example), and it
lives at the repo root rather than on the default import path. Both are
fixed here so individual test modules can just `import app` / `import
logic` / `import money`.

TWO KINDS OF TEST LIVE IN THIS DIRECTORY
----------------------------------------
1. **Pure tests** (`test_money.py`, `test_frontend.py`) — no database, no
   Docker, no running app. They exercise the money arithmetic, the input
   parsers and the stylesheet. These always run.

2. **Route tests** (`test_money_routes.py`) — a real Flask test client
   against a real Postgres, because the money *transactions* (checkout,
   billing, refunds) cannot be exercised any other way: they write rows,
   decrement stock and compute change in one request. They need a
   throwaway database and **skip cleanly when there isn't one**, so the
   fast suite stays runnable anywhere.

   To run them, bring up the isolated environment first:

       scripts/isolated_test_env.sh up jo

   then point the tests at it:

       TEST_DATABASE_URL=postgresql://postgres:test@localhost:55492/vetclinicsystemjo \\
         venv/bin/python -m pytest tests/ -q

   Never point TEST_DATABASE_URL at a real clinic database. These tests
   write and delete rows.
"""
import os
import sys
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Any non-placeholder value satisfies app.py's guard. Set before import.
os.environ.setdefault("SECRET_KEY", "test-only-key-not-used-for-real-sessions")

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")

# A safety interlock, not a formality. These tests write rows. If someone
# exports a real DATABASE_URL in their shell and runs pytest, we must not
# quietly reuse it — TEST_DATABASE_URL has to be set deliberately.
SKIP_REASON = (
    "no TEST_DATABASE_URL — route tests need a throwaway database. "
    "Run: scripts/isolated_test_env.sh up jo, then set "
    "TEST_DATABASE_URL=postgresql://postgres:test@localhost:55492/vetclinicsystemjo"
)

needs_db = pytest.mark.skipif(not TEST_DB_URL, reason=SKIP_REASON)


@pytest.fixture(scope="session")
def flask_app():
    """The real application object, wired to the throwaway database."""
    if not TEST_DB_URL:
        pytest.skip(SKIP_REASON)
    os.environ["DATABASE_URL"] = TEST_DB_URL
    import app as app_module

    app_module.app.config.update(
        TESTING=True,
        # TESTING=True turns PROPAGATE_EXCEPTIONS on, which bypasses every
        # @app.errorhandler the app registers — so a route that degrades
        # gracefully in production (flash + redirect) would surface here as a
        # raw 500. That is a configuration no clinic ever runs, and testing it
        # produces findings that are artefacts of the harness. Turn it back off
        # so these tests exercise the real error paths.
        PROPAGATE_EXCEPTIONS=False,
        # CSRF is verified separately by the app's own error handling; leaving
        # it on here would mean every test parsing a token out of HTML, which
        # tests Flask-WTF rather than the money logic.
        WTF_CSRF_ENABLED=False,
    )
    return app_module.app


@pytest.fixture(scope="session")
def client(flask_app):
    """Logged in as the seeded admin, who holds every permission."""
    c = flask_app.test_client()
    resp = c.post("/login", data={"username": "admin", "password": "Admin12345!"},
                  follow_redirects=True)
    assert resp.status_code == 200, "could not log in to the test database"
    with c.session_transaction() as sess:
        assert sess.get("user_id"), (
            "login did not establish a session — is the admin user seeded? "
            "scripts/isolated_test_env.sh up jo creates admin/Admin12345!")
    return c


@pytest.fixture
def db(flask_app):
    """A direct connection for arranging fixtures and asserting on stored
    rows — deliberately separate from the request-scoped pool the app uses,
    so a test reads what was actually committed rather than what a shared
    transaction is holding."""
    import db as dbmod
    con = dbmod.connect()
    try:
        yield con
    finally:
        # A failed statement leaves Postgres refusing everything else on this
        # connection ("current transaction is aborted"). Rolling back before
        # close keeps one broken test from being reported as a broken fixture.
        try:
            con.rollback()
        except Exception:
            pass
        con.close()
