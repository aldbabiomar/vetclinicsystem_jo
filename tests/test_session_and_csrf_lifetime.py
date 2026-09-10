"""
Two ways this app used to throw away a person's work without saying so.

U1 — the CSRF token lifetime was Flask-WTF's default of one hour, while
PERMANENT_SESSION_LIFETIME was set deliberately to twelve, with a comment
explaining that a front-desk browser stays open for a whole shift. A form open
across a consultation therefore failed on submit, lost everything typed, and
reported "Your session expired" — which is not what had happened, and sent the
person to a login page they did not need.

U2 — a POST that arrives with no session is redirected to /login?next=<path>,
and login() then redirects to that path with a GET. The body is gone. Nothing
told the person, so a visit or a bill got quietly typed twice.

Neither is fixed by preserving the submission (that is a larger change, see
FULL_APP_REVIEW U2); both are fixed by not lying about what happened.
"""
import pytest

from conftest import needs_db

pytestmark = needs_db


# ---------------------------------------------------------------------------
# U1 — the two lifetimes cannot drift apart
# ---------------------------------------------------------------------------

def test_csrf_token_lives_as_long_as_the_session(flask_app):
    """GUARD. Leaving WTF_CSRF_TIME_LIMIT unset is the bug: Flask-WTF then
    defaults it to 3600s regardless of how long the session is good for."""
    limit = flask_app.config.get("WTF_CSRF_TIME_LIMIT")
    assert limit is not None, (
        "WTF_CSRF_TIME_LIMIT is unset, so Flask-WTF's 1-hour default applies "
        "while the session lasts PERMANENT_SESSION_LIFETIME")
    session_seconds = flask_app.config["PERMANENT_SESSION_LIFETIME"].total_seconds()
    assert limit == pytest.approx(session_seconds, rel=0.01), (
        f"CSRF token expires after {limit}s but the session lasts "
        f"{session_seconds:.0f}s — a form open in between fails on submit")


def test_the_session_lifetime_is_still_twelve_hours_by_default(flask_app):
    """CONTROL. Tying the two together must not have shortened the session;
    setting both to one hour would also satisfy the guard above."""
    assert flask_app.config["PERMANENT_SESSION_LIFETIME"].total_seconds() == 12 * 3600


def test_raising_the_session_lifetime_raises_both(monkeypatch):
    """CONTROL. They are derived from one value, so they cannot be changed
    apart by an operator setting the documented environment variable."""
    import importlib
    import os

    monkeypatch.setenv("SESSION_LIFETIME_HOURS", "4")
    import app as app_module
    reloaded = importlib.reload(app_module)
    try:
        assert reloaded.app.config["PERMANENT_SESSION_LIFETIME"].total_seconds() == 4 * 3600
        assert reloaded.app.config["WTF_CSRF_TIME_LIMIT"] == 4 * 3600
    finally:
        monkeypatch.delenv("SESSION_LIFETIME_HOURS", raising=False)
        importlib.reload(app_module)


# ---------------------------------------------------------------------------
# U1 — the message tells the truth about which thing expired
# ---------------------------------------------------------------------------

def test_a_stale_token_with_a_live_session_does_not_force_a_re_login(flask_app, client):
    """GUARD. The old handler redirected to /login and blamed the session,
    whatever had actually happened."""
    csrf_app = flask_app.test_client()
    csrf_app.post("/login", data={"username": "admin", "password": "Admin12345!"},
                  follow_redirects=True)
    flask_app.config["WTF_CSRF_ENABLED"] = True
    try:
        resp = csrf_app.post("/settings", data={"clinic_name": "Nope"}, follow_redirects=False)
        assert resp.status_code in (302, 303)
        assert "/login" not in resp.headers["Location"], (
            "a stale form token logged out a session that was still valid")
        with csrf_app.session_transaction() as sess:
            assert sess.get("user_id"), "the session was cleared by a CSRF failure"
            flashes = [m for _, m in sess.get("_flashes", [])]
        assert any("open too long" in m for m in flashes), flashes
        assert not any("signed out" in m for m in flashes), (
            f"the message still blames the session: {flashes}")
    finally:
        flask_app.config["WTF_CSRF_ENABLED"] = False


def test_a_stale_token_with_no_session_still_sends_you_to_log_in(flask_app):
    """CONTROL. When the session really is gone, the login redirect is
    correct — the guard above must not have removed it."""
    anon = flask_app.test_client()
    flask_app.config["WTF_CSRF_ENABLED"] = True
    try:
        resp = anon.post("/settings", data={"clinic_name": "Nope"}, follow_redirects=False)
        assert resp.status_code in (302, 303)
        assert "/login" in resp.headers["Location"]
    finally:
        flask_app.config["WTF_CSRF_ENABLED"] = False


# ---------------------------------------------------------------------------
# U2 — a discarded submission is announced
# ---------------------------------------------------------------------------

def test_a_post_without_a_session_says_the_data_was_not_saved(flask_app):
    """GUARD. The redirect already happened; what was missing was telling
    anyone that their typing went with it."""
    anon = flask_app.test_client()
    resp = anon.post("/owners/new", data={"name": "Jane Doe", "phone": "07701234567"},
                     follow_redirects=False)
    assert resp.status_code in (302, 303)
    assert "/login" in resp.headers["Location"]
    with anon.session_transaction() as sess:
        flashes = [m for _, m in sess.get("_flashes", [])]
    assert any("nothing was stored" in m for m in flashes), (
        f"a discarded POST said nothing to the user: {flashes}")


def test_a_get_without_a_session_stays_quiet(flask_app):
    """CONTROL. Every anonymous page load redirects to login too; warning
    about lost data there would be noise on the normal path."""
    anon = flask_app.test_client()
    resp = anon.get("/owners", follow_redirects=False)
    assert resp.status_code in (302, 303)
    with anon.session_transaction() as sess:
        flashes = [m for _, m in sess.get("_flashes", [])]
    assert not any("nothing was stored" in m for m in flashes), (
        f"an ordinary signed-out page view warned about losing data: {flashes}")


def test_the_next_parameter_still_points_back_at_the_attempted_page(flask_app):
    """CONTROL. The warning must not have disturbed the redirect target."""
    anon = flask_app.test_client()
    resp = anon.post("/owners/new", data={"name": "Jane"}, follow_redirects=False)
    assert "next=%2Fowners%2Fnew" in resp.headers["Location"] or \
           "next=/owners/new" in resp.headers["Location"], resp.headers["Location"]
