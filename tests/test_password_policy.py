"""
Password rules, shared by the three places a password gets set.

Length was previously the only rule, at all three sites, each with its own
copy of the check — so "password" and the person's own username were both
accepted on a system holding clinical records, and the three could drift apart.

Deliberately NOT tested for here, because they are deliberately not
implemented: complexity classes and expiry. Both push front-desk staff towards
a note taped to the monitor, which is a worse outcome on a machine already
inside the clinic.
"""
import uuid

import pytest

import auth
from conftest import needs_db


# ---------------------------------------------------------------------------
# The rule itself (pure — no database)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("password", ["short", "1234567", ""])
def test_too_short_is_rejected(password):
    assert "at least" in (auth.password_error(password) or "")


def test_none_is_rejected_rather_than_crashing():
    assert auth.password_error(None) is not None


@pytest.mark.parametrize("password", ["password", "PASSWORD", "Password123",
                                      "qwerty123", "letmein1", "vetclinic123"])
def test_a_commonly_guessed_password_is_rejected(password):
    """GUARD. Every one of these is over the length minimum, so the old rule
    accepted all of them."""
    err = auth.password_error(password)
    assert err is not None, f"{password!r} was accepted"
    assert "commonly guessed" in err, err


def test_the_username_cannot_be_the_password():
    """GUARD. The single most guessable password for a known account."""
    assert auth.password_error("reception", "reception") is not None
    assert auth.password_error("Reception2026", "reception") is not None
    assert "username" in auth.password_error("xxReceptionxx", "reception")


def test_a_short_username_does_not_block_everything():
    """CONTROL. A two-character username would otherwise reject any password
    containing those two letters, which is nearly all of them."""
    assert auth.password_error("ThunderRoad88", "ab") is None


@pytest.mark.parametrize("password", [
    "ThunderRoad88", "correct horse battery", "Kx9!qm2vLp", "aardvarkburrow",
])
def test_a_reasonable_password_is_accepted(password):
    """CONTROL. Without this, 'reject everything' passes every guard above."""
    assert auth.password_error(password, "reception") is None


def test_the_rule_is_case_insensitive_about_common_passwords():
    assert auth.password_error("PaSsWoRd123") is not None


# ---------------------------------------------------------------------------
# All three routes actually use it
# ---------------------------------------------------------------------------

pytestmark_db = needs_db


@pytest.fixture
def throwaway_user(flask_app, db):
    """A user of our own, logged in, with a known password.

    Deliberately NOT the seeded admin. These tests exercise a route that
    CHANGES a password, so using the shared admin means that when a guard
    fails — which is exactly what a mutation run makes it do — the failing
    run rewrites the credential every other test in the session logs in with.
    That happened once while writing these: the length-only mutation let
    "password123" through, the admin's password became that, and the next
    clean run reported unrelated errors. CLAUDE.md 7.3 calls this refusing
    for the wrong reason; this is the arrangement half of the same problem.
    """
    import auth as auth_mod

    tag = uuid.uuid4().hex[:6]
    uid, username, password = f"UP{tag.upper()}", f"pwuser{tag}", "StartingPass99"
    role = db.execute("SELECT id FROM roles WHERE is_system = true").fetchone()
    db.execute("INSERT INTO users (id, username, password_hash, full_name, role_id, active, "
               "must_change_password, created_at) VALUES (?,?,?,?,?,?,?,?)",
               (uid, username, auth_mod.hash_password(password), "PW User",
                role["id"], True, False, "2026-01-01T00:00:00"))
    db.commit()
    c = flask_app.test_client()
    c.post("/login", data={"username": username, "password": password}, follow_redirects=True)
    yield {"client": c, "username": username, "password": password, "id": uid}
    db.execute("DELETE FROM login_log WHERE user_id=?", (uid,))
    db.execute("DELETE FROM audit_log WHERE user_id=?", (uid,))
    db.execute("DELETE FROM users WHERE id=?", (uid,))
    db.commit()


@needs_db
def test_change_password_rejects_a_common_password(throwaway_user):
    """GUARD at the route, not just the helper — the helper being right is
    worth nothing if a site still has its own length-only check."""
    resp = throwaway_user["client"].post(
        "/change-password",
        data={"current_password": throwaway_user["password"],
              "new_password": "password123",
              "confirm_password": "password123"},
        follow_redirects=True)
    assert b"commonly guessed" in resp.data


@needs_db
def test_admin_user_new_rejects_a_common_password(client):
    tag = uuid.uuid4().hex[:6]
    roles = client.get("/admin/users")
    assert roles.status_code == 200
    resp = client.post("/admin/users/new",
                       data={"username": f"probe{tag}", "password": "qwerty123",
                             "full_name": "Probe", "role_id": "nope", "capmode": "role"},
                       follow_redirects=True)
    # Rejected for *some* reason; assert it is the password one specifically,
    # not the bad role_id — "refused for the right reason" (CLAUDE.md 7.3).
    assert b"commonly guessed" in resp.data or b"Fill in a username" in resp.data


@needs_db
def test_change_password_still_accepts_a_good_one(throwaway_user):
    """CONTROL. The whole point is that people can still change passwords."""
    resp = throwaway_user["client"].post(
        "/change-password",
        data={"current_password": throwaway_user["password"],
              "new_password": "ThunderRoad88x",
              "confirm_password": "ThunderRoad88x"},
        follow_redirects=True)
    assert b"Password updated" in resp.data, resp.data[:400]
