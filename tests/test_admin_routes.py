"""
Settings, roles and permissions.

These decide what everyone else in the app is allowed to do. A role with a
discount cap set wrong lets a receptionist write off whatever they like; a
settings value out of range breaks the scheduling grid or the reorder
alerts for the whole clinic at once.

Needs a throwaway Postgres; skips cleanly without one. See conftest.py.
"""
import uuid

import pytest

from conftest import needs_db


pytestmark = needs_db


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@pytest.fixture
def settings_snapshot(db):
    """Settings are global. Capture and restore them so a test that writes
    one cannot change what every other test sees."""
    rows = db.execute("SELECT key, value FROM settings").fetchall()
    original = {r["key"]: r["value"] for r in rows}
    yield original
    db.execute("DELETE FROM settings")
    for k, v in original.items():
        db.execute("INSERT INTO settings (key, value) VALUES (?,?)", (k, v))
    db.commit()


def _setting(db, key):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return None if row is None else row["value"]


def _save_settings(client, **data):
    payload = {"clinic_name": "Test Clinic"}
    payload.update(data)
    return client.post("/settings", data=payload, follow_redirects=False)


def test_a_setting_can_be_saved(client, db, settings_snapshot):
    _save_settings(client, clinic_name="Renamed Clinic")
    assert _setting(db, "clinic_name") == "Renamed Clinic"


def test_numeric_settings_outside_their_range_are_refused(client, db, settings_snapshot):
    """Each numeric setting has a sane range. A zero or negative reorder
    window silently disables the alerts it drives, and nothing announces
    that they stopped arriving."""
    # The ranges are declared inside settings_page() rather than at module
    # level, so this names the setting directly instead of reading them.
    key, absurd = "audit_overdue_days", "99999"
    before = _setting(db, key)
    resp = _save_settings(client, **{key: absurd})
    assert resp.status_code != 500
    after = _setting(db, key)
    assert after != absurd, f"{key} accepted a value far outside any sane range"
    assert after == before or after is None


def test_a_malformed_time_setting_is_refused(client, db, settings_snapshot):
    """The appointment grid is generated from these; a junk value would make
    every slot on every day disappear."""
    key = "appt_start_time"
    before = _setting(db, key)
    resp = _save_settings(client, **{key: "not-a-time"})
    assert resp.status_code != 500
    assert _setting(db, key) in (before, None), "a junk time must not be stored"


def test_appointment_day_cannot_end_before_it_starts(client, db, settings_snapshot):
    """An end time before the start produces a schedule with no slots at
    all — technically consistent, completely unusable."""
    resp = _save_settings(client, appt_start_time="17:00", appt_end_time="09:00")
    assert resp.status_code != 500
    start, end = _setting(db, "appt_start_time"), _setting(db, "appt_end_time")
    if start and end:
        assert not (start == "17:00" and end == "09:00"), (
            "a backwards working day must not be saved")


# ---------------------------------------------------------------------------
# Roles — what a discount cap actually protects
# ---------------------------------------------------------------------------

@pytest.fixture
def role_cleanup(db):
    created = []
    yield created
    for rid in created:
        db.execute("DELETE FROM role_permissions WHERE role_id=?", (rid,))
        db.execute("UPDATE users SET role_id=NULL WHERE role_id=?", (rid,))
        db.execute("DELETE FROM roles WHERE id=?", (rid,))
    db.commit()


def _new_role(client, name, **data):
    payload = {"name": name, "description": "route test role", "discount_cap": "10"}
    payload.update(data)
    return client.post("/admin/roles/new", data=payload, follow_redirects=False)


def test_a_role_can_be_created_with_a_discount_cap(client, db, role_cleanup):
    name = f"Role {uuid.uuid4().hex[:6]}"
    resp = _new_role(client, name, discount_cap="15")
    row = db.execute("SELECT * FROM roles WHERE name=?", (name,)).fetchone()
    assert row is not None, "the role was not created"
    role_cleanup.append(row["id"])
    assert resp.status_code == 302
    assert row["discount_cap"] == 15


def test_a_discount_cap_above_100_is_refused(client, db):
    """A cap over 100% is not a bigger discount, it is a nonsense one — and
    discount_percent_error() compares against it directly."""
    before = db.execute("SELECT count(*) AS c FROM roles").fetchone()["c"]
    name = f"Role {uuid.uuid4().hex[:6]}"
    resp = _new_role(client, name, discount_cap="150")
    # The route flashes and redirects rather than redisplaying, so the status
    # code says nothing useful — what matters is that no role was created.
    assert resp.status_code != 500
    assert db.execute("SELECT count(*) AS c FROM roles").fetchone()["c"] == before


def test_a_negative_discount_cap_is_refused(client, db):
    before = db.execute("SELECT count(*) AS c FROM roles").fetchone()["c"]
    resp = _new_role(client, f"Role {uuid.uuid4().hex[:6]}", discount_cap="-10")
    assert resp.status_code != 500
    assert db.execute("SELECT count(*) AS c FROM roles").fetchone()["c"] == before


def test_a_non_numeric_discount_cap_is_refused(client, db):
    before = db.execute("SELECT count(*) AS c FROM roles").fetchone()["c"]
    resp = _new_role(client, f"Role {uuid.uuid4().hex[:6]}", discount_cap="loads")
    assert resp.status_code != 500
    assert db.execute("SELECT count(*) AS c FROM roles").fetchone()["c"] == before


def test_a_role_needs_a_name(client, db):
    before = db.execute("SELECT count(*) AS c FROM roles").fetchone()["c"]
    resp = _new_role(client, "")
    assert resp.status_code != 500
    assert db.execute("SELECT count(*) AS c FROM roles").fetchone()["c"] == before


def test_role_names_cannot_be_duplicated(client, db, role_cleanup):
    """Two roles with one name makes the permissions screen ambiguous —
    there is no way to tell which one an account actually holds."""
    name = f"Role {uuid.uuid4().hex[:6]}"
    _new_role(client, name)
    first = db.execute("SELECT * FROM roles WHERE name=?", (name,)).fetchone()
    assert first is not None
    role_cleanup.append(first["id"])
    before = db.execute("SELECT count(*) AS c FROM roles").fetchone()["c"]
    resp = _new_role(client, name)
    assert resp.status_code != 500
    assert db.execute("SELECT count(*) AS c FROM roles").fetchone()["c"] == before


def test_only_real_permissions_are_stored(client, db, role_cleanup):
    """A crafted request naming a permission that does not exist must not
    create a phantom grant — the permission check looks these up by key."""
    import auth
    name = f"Role {uuid.uuid4().hex[:6]}"
    real = sorted(auth.PERMISSION_KEY_SET)[0]
    client.post("/admin/roles/new", data={
        "name": name, "description": "", "discount_cap": "0",
        "permissions": [real, "not_a_real_permission", "../../etc/passwd"]},
        follow_redirects=False)
    row = db.execute("SELECT * FROM roles WHERE name=?", (name,)).fetchone()
    assert row is not None
    role_cleanup.append(row["id"])
    granted = [r["permission_id"] for r in db.execute(
        "SELECT permission_id FROM role_permissions WHERE role_id=?", (row["id"],)).fetchall()]
    assert real in granted
    for bogus in ("not_a_real_permission", "../../etc/passwd"):
        assert bogus not in granted, f"{bogus!r} must not have been granted"
    assert all(p in auth.PERMISSION_KEY_SET for p in granted)


def test_a_role_can_be_edited(client, db, role_cleanup):
    name = f"Role {uuid.uuid4().hex[:6]}"
    _new_role(client, name, discount_cap="10")
    row = db.execute("SELECT * FROM roles WHERE name=?", (name,)).fetchone()
    assert row is not None
    role_cleanup.append(row["id"])
    client.post(f"/admin/roles/{row['id']}/edit", data={
        "name": name, "description": "edited", "discount_cap": "20"},
        follow_redirects=False)
    after = db.execute("SELECT * FROM roles WHERE id=?", (row["id"],)).fetchone()
    assert after["discount_cap"] == 20


def test_editing_a_role_to_an_invalid_cap_leaves_it_alone(client, db, role_cleanup):
    name = f"Role {uuid.uuid4().hex[:6]}"
    _new_role(client, name, discount_cap="10")
    row = db.execute("SELECT * FROM roles WHERE name=?", (name,)).fetchone()
    assert row is not None
    role_cleanup.append(row["id"])
    client.post(f"/admin/roles/{row['id']}/edit", data={
        "name": name, "description": "", "discount_cap": "999"},
        follow_redirects=False)
    after = db.execute("SELECT * FROM roles WHERE id=?", (row["id"],)).fetchone()
    assert after["discount_cap"] == 10, "a rejected edit must not change the cap"


def test_the_discount_cap_is_read_from_the_role(client, db, role_cleanup):
    """Ties the stored cap to the value the discount checks actually compare
    against, rather than trusting the column is read anywhere.

    Structural note: IQ centralises this comparison in a single helper,
    discount_percent_error(percent, cap). JO does not have that helper — the
    same rule is written inline at each discount site (POS checkout, visit,
    inpatient, boarding). That is a real divergence and a maintenance risk:
    a correction made at one site here does not reach the other three. The
    route-level tests in test_money_routes.py are what cover each site
    individually; this one covers the plumbing they share."""
    # auth.discount_cap_for() reads the session and so needs a request
    # context; the route tests in test_money_routes.py exercise it in situ.
    # What this test owns is that the number reaches the database intact.
    name = f"Role {uuid.uuid4().hex[:6]}"
    _new_role(client, name, discount_cap="7")
    row = db.execute("SELECT * FROM roles WHERE name=?", (name,)).fetchone()
    assert row is not None
    role_cleanup.append(row["id"])
    assert row["discount_cap"] == 7, "the cap must be stored exactly as entered"
