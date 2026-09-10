"""
The login audit trail must record where a request actually came from.

auth.log_login() read `request.headers.get("X-Forwarded-For", request.remote_addr)`
unconditionally. The default deployment is plain HTTP on the clinic LAN with
no proxy in front, so nothing set that header except a client choosing to —
which meant the IP shown on Admin > Logins and Changes was whatever the person
signing in decided it should be. An audit trail an attacker can write is worse
than none, because it is believed.

When a proxy IS in front, BEHIND_TLS_PROXY=1 installs ProxyFix, which rewrites
request.remote_addr from that header before the view ever runs. So reading
remote_addr is correct in both deployments, and reading the header is correct
in neither.
"""
import pytest

from conftest import needs_db

pytestmark = needs_db


def _last_login_row(db, username):
    return db.execute(
        "SELECT ip, user_agent FROM login_log WHERE username=? ORDER BY id DESC LIMIT 1",
        (username,),
    ).fetchone()


def test_a_spoofed_forwarded_header_is_not_recorded(flask_app, db):
    """GUARD. Reinstating the X-Forwarded-For read fails this."""
    c = flask_app.test_client()
    c.post("/login",
           data={"username": "nosuchuser_xff", "password": "wrong"},
           headers={"X-Forwarded-For": "10.9.9.9"},
           follow_redirects=True)
    row = _last_login_row(db, "nosuchuser_xff")
    assert row is not None, "the failed attempt was not logged at all"
    assert row["ip"] != "10.9.9.9", (
        "login_log.ip recorded a client-supplied X-Forwarded-For header — "
        "the audit trail is forgeable")


def test_the_real_client_address_is_still_recorded(flask_app, db):
    """CONTROL. Without this, dropping the column entirely would satisfy the
    guard above while destroying the thing it protects."""
    c = flask_app.test_client()
    c.post("/login",
           data={"username": "nosuchuser_real", "password": "wrong"},
           follow_redirects=True)
    row = _last_login_row(db, "nosuchuser_real")
    assert row is not None
    assert row["ip"], "no client address was recorded at all"
    # Werkzeug's test client reports 127.0.0.1 as the peer.
    assert row["ip"] == "127.0.0.1", row["ip"]


def test_the_user_agent_is_still_recorded(flask_app, db):
    """CONTROL. The device summary on the Logins screen is built from this."""
    c = flask_app.test_client()
    c.post("/login",
           data={"username": "nosuchuser_ua", "password": "wrong"},
           headers={"User-Agent": "Mozilla/5.0 (Macintosh) Safari/605.1"},
           follow_redirects=True)
    row = _last_login_row(db, "nosuchuser_ua")
    assert "Safari" in (row["user_agent"] or "")


def test_auth_does_not_read_the_forwarded_header_anywhere(flask_app):
    """GUARD on the pattern, not just this call site — ProxyFix is the only
    thing that should ever consult that header."""
    import inspect
    import auth

    src = inspect.getsource(auth)
    # Strip comments: the explanation of why we do not read it mentions it.
    code = "\n".join(line.split("#")[0] for line in src.splitlines())
    assert "X-Forwarded-For" not in code, (
        "auth.py reads X-Forwarded-For in code again — see log_login()'s comment")
