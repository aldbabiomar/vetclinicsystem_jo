"""
The daily workflows: visits, inventory, audits, users.

These are the paths a clinic walks every day — register a new client and
their pet in one form, fill in the visit, count the stock, add a staff
account. They are not money routes, but the visit form is where a bill's
line items come from, the audit is what makes an item sellable at all, and
the user form is what decides who can apply a discount.

Needs a throwaway Postgres; skips cleanly without one. See conftest.py.
"""
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from conftest import needs_db


pytestmark = needs_db


def _uid(prefix):
    return f"{prefix}{uuid.uuid4().hex[:8].upper()}"


def _phone():
    import app as app_module
    body = str(uuid.uuid4().int)[:app_module.PHONE_LOCAL_LENGTH - 1].ljust(
        app_module.PHONE_LOCAL_LENGTH - 1, "0")
    return "07" + body


# ---------------------------------------------------------------------------
# New client + pet + visit, all in one form
# ---------------------------------------------------------------------------

@pytest.fixture
def registered(db):
    """Tracks whatever the one-shot registration form created, so it can be
    unwound in dependency order afterwards."""
    made = {"visits": [], "patients": [], "owners": []}
    yield made
    for vid in made["visits"]:
        for sql in ("DELETE FROM payments WHERE visit_id=?",
                    "DELETE FROM visit_billing_lines WHERE visit_id=?",
                    "DELETE FROM billing WHERE visit_id=?",
                    "DELETE FROM visits WHERE id=?"):
            db.execute(sql, (vid,))
    for pid in made["patients"]:
        db.execute("DELETE FROM patients WHERE id=?", (pid,))
    for oid in made["owners"]:
        db.execute("DELETE FROM owners WHERE id=?", (oid,))
    db.commit()


def test_new_client_pet_and_visit_are_created_together(client, db, registered):
    """One form, three linked records. If any of them lands without the
    others the client exists with no pet, or a pet with no owner — the exact
    orphan shape the schema's foreign keys exist to prevent."""
    owner_name = f"Reg Owner {uuid.uuid4().hex[:6]}"
    animal = f"Reg Pet {uuid.uuid4().hex[:6]}"
    resp = client.post("/visits/new/new-patient", data={
        "owner_name": owner_name, "owner_phone": _phone(), "owner_address": "Somewhere",
        "animal_name": animal, "species": "Dog"}, follow_redirects=False)

    owner = db.execute("SELECT * FROM owners WHERE name=?", (owner_name,)).fetchone()
    assert owner is not None, "the owner was not created"
    registered["owners"].append(owner["id"])
    patient = db.execute("SELECT * FROM patients WHERE animal_name=?", (animal,)).fetchone()
    assert patient is not None, "the patient was not created"
    registered["patients"].append(patient["id"])
    assert patient["owner_id"] == owner["id"], "the pet must be linked to the owner just created"
    assert resp.status_code == 302

    visits = db.execute("SELECT * FROM visits WHERE patient_id=?", (patient["id"],)).fetchall()
    for v in visits:
        registered["visits"].append(v["id"])


def test_registration_rejects_a_bad_phone_without_creating_anything(client, db):
    """Partial creation is the danger: an owner saved, then the pet rejected,
    leaves a client on file who never existed as a client."""
    before_o = db.execute("SELECT count(*) AS c FROM owners").fetchone()["c"]
    before_p = db.execute("SELECT count(*) AS c FROM patients").fetchone()["c"]
    resp = client.post("/visits/new/new-patient", data={
        "owner_name": "Rollback Owner", "owner_phone": "not-a-phone",
        "animal_name": "Rollback Pet", "species": "Cat"}, follow_redirects=False)
    assert resp.status_code == 200
    assert db.execute("SELECT count(*) AS c FROM owners").fetchone()["c"] == before_o
    assert db.execute("SELECT count(*) AS c FROM patients").fetchone()["c"] == before_p


# ---------------------------------------------------------------------------
# Editing a visit
# ---------------------------------------------------------------------------

@pytest.fixture
def a_visit(db):
    o_id, p_id, v_id = _uid("O"), _uid("P"), _uid("V")
    db.execute("INSERT INTO owners (id, name) VALUES (?,?)", (o_id, f"WF Owner {o_id}"))
    db.execute("INSERT INTO patients (id, owner_id, animal_name) VALUES (?,?,?)",
               (p_id, o_id, f"WF Pet {p_id}"))
    db.execute("INSERT INTO visits (id, patient_id, date, case_status) VALUES (?,?,?,?)",
               (v_id, p_id, date.today().isoformat(), "Ongoing"))
    db.commit()
    yield {"visit_id": v_id, "patient_id": p_id, "owner_id": o_id}
    for sql in ("DELETE FROM payments WHERE visit_id=?",
                "DELETE FROM visit_billing_lines WHERE visit_id=?",
                "DELETE FROM billing WHERE visit_id=?",
                "DELETE FROM visits WHERE id=?"):
        db.execute(sql, (v_id,))
    db.execute("DELETE FROM patients WHERE id=?", (p_id,))
    db.execute("DELETE FROM owners WHERE id=?", (o_id,))
    db.commit()


def _current_updated_at(db, visit_id):
    row = db.execute("SELECT updated_at FROM visits WHERE id=?", (visit_id,)).fetchone()
    return "" if row is None or row["updated_at"] is None else str(row["updated_at"])


def _edit_visit(client, visit_id, db=None, **data):
    """The visit form carries the row's updated_at back with it, and the
    route refuses the save if it no longer matches — optimistic locking, so
    two people editing the same visit cannot silently overwrite each other.
    A test that omits it is rejected before reaching any of the validation
    it meant to exercise."""
    # visit_type is not optional — the route rejects anything that is not
    # Outpatient or Inpatient, before it reaches the validation this helper
    # exists to exercise.
    payload = {"date": date.today().isoformat(), "case_status": "Ongoing",
               "visit_type": "Outpatient", "doctor": "", "complaint": "",
               "history": "", "exam": "", "treatment": ""}
    if db is not None:
        payload["expected_updated_at"] = _current_updated_at(db, visit_id)
    payload.update(data)
    return client.post(f"/visits/{visit_id}/edit", data=payload, follow_redirects=False)


def test_visit_can_be_edited(client, db, a_visit):
    _edit_visit(client, a_visit["visit_id"], db, complaint="Updated complaint", doctor="Dr Test")
    row = db.execute("SELECT * FROM visits WHERE id=?", (a_visit["visit_id"],)).fetchone()
    assert row["complaint"] == "Updated complaint"
    assert row["doctor"] == "Dr Test"


def test_visit_edit_rejects_an_unknown_case_status(client, db, a_visit):
    """case_status is constrained in the database as well, so this asserts
    the outcome rather than which layer refused it."""
    _edit_visit(client, a_visit["visit_id"], db, case_status="Teleported")
    row = db.execute("SELECT * FROM visits WHERE id=?", (a_visit["visit_id"],)).fetchone()
    assert row["case_status"] == "Ongoing", "the original status must stand"


def test_visit_edit_rejects_a_malformed_date(client, db, a_visit):
    """The v1.10.1 prefix-truncation shape, on a write path this time."""
    original = db.execute("SELECT date FROM visits WHERE id=?", (a_visit["visit_id"],)).fetchone()["date"]
    for bad in ("not-a-date", "2026-08-25garbage", "2026-13-99"):
        resp = _edit_visit(client, a_visit["visit_id"], db, date=bad)
        assert resp.status_code != 500, f"{bad!r} must not raise"
    row = db.execute("SELECT date FROM visits WHERE id=?", (a_visit["visit_id"],)).fetchone()
    assert row["date"] == original, "a rejected edit must not change the date"


def test_visit_edit_rejects_an_out_of_range_bcs(client, db, a_visit):
    """Body condition score is 1-9, enforced by a CHECK constraint. A value
    outside it must be refused rather than reaching the database raw."""
    for bad in ("0", "10", "99"):
        resp = _edit_visit(client, a_visit["visit_id"], db, bcs=bad)
        assert resp.status_code != 500, f"bcs={bad} must not raise"
    row = db.execute("SELECT bcs FROM visits WHERE id=?", (a_visit["visit_id"],)).fetchone()
    assert row["bcs"] is None or 1 <= row["bcs"] <= 9


def test_visit_edit_accepts_a_valid_bcs(client, db, a_visit):
    _edit_visit(client, a_visit["visit_id"], db, bcs="5", weight_kg="12.5")
    row = db.execute("SELECT * FROM visits WHERE id=?", (a_visit["visit_id"],)).fetchone()
    assert row["bcs"] == 5
    assert row["weight_kg"] == 12.5


def test_editing_a_missing_visit_degrades(client, db):
    resp = _edit_visit(client, "NOPE-NOT-A-VISIT", complaint="ghost")
    assert resp.status_code != 500


# ---------------------------------------------------------------------------
# Inventory catalog and audits — what makes an item sellable
# ---------------------------------------------------------------------------

@pytest.fixture
def catalog_cleanup(db):
    created = []
    yield created
    for iid in created:
        for sql in ("DELETE FROM inventory_transactions WHERE item_id=?",
                    "DELETE FROM audit_session_lines WHERE item_id=?",
                    "DELETE FROM inventory_list WHERE id=?"):
            db.execute(sql, (iid,))
    db.commit()


def test_inventory_item_can_be_created(client, db, catalog_cleanup):
    name = f"Catalog Item {uuid.uuid4().hex[:6]}"
    resp = client.post("/inventory-catalog/new", data={
        "name": name, "category": "Retail", "unit": "unit",
        "track_expiry": "on", "cost_price": "3.000", "notes": ""}, follow_redirects=False)
    row = db.execute("SELECT * FROM inventory_list WHERE name=?", (name,)).fetchone()
    assert row is not None, "the item was not created"
    catalog_cleanup.append(row["id"])
    assert resp.status_code == 302
    assert row["cost_price"] == Decimal("3.000")


def test_inventory_item_rejects_a_negative_cost(client, db):
    before = db.execute("SELECT count(*) AS c FROM inventory_list").fetchone()["c"]
    resp = client.post("/inventory-catalog/new", data={
        "name": "Negative Cost Item", "category": "Retail", "unit": "unit",
        "cost_price": "-5.000"}, follow_redirects=False)
    assert resp.status_code == 200
    assert db.execute("SELECT count(*) AS c FROM inventory_list").fetchone()["c"] == before


def test_a_confirmed_audit_is_what_gives_an_item_a_stock_figure(client, db, catalog_cleanup):
    """The rule POS depends on: current_stock is None until an item has been
    through a confirmed audit, and pos_checkout refuses to sell anything
    whose stock is unknown. This pins the mechanism behind that guard."""
    import logic
    name = f"Audit Item {uuid.uuid4().hex[:6]}"
    client.post("/inventory-catalog/new", data={
        "name": name, "category": "Retail", "unit": "unit", "cost_price": "2.000"},
        follow_redirects=False)
    row = db.execute("SELECT * FROM inventory_list WHERE name=?", (name,)).fetchone()
    assert row is not None
    catalog_cleanup.append(row["id"])

    before = logic.inventory_status_by_id(db, row["id"])
    assert before is None or before["current_stock"] is None, (
        "a never-audited item must not have a stock figure")

    cur = db.execute("INSERT INTO audit_sessions (audit_date, performed_by, status, created_at, confirmed_at) "
                     "VALUES (?,?,?,?,?) RETURNING id",
                     (date.today().isoformat(), "U001", "Confirmed",
                      datetime.now().isoformat(timespec="seconds"),
                      datetime.now().isoformat(timespec="microseconds")))
    sid = cur.fetchone()["id"]
    db.execute("INSERT INTO audit_session_lines (session_id, item_id, stock_counted, received_since_prior) "
               "VALUES (?,?,?,?)", (sid, row["id"], Decimal("25.000"), Decimal(0)))
    db.commit()
    try:
        after = logic.inventory_status_by_id(db, row["id"])
        assert after is not None and after["current_stock"] == Decimal("25.000"), (
            "a confirmed audit must establish the stock figure")
    finally:
        db.execute("DELETE FROM audit_session_lines WHERE session_id=?", (sid,))
        db.execute("DELETE FROM audit_sessions WHERE id=?", (sid,))
        db.commit()


# ---------------------------------------------------------------------------
# Staff accounts — who is allowed to discount
# ---------------------------------------------------------------------------

@pytest.fixture
def user_cleanup(db):
    created = []
    yield created
    for uid in created:
        db.execute("DELETE FROM login_log WHERE user_id=?", (uid,))
        db.execute("DELETE FROM users WHERE id=?", (uid,))
    db.commit()


def test_staff_account_can_be_created(client, db, user_cleanup):
    username = f"staff{uuid.uuid4().hex[:6]}"
    role = db.execute("SELECT id FROM roles ORDER BY id LIMIT 1").fetchone()
    resp = client.post("/admin/users/new", data={
        "username": username, "full_name": "Test Staff",
        "password": "TestPass12345!", "role_id": role["id"], "capmode": "role"},
        follow_redirects=False)
    row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    assert row is not None, "the account was not created"
    user_cleanup.append(row["id"])
    assert resp.status_code == 302


def test_a_new_account_never_stores_the_password_in_the_clear(client, db, user_cleanup):
    """The single most damaging thing this form could get wrong."""
    username = f"staff{uuid.uuid4().hex[:6]}"
    password = "PlaintextCheck12345!"
    role = db.execute("SELECT id FROM roles ORDER BY id LIMIT 1").fetchone()
    client.post("/admin/users/new", data={
        "username": username, "full_name": "Hash Check",
        "password": password, "role_id": role["id"], "capmode": "role"},
        follow_redirects=False)
    row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    assert row is not None
    user_cleanup.append(row["id"])
    assert row["password_hash"] != password
    assert password not in str(dict(row)), "the password must not appear anywhere on the row"


def test_usernames_cannot_be_duplicated(client, db, user_cleanup):
    """Two accounts with one username makes the audit trail meaningless —
    every change is attributed to an ambiguous person."""
    username = f"staff{uuid.uuid4().hex[:6]}"
    role = db.execute("SELECT id FROM roles ORDER BY id LIMIT 1").fetchone()
    for _ in range(2):
        client.post("/admin/users/new", data={
            "username": username, "full_name": "Dup Check",
            "password": "TestPass12345!", "role_id": role["id"], "capmode": "role"},
            follow_redirects=False)
    rows = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchall()
    for r in rows:
        user_cleanup.append(r["id"])
    assert len(rows) == 1, "a duplicate username must not be created"


def test_a_stale_edit_is_refused_rather_than_overwriting(client, db, a_visit):
    """Two people open the same visit; one saves; the second saves over the
    top. Without this guard the first person's changes vanish with no trace
    and no warning — the classic lost update. The route compares the
    updated_at the form was rendered with against the row as it stands now."""
    stale = _current_updated_at(db, a_visit["visit_id"])
    _edit_visit(client, a_visit["visit_id"], db, complaint="First edit wins")
    saved = db.execute("SELECT * FROM visits WHERE id=?", (a_visit["visit_id"],)).fetchone()
    assert saved["complaint"] == "First edit wins"

    resp = client.post(f"/visits/{a_visit['visit_id']}/edit", data={
        "date": date.today().isoformat(), "case_status": "Ongoing",
        "visit_type": "Outpatient", "doctor": "", "complaint": "Second edit clobbers",
        "history": "", "exam": "", "treatment": "",
        "expected_updated_at": stale}, follow_redirects=False)
    # Assert the outcome, not the status code: the route flashes the conflict
    # and redirects rather than redisplaying, and either is a fine answer.
    # What must never happen is the second save landing.
    assert resp.status_code != 500
    after = db.execute("SELECT * FROM visits WHERE id=?", (a_visit["visit_id"],)).fetchone()
    assert after["complaint"] == "First edit wins", (
        "the second save must not overwrite the first")


def test_the_stale_edit_guard_only_engages_once_a_row_has_been_edited(client, db, a_visit):
    """Deliberate, and worth pinning: updated_at is NULL until the first
    edit, and stale_edit_error() treats NULL as "nothing to compare against"
    so the first save always proceeds. Without this test the guard looks
    broken on a fresh row when it is in fact behaving as designed."""
    assert _current_updated_at(db, a_visit["visit_id"]) == "", "a new visit has no edit stamp"
    resp = _edit_visit(client, a_visit["visit_id"], db, complaint="First ever edit")
    assert resp.status_code == 302
    row = db.execute("SELECT * FROM visits WHERE id=?", (a_visit["visit_id"],)).fetchone()
    assert row["complaint"] == "First ever edit"
    assert row["updated_at"], "the first edit must stamp updated_at, or the guard never engages"


# ---------------------------------------------------------------------------
# Negative-value guards
#
# These exist because a negative cost makes an item look infinitely
# profitable everywhere margin and COGS are calculated, and a negative
# weight is not a measurement. Each guard below was found missing in JO by
# porting this file across; all five now have a test so none can be removed
# without something failing.
# ---------------------------------------------------------------------------

def test_bulk_edit_rejects_a_negative_cost(client, db, catalog_cleanup):
    """The bulk editor is the *easier* way to set a negative cost, not the
    harder one — it takes JSON and skips the form entirely."""
    name = f"Bulk Item {uuid.uuid4().hex[:6]}"
    client.post("/inventory-catalog/new", data={
        "name": name, "category": "Retail", "unit": "unit", "cost_price": "2.000"},
        follow_redirects=False)
    row = db.execute("SELECT * FROM inventory_list WHERE name=?", (name,)).fetchone()
    assert row is not None
    catalog_cleanup.append(row["id"])

    # The payload must be otherwise VALID, name included, or the row is
    # rejected for a missing name and the cost never gets looked at — which
    # makes the test pass whether the guard exists or not.
    ok = client.post("/inventory-catalog/bulk-edit", json={
        "items": [{"id": row["id"], "fields": {"name": name, "cost_price": "2.000"}}]})
    assert ok.get_json().get("ok"), f"control edit should succeed: {ok.get_json()}"

    resp = client.post("/inventory-catalog/bulk-edit", json={
        "items": [{"id": row["id"], "fields": {"name": name, "cost_price": "-5.000"}}]})
    assert resp.status_code < 500
    assert not resp.get_json().get("ok"), "a negative cost must be reported as an error"
    after = db.execute("SELECT * FROM inventory_list WHERE id=?", (row["id"],)).fetchone()
    assert after["cost_price"] >= 0, "a negative cost must not be saved by the bulk editor"


def test_a_visit_cannot_record_a_negative_weight(client, db, a_visit):
    """Weight feeds the patient's chart and every trend drawn from it."""
    _edit_visit(client, a_visit["visit_id"], db, weight_kg="-12.5")
    row = db.execute("SELECT weight_kg FROM visits WHERE id=?", (a_visit["visit_id"],)).fetchone()
    assert row["weight_kg"] is None or row["weight_kg"] >= 0


def test_an_inpatient_admission_cannot_record_a_negative_weight(client, db, a_visit):
    before = db.execute("SELECT count(*) AS c FROM inpatient_cases").fetchone()["c"]
    resp = client.post("/inpatient/new", data={
        "patient_id": a_visit["patient_id"],
        "admission_date": date.today().isoformat(),
        "weight_kg": "-8", "bcs": "5", "complaint": "neg weight"},
        follow_redirects=False)
    assert resp.status_code != 500
    after = db.execute("SELECT count(*) AS c FROM inpatient_cases").fetchone()["c"]
    if after > before:
        row = db.execute("SELECT * FROM inpatient_cases ORDER BY id DESC LIMIT 1").fetchone()
        try:
            assert (row["weight_kg"] is None or row["weight_kg"] >= 0), (
                "a negative admission weight must not be stored")
        finally:
            db.execute("DELETE FROM inpatient_cases WHERE id=?", (row["id"],))
            db.commit()


def test_consignment_bulk_edit_rejects_a_negative_cost(client, db, catalog_cleanup):
    """Flagging an item as Consignment requires a distributor and a cost, and
    the cost must not be negative — a negative consignment cost would make
    every settlement owed to that distributor come out wrong.

    Note the field names: this route reads `is_consignment` and
    `distributor_id`, not `ownership_type`. A payload naming the wrong field
    is skipped silently with ok:true and nothing saved, which reads as a
    passing test while exercising nothing."""
    name = f"Consign Item {uuid.uuid4().hex[:6]}"
    dist = db.execute("SELECT id FROM distributors ORDER BY id LIMIT 1").fetchone()
    if dist is None:
        did = _uid("D")
        db.execute("INSERT INTO distributors (id, name) VALUES (?,?)", (did, f"Test Dist {did}"))
        db.commit()
    else:
        did = dist["id"]
    client.post("/inventory-catalog/new", data={
        "name": name, "category": "Retail", "unit": "unit", "cost_price": "2.000"},
        follow_redirects=False)
    row = db.execute("SELECT * FROM inventory_list WHERE name=?", (name,)).fetchone()
    assert row is not None
    catalog_cleanup.append(row["id"])

    payload = {"is_consignment": "on", "distributor_id": did, "cost_price": "-5.000"}
    resp = client.post("/consignment/items/bulk-edit",
                       json={"items": [{"id": row["id"], "fields": payload}]})
    assert resp.status_code < 500
    body = resp.get_json()
    assert body.get("errors", {}).get(row["id"]), (
        f"a negative consignment cost must be reported as an error, got {body}")
    after = db.execute("SELECT * FROM inventory_list WHERE id=?", (row["id"],)).fetchone()
    assert after["cost_price"] >= 0, "a negative cost must not be saved here either"


def test_a_new_visit_cannot_record_a_negative_weight(client, db):
    """The guard in _parse_visit_fields covers the *creation* paths only —
    visit_edit carries its own separate weight check, so an edit-based test
    cannot reach this one. Registering a new client and pet is the path that
    goes through it, and it had no guard at all before this."""
    before_o = db.execute("SELECT count(*) AS c FROM owners").fetchone()["c"]
    owner_name = f"NegW Owner {uuid.uuid4().hex[:6]}"
    animal = f"NegW Pet {uuid.uuid4().hex[:6]}"
    resp = client.post("/visits/new/new-patient", data={
        "owner_name": owner_name, "owner_phone": _phone(), "owner_address": "",
        "animal_name": animal, "species": "Dog",
        "weight_kg": "-15", "bcs": "5"}, follow_redirects=False)
    assert resp.status_code != 500

    owner = db.execute("SELECT * FROM owners WHERE name=?", (owner_name,)).fetchone()
    try:
        if owner is not None:
            patient = db.execute("SELECT * FROM patients WHERE animal_name=?", (animal,)).fetchone()
            if patient is not None:
                rows = db.execute("SELECT weight_kg FROM visits WHERE patient_id=?",
                                  (patient["id"],)).fetchall()
                for r in rows:
                    assert r["weight_kg"] is None or r["weight_kg"] >= 0, (
                        "a negative weight must not reach the chart")
    finally:
        if owner is not None:
            patient = db.execute("SELECT * FROM patients WHERE animal_name=?", (animal,)).fetchone()
            if patient is not None:
                db.execute("DELETE FROM visits WHERE patient_id=?", (patient["id"],))
                db.execute("DELETE FROM patients WHERE id=?", (patient["id"],))
            db.execute("DELETE FROM owners WHERE id=?", (owner["id"],))
            db.commit()
