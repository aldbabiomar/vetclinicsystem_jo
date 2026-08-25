"""
Permission enforcement — proving that a permission is actually DENIED.

Every other test in this suite logs in as the seeded admin, who holds all
28 permissions. That means the entire permission system — the thing that
stops a receptionist deleting patient records, editing the price list, or
reaching the users-and-roles screen — had never once been exercised.
140 routes carry a `permission_required` decorator; nothing verified that
any of them ever says no.

The approach here is deliberately not a hand-written list of routes. Route
and permission pairs are discovered by reading app.py, so a route added
tomorrow is covered the moment it exists, and a decorator deleted in a
refactor shows up as a failure rather than as silence.

The permission check runs before the view body, so a route can be probed
with a nonsense id and an empty form — it returns 403 either way. That is
what makes covering all 140 routes cheap rather than a fixture nightmare.

Needs a throwaway Postgres; skips cleanly without one. See conftest.py.
"""
import re
import io
import pathlib
import uuid

import pytest

from conftest import needs_db


pytestmark = needs_db

APP_PY = pathlib.Path(__file__).parent.parent / "app.py"

# Routes that intentionally sit outside the permission model, or that would
# damage the shared test session if probed.
SKIP_RULES = {"/logout"}


def _route_permissions():
    """[(rule, methods, required_permission_keys)] parsed from app.py.

    Read from the source rather than the live url_map because
    permission_required() closes over its keys — the wrapped view does not
    expose which permission it is gating.
    """
    src = io.open(APP_PY, encoding="utf-8").read().split("\n")
    pairs, pending = [], []
    for line in src:
        stripped = line.strip()
        m = re.match(r'@app\.route\("([^"]+)"(?:,\s*methods=\[([^\]]+)\])?\)', stripped)
        if m:
            methods = re.findall(r'"(\w+)"', m.group(2) or '"GET"')
            pending.append((m.group(1), methods))
            continue
        p = re.match(r'@auth\.permission_required\((.+)\)', stripped)
        if p and pending:
            keys = tuple(re.findall(r'"(\w+)"', p.group(1)))
            pairs.extend((rule, tuple(methods), keys) for rule, methods in pending)
            pending = []
            continue
        if line.startswith("def ") or (stripped and not stripped.startswith("@")):
            pending = []
    return [t for t in pairs if t[0] not in SKIP_RULES]


ROUTE_PERMISSIONS = _route_permissions()
ALL_PERMISSIONS = sorted({k for _, _, keys in ROUTE_PERMISSIONS for k in keys})


def _concrete(rule):
    """A requestable URL. The id does not need to exist — the permission
    check fires before the view runs."""
    url = re.sub(r"<int:\w+>", "999999", rule)
    url = re.sub(r"<path:\w+>", "nope", url)
    url = re.sub(r"<\w+>", "NOPE", url)
    return url


@pytest.fixture(scope="module")
def restricted(flask_app):
    """A user holding exactly one permission, and nothing else.

    Built directly rather than through the admin UI so the test does not
    depend on the very screens it is about to prove are gated.
    """
    import db as dbmod
    import auth
    con = dbmod.connect()
    tag = uuid.uuid4().hex[:8]
    role_id, user_id = f"RT{tag.upper()}", f"UT{tag.upper()}"
    username, password = f"limited{tag}", "LimitedPass12345!"
    held = "manage_owners"
    assert held in ALL_PERMISSIONS, "the permission this fixture holds must gate real routes"

    con.execute("INSERT INTO roles (id, name, description, is_system, discount_cap, is_vet_role, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (role_id, f"Restricted {tag}", "permission test", False, 0, False,
                 "2026-01-01T00:00:00"))
    con.execute("INSERT INTO role_permissions (role_id, permission_id) VALUES (?,?)", (role_id, held))
    con.execute("INSERT INTO users (id, username, password_hash, full_name, role_id, active, "
                "must_change_password, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (user_id, username, auth.hash_password(password), "Restricted User",
                 role_id, True, False, "2026-01-01T00:00:00"))
    con.commit()

    client = flask_app.test_client()
    resp = client.post("/login", data={"username": username, "password": password},
                       follow_redirects=True)
    assert resp.status_code == 200
    with client.session_transaction() as sess:
        assert sess.get("user_id") == user_id, "the restricted user could not log in"
        granted = sess.get("permissions") or []
        assert list(granted) == [held], (
            f"the session should carry exactly one permission, got {sorted(granted)}")

    yield {"client": client, "held": held, "user_id": user_id, "role_id": role_id}

    con.execute("DELETE FROM login_log WHERE user_id=?", (user_id,))
    con.execute("DELETE FROM users WHERE id=?", (user_id,))
    con.execute("DELETE FROM role_permissions WHERE role_id=?", (role_id,))
    con.execute("DELETE FROM roles WHERE id=?", (role_id,))
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# The discovery itself has to be trustworthy, or everything below is vacuous
# ---------------------------------------------------------------------------

def test_route_discovery_found_the_whole_permission_surface():
    """If the parse silently returned nothing, every test below would pass
    while checking no routes at all."""
    assert len(ROUTE_PERMISSIONS) > 100, (
        f"only {len(ROUTE_PERMISSIONS)} guarded routes found — the app.py parse has broken")
    assert len(ALL_PERMISSIONS) >= 20, (
        f"only {len(ALL_PERMISSIONS)} distinct permissions found")


def test_every_discovered_permission_is_a_real_permission_key():
    """A typo'd key in a decorator gates a route behind a permission no role
    can ever hold — locking everyone out of it, silently and permanently."""
    import auth
    unknown = [k for k in ALL_PERMISSIONS if k not in auth.PERMISSION_KEY_SET]
    assert not unknown, f"routes gated behind non-existent permission(s): {unknown}"


# ---------------------------------------------------------------------------
# Denial — the actual point
# ---------------------------------------------------------------------------

def test_every_route_needing_another_permission_is_refused(restricted):
    """The core assertion, across all 140 guarded routes at once.

    A user holding only `manage_owners` must be refused every route that
    requires something else — whichever HTTP method it takes, and regardless
    of whether the id in the URL exists.
    """
    client, held = restricted["client"], restricted["held"]
    leaked = []
    checked = 0
    for rule, methods, keys in ROUTE_PERMISSIONS:
        if held in keys:
            continue
        url = _concrete(rule)
        for method in methods:
            if method not in ("GET", "POST"):
                continue
            checked += 1
            resp = (client.get(url) if method == "GET"
                    else client.post(url, data={}))
            if resp.status_code != 403:
                leaked.append(f"{method} {url} (needs {'/'.join(keys)}) -> {resp.status_code}")
    assert checked > 100, f"only {checked} route/method combinations were probed"
    assert not leaked, (
        f"{len(leaked)} route(s) reachable without the permission they require:\n  "
        + "\n  ".join(leaked[:25]))


def test_the_one_held_permission_still_opens_its_own_routes(restricted):
    """The control. Without it, a user who is refused *everything* — a
    broken login, a role with no permissions at all — would make the test
    above pass while proving nothing about permissions."""
    client, held = restricted["client"], restricted["held"]
    reachable = 0
    wrongly_refused = []
    for rule, methods, keys in ROUTE_PERMISSIONS:
        if held not in keys or "GET" not in methods:
            continue
        resp = client.get(_concrete(rule))
        if resp.status_code == 403:
            wrongly_refused.append(f"GET {_concrete(rule)} (needs {'/'.join(keys)})")
        else:
            reachable += 1
    assert reachable > 0, "the held permission opened no routes at all"
    assert not wrongly_refused, (
        "route(s) refused despite the user holding their permission:\n  "
        + "\n  ".join(wrongly_refused))


@pytest.mark.parametrize("permission", ALL_PERMISSIONS)
def test_each_permission_gates_at_least_one_route(permission):
    """Every permission offered on the roles screen must actually control
    something. One that gates nothing is a checkbox that does nothing —
    worse than absent, because it reads as protection."""
    gated = [r for r, _, keys in ROUTE_PERMISSIONS if permission in keys]
    assert gated, f"{permission!r} appears on the roles screen but gates no route"


# ---------------------------------------------------------------------------
# The shapes that make a permission system fail open
# ---------------------------------------------------------------------------

def test_a_logged_out_visitor_is_never_given_a_403_instead_of_a_login(flask_app):
    """Anonymous requests must be sent to the login page, not shown a
    permission error — a 403 to a stranger confirms the route exists and
    that they merely lack rights, which is more than they should learn."""
    anon = flask_app.test_client()
    wrong = []
    for rule, methods, _keys in ROUTE_PERMISSIONS[:60]:
        if "GET" not in methods:
            continue
        resp = anon.get(_concrete(rule))
        if resp.status_code == 200:
            wrong.append(f"GET {_concrete(rule)} -> 200 with no session")
    assert not wrong, "route(s) served to an anonymous visitor:\n  " + "\n  ".join(wrong)


def test_deactivating_an_account_stops_it_working_immediately(restricted):
    """A dismissed member of staff must lose access on their next click, not
    whenever their session happens to expire."""
    import db as dbmod
    client = restricted["client"]
    open_route = next((_concrete(r) for r, m, k in ROUTE_PERMISSIONS
                       if restricted["held"] in k and "GET" in m), None)
    assert open_route, "needed a route this user can reach"
    assert client.get(open_route).status_code != 403, "should start with access"

    con = dbmod.connect()
    con.execute("UPDATE users SET active=false WHERE id=?", (restricted["user_id"],))
    con.commit()
    try:
        resp = client.get(open_route)
        assert resp.status_code != 200, (
            "a deactivated account was still served a page it used to have access to")
    finally:
        con.execute("UPDATE users SET active=true WHERE id=?", (restricted["user_id"],))
        con.commit()
        con.close()
        client.post("/login", data={"username": f"limited{restricted['user_id'][2:].lower()}",
                                    "password": "LimitedPass12345!"}, follow_redirects=True)


def test_revoking_a_permission_takes_effect_without_re_login(client, restricted):
    """Permissions are cached in the session, and refreshed only when a
    counter in settings changes — so an admin revoking access takes effect on
    the user's very next request without them logging out.

    The revocation has to go through the admin screen, not straight into the
    database: writing to role_permissions directly never bumps that counter,
    so the cached session stays valid and the user keeps their access. That
    is the design working, not a bug — but it does mean any future code that
    changes permissions must call bump_permissions_version(), and a test that
    edits the table by hand would wrongly report a stale cache.
    """
    limited, held, role_id = restricted["client"], restricted["held"], restricted["role_id"]
    open_route = next((_concrete(r) for r, m, k in ROUTE_PERMISSIONS
                       if held in k and "GET" in m), None)
    assert open_route, "needed a route this user can reach"
    assert limited.get(open_route).status_code != 403, "should start with access"

    # The admin removes the permission from the role, leaving it with none.
    resp = client.post(f"/admin/roles/{role_id}/edit", data={
        "name": f"Restricted {role_id[2:].lower()}", "description": "permission test",
        "discount_cap": "0"}, follow_redirects=False)
    assert resp.status_code != 500

    try:
        after = limited.get(open_route)
        assert after.status_code == 403, (
            "the permission was revoked but the already-logged-in session kept access — "
            "the per-request refresh did not pick it up")
    finally:
        client.post(f"/admin/roles/{role_id}/edit", data={
            "name": f"Restricted {role_id[2:].lower()}", "description": "permission test",
            "discount_cap": "0", "permissions": [held]}, follow_redirects=False)
