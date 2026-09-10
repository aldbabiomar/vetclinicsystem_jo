"""
`manage_maintenance` — the permission that separates administering this
*installation* (backups, restore, updates, autostart, browsing the server's
disk) from editing ordinary clinic settings.

Two distinct things are proven here, because the change had two distinct
ways to go wrong.

1. THE GRANT SURVIVES AN UPGRADE. Adding a key to auth.PERMISSIONS is not
   enough on an install that already exists: seed_default_roles_and_permissions()
   only *creates* roles that are missing, and admin_role_edit() refuses to
   edit a system role at all — so without a backfill the new permission would
   be held by nobody, with no way to grant it from the UI. Every clinic would
   lose backup and restore on upgrade. A fresh-install test cannot see this,
   because a fresh install creates the Admin role after the key exists.

2. THE ROUTES ARE GATED ON IT. test_permissions.py already proves every
   guarded route denies a user without its permission, generically. What it
   cannot notice is someone "fixing" a 403 by widening a decorator back to
   manage_settings — which would silently restore the original hole, where
   the Settings page hid these controls while the routes behind them
   accepted the request anyway.

Folder-browser confinement (the same change) is covered at the bottom.

Needs a throwaway Postgres; skips cleanly without one. See conftest.py.
"""
import os
import pathlib
import re
import uuid

import pytest

from conftest import needs_db

pytestmark = needs_db

APP_PY = pathlib.Path(__file__).parent.parent / "app.py"

# Everything the Settings page hides behind the maintenance gate. If a route
# is added to that block, add it here too — the whole point is that the UI
# gate and the server gate name the same permission.
MAINTENANCE_ROUTES = [
    "settings_backup_now",
    "settings_restore_now",
    "settings_job_status",
    "settings_autostart",
    "settings_updates_status",
    "settings_updates_check",
    "settings_updates_apply",
    "settings_updates_rollback",
    "api_browse_folder",
    "api_browse_folder_new",
]


def _decorated_permission(func_name):
    """The permission key on the route whose view is `func_name`, read from
    app.py's source — permission_required() closes over its keys, so the
    wrapped view does not expose them at runtime."""
    src = APP_PY.read_text(encoding="utf-8")
    m = re.search(
        r'@auth\.permission_required\(([^)]*)\)\s*\ndef ' + re.escape(func_name) + r'\(',
        src,
    )
    if not m:
        return None
    return tuple(re.findall(r'"(\w+)"', m.group(1)))


# ---------------------------------------------------------------------------
# 1. The upgrade path — the failure mode a fresh-install test cannot see
# ---------------------------------------------------------------------------

def test_reseeding_restores_the_system_admins_maintenance_grant(db):
    """GUARD. Simulates an install that predates the permission: strip the
    grant, then re-run the seeding an upgrade actually performs
    (updater._run_schema_sync -> setup.apply_schema -> this function).

    Reverting the backfill in seed_default_roles_and_permissions() fails
    exactly this test.

    The grant is put back in `finally` whatever happens. Without that, a
    genuine failure here would also strip the session-scoped admin client of
    manage_maintenance, and every later test in this file would fail for that
    unrelated reason instead of the one it names — the "refused for the wrong
    reason" trap in CLAUDE.md §7.3.
    """
    import auth

    admin_role = db.execute("SELECT id FROM roles WHERE is_system = true").fetchone()
    assert admin_role, "no system Admin role in the test database"

    try:
        db.execute(
            "DELETE FROM role_permissions WHERE role_id=? AND permission_id='manage_maintenance'",
            (admin_role["id"],),
        )
        db.commit()
        held_before = db.execute(
            "SELECT 1 FROM role_permissions WHERE role_id=? AND permission_id='manage_maintenance'",
            (admin_role["id"],),
        ).fetchone()
        assert held_before is None, "arrangement failed — the grant was not actually removed"

        auth.seed_default_roles_and_permissions(db)

        held_after = db.execute(
            "SELECT 1 FROM role_permissions WHERE role_id=? AND permission_id='manage_maintenance'",
            (admin_role["id"],),
        ).fetchone()
        assert held_after is not None, (
            "re-seeding did not restore manage_maintenance to the system Admin role — "
            "on a real install this leaves nobody able to back up, restore or update, "
            "and admin_role_edit() refuses to edit a system role, so it cannot be "
            "granted from the UI either"
        )
    finally:
        db.execute(
            "INSERT INTO role_permissions (role_id, permission_id) VALUES (?, 'manage_maintenance') "
            "ON CONFLICT DO NOTHING",
            (admin_role["id"],),
        )
        db.commit()


def test_reseeding_does_not_hand_maintenance_to_ordinary_roles(db):
    """CONTROL for the test above. The backfill targets is_system roles only;
    a blanket 'grant everything to everyone' would also make the guard pass."""
    import auth

    auth.seed_default_roles_and_permissions(db)
    leaked = db.execute(
        "SELECT r.name FROM roles r JOIN role_permissions rp ON rp.role_id = r.id "
        "WHERE rp.permission_id='manage_maintenance' AND r.is_system = false"
    ).fetchall()
    assert not leaked, (
        f"non-system roles gained manage_maintenance: {[r['name'] for r in leaked]}"
    )


def test_maintenance_is_admin_only_by_default(db):
    """A brand-new Vet or Reception must not seed with it."""
    import auth

    assert "manage_maintenance" in auth.PERMISSION_KEY_SET
    assert "manage_maintenance" in auth.ADMIN_ONLY_TODAY
    assert "manage_maintenance" not in auth.VET_RECEPTION_DEFAULT_PERMISSIONS


# ---------------------------------------------------------------------------
# 2. The routes are gated on it, and ordinary Settings still is not
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("view", MAINTENANCE_ROUTES)
def test_maintenance_route_requires_the_maintenance_permission(view):
    """GUARD. Widening any of these back to manage_settings reopens the
    original hole and fails here."""
    keys = _decorated_permission(view)
    assert keys is not None, f"{view} has no permission_required decorator at all"
    assert keys == ("manage_maintenance",), (
        f"{view} is gated on {keys}, not ('manage_maintenance',) — a "
        f"manage_settings holder would reach it while the Settings page hides it"
    )


def test_the_settings_page_itself_still_only_needs_manage_settings():
    """CONTROL. Ordinary clinic settings — clinic name, appointment hours,
    alert windows — must stay reachable for a plain settings administrator.
    Without this, the guard above is also satisfied by locking everything."""
    assert _decorated_permission("settings_page") == ("manage_settings",)


# ---------------------------------------------------------------------------
# 3. The folder browser is confined to real roots
# ---------------------------------------------------------------------------

@pytest.fixture
def browse_root(db, tmp_path):
    """Point the configured backup folder at a temp directory, making it one
    of the browser's permitted roots, and put a subfolder inside it."""
    previous = db.execute("SELECT value FROM settings WHERE key='backup_dir'").fetchone()
    root = tmp_path / "backups"
    (root / "nested").mkdir(parents=True)
    db.execute(
        "INSERT INTO settings (key,value) VALUES ('backup_dir',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(root),),
    )
    db.commit()
    yield root
    db.execute(
        "INSERT INTO settings (key,value) VALUES ('backup_dir',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (previous["value"] if previous else "",),
    )
    db.commit()


@pytest.mark.parametrize("outside", ["/etc", "/", "/var/log", "/usr"])
def test_browse_folder_refuses_paths_outside_every_root(client, browse_root, outside):
    """GUARD. This endpoint used to resolve any absolute path and list it."""
    resp = client.get("/api/browse-folder", query_string={"path": outside})
    assert resp.status_code == 400, f"{outside} was listed, not refused"
    assert "outside" in resp.get_json().get("error", "").lower()


def test_browse_folder_refuses_an_escape_from_inside_a_root(browse_root, client):
    """GUARD. commonpath(), not startswith() — and realpath() first, so `..`
    cannot walk out of a permitted root."""
    resp = client.get("/api/browse-folder", query_string={"path": str(browse_root / "..")})
    assert resp.status_code == 400


def test_browse_folder_refuses_a_sibling_that_shares_a_prefix(browse_root, client):
    """GUARD. '/x/backups-evil' must not pass a '/x/backups' check — the exact
    case a startswith() implementation gets wrong."""
    sibling = str(browse_root) + "-evil"
    os.makedirs(sibling, exist_ok=True)
    resp = client.get("/api/browse-folder", query_string={"path": sibling})
    assert resp.status_code == 400


def test_browse_folder_still_lists_a_permitted_root(client, browse_root):
    """CONTROL. Without this, 'refuses everything' would pass every guard."""
    resp = client.get("/api/browse-folder", query_string={"path": str(browse_root)})
    assert resp.status_code == 200, "the configured backup folder must still be browsable"
    body = resp.get_json()
    assert "nested" in body["folders"]
    assert body["parent"] is None, "the parent of a root must not be offered"


def test_browse_folder_still_lists_a_subfolder_of_a_root(client, browse_root):
    """CONTROL. Confinement must not stop normal navigation downwards."""
    resp = client.get("/api/browse-folder", query_string={"path": str(browse_root / "nested")})
    assert resp.status_code == 200
    assert resp.get_json()["parent"] == str(browse_root)


def test_new_folder_refuses_a_parent_outside_every_root(client, browse_root):
    """GUARD. Creating a folder is confined to the same roots as listing."""
    resp = client.post("/api/browse-folder/new-folder",
                       json={"path": "/tmp", "name": f"probe{uuid.uuid4().hex[:6]}"})
    assert resp.status_code == 400
    assert "outside" in resp.get_json().get("error", "").lower()


def test_new_folder_still_works_inside_a_root(client, browse_root):
    """CONTROL."""
    name = f"probe{uuid.uuid4().hex[:6]}"
    resp = client.post("/api/browse-folder/new-folder",
                       json={"path": str(browse_root), "name": name})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert (browse_root / name).is_dir()
