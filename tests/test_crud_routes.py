"""
Create / edit / delete tests for the everyday records.

These are the routes staff touch dozens of times a day — owners, patients,
appointments, the price list — and until now none of them was exercised by
anything. They are not money routes (those live in test_money_routes.py),
but they are where a validation guard or a uniqueness rule quietly stops
working, and where a bad row starts the chain that produces a wrong bill
later.

Like the money route tests, these need a throwaway Postgres and skip
cleanly without one. See conftest.py.
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from conftest import needs_db


pytestmark = needs_db


def _uid(prefix):
    return f"{prefix}{uuid.uuid4().hex[:8].upper()}"


def _phone():
    """A local-format mobile number of exactly PHONE_LOCAL_LENGTH digits,
    unique per call so tests never collide with a number already on file.

    Phone format is one of the two apps' deliberate divergences, so the
    length is read from the app rather than hardcoded here — this helper
    works unchanged in both."""
    import app as app_module
    # PHONE_LOCAL_LENGTH counts digits AFTER the leading trunk 0 is stripped,
    # so the string itself carries one more: "0" + LENGTH digits. Verified
    # against normalize_phone() rather than assumed.
    body = str(uuid.uuid4().int)[:app_module.PHONE_LOCAL_LENGTH - 1].ljust(
        app_module.PHONE_LOCAL_LENGTH - 1, "0")
    return "07" + body


# ---------------------------------------------------------------------------
# Owners
# ---------------------------------------------------------------------------

@pytest.fixture
def cleanup_owners(db):
    created = []
    yield created
    for oid in created:
        db.execute("DELETE FROM patients WHERE owner_id=?", (oid,))
        db.execute("DELETE FROM owners WHERE id=?", (oid,))
    db.commit()


def test_owner_can_be_created(client, db, cleanup_owners):
    name = f"Test Owner {uuid.uuid4().hex[:6]}"
    resp = client.post("/owners/new", data={
        "name": name, "phone": _phone(), "address": "Somewhere", "notes": ""},
        follow_redirects=False)
    assert resp.status_code == 302, "a successful create redirects"
    row = db.execute("SELECT * FROM owners WHERE name=?", (name,)).fetchone()
    assert row is not None
    cleanup_owners.append(row["id"])


def test_owner_rejects_an_invalid_phone_number(client, db):
    """Phone format is one of the two apps' deliberate divergences, so this
    asserts only that a clearly bad value is refused — not the exact rule."""
    before = db.execute("SELECT count(*) AS c FROM owners").fetchone()["c"]
    resp = client.post("/owners/new", data={
        "name": "Bad Phone Owner", "phone": "not-a-phone-number", "address": ""},
        follow_redirects=False)
    assert resp.status_code == 200, "should redisplay the form, not save"
    assert db.execute("SELECT count(*) AS c FROM owners").fetchone()["c"] == before


def test_a_duplicate_phone_sends_staff_to_the_existing_owner(client, db, cleanup_owners):
    """Two owners sharing a phone number is how a pet ends up filed under the
    wrong client. The app does not merely refuse — it redirects to whoever
    already holds that number, because "add another pet to them" is almost
    always what was meant. What matters is that no second owner is created."""
    phone = _phone()
    name = f"First Owner {uuid.uuid4().hex[:6]}"
    client.post("/owners/new", data={"name": name, "phone": phone, "address": ""},
                follow_redirects=False)
    first = db.execute("SELECT * FROM owners WHERE name=?", (name,)).fetchone()
    assert first is not None, "the first owner should have been created"
    cleanup_owners.append(first["id"])

    before = db.execute("SELECT count(*) AS c FROM owners").fetchone()["c"]
    resp = client.post("/owners/new", data={
        "name": "Second Owner", "phone": phone, "address": ""}, follow_redirects=False)

    assert db.execute("SELECT count(*) AS c FROM owners").fetchone()["c"] == before, (
        "a second owner must not be created for a phone number already on file")
    assert resp.status_code == 302
    assert first["id"] in resp.headers["Location"], (
        "should redirect to the owner who already holds this number")
    # Stored normalized to E.164, not as typed — query the stored form.
    import app as app_module
    stored = app_module.normalize_phone(phone)
    assert db.execute("SELECT count(*) AS c FROM owners WHERE phone=?",
                      (stored,)).fetchone()["c"] == 1


def test_owner_can_be_edited(client, db, cleanup_owners):
    name = f"Edit Owner {uuid.uuid4().hex[:6]}"
    client.post("/owners/new", data={"name": name, "phone": _phone(), "address": "Old"},
                follow_redirects=False)
    row = db.execute("SELECT * FROM owners WHERE name=?", (name,)).fetchone()
    cleanup_owners.append(row["id"])
    client.post(f"/owners/{row['id']}/edit", data={
        "name": name, "phone": row["phone"], "address": "New Address", "notes": "changed"},
        follow_redirects=False)
    after = db.execute("SELECT * FROM owners WHERE id=?", (row["id"],)).fetchone()
    assert after["address"] == "New Address"


def test_editing_a_missing_owner_degrades(client, db):
    resp = client.post("/owners/NOPE-NOT-AN-OWNER/edit", data={
        "name": "Ghost", "phone": _phone(), "address": ""}, follow_redirects=False)
    assert resp.status_code != 500


# ---------------------------------------------------------------------------
# Appointments
# ---------------------------------------------------------------------------

@pytest.fixture
def appointment_cleanup(db):
    created = []
    yield created
    for aid in created:
        db.execute("DELETE FROM appointments WHERE id=?", (aid,))
    db.commit()


def _book(client, **data):
    """A booking that satisfies every guard on the route.

    Values are not invented: resource_type must be one of RESOURCE_TYPES
    ('vet'/'grooming'), appointment_type one of APPOINTMENT_TYPES, and
    slot_label must match a slot generate_slots() actually produces — the
    route re-checks all three, so a made-up value is rejected before
    anything is written. 'grooming' is used deliberately because the 'vet'
    path additionally requires a valid active vet id."""
    payload = {
        "appt_date": (date.today() + timedelta(days=1)).isoformat(),
        "slot_label": "09:00",
        "resource_type": "grooming",
        "pet_name": "Rex",
        "owner_name": "Someone",
        "appointment_type": "Grooming",
    }
    payload.update(data)
    return client.post("/appointments/new", data=payload, follow_redirects=False)


def test_appointment_can_be_booked(client, db, appointment_cleanup):
    pet = f"Pet{uuid.uuid4().hex[:6]}"
    resp = _book(client, pet_name=pet)
    row = db.execute("SELECT * FROM appointments WHERE pet_name=?", (pet,)).fetchone()
    assert row is not None, "the booking was rejected — check the guards in _book()"
    appointment_cleanup.append(row["id"])
    assert resp.status_code == 302
    assert row["resource_type"] == "grooming"
    assert row["slot_label"] == "09:00"


def test_two_appointments_cannot_take_the_same_slot(client, db, appointment_cleanup):
    """Double-booking one groomer at one time is a real-world scheduling
    error the grid cannot show, because both rows look valid on their own."""
    slot, when = "10:30", (date.today() + timedelta(days=2)).isoformat()
    first = f"Pet{uuid.uuid4().hex[:6]}"
    _book(client, pet_name=first, slot_label=slot, appt_date=when)
    row = db.execute("SELECT * FROM appointments WHERE pet_name=?", (first,)).fetchone()
    assert row is not None, "the first booking should have succeeded"
    appointment_cleanup.append(row["id"])

    before = db.execute("SELECT count(*) AS c FROM appointments").fetchone()["c"]
    second = f"Pet{uuid.uuid4().hex[:6]}"
    resp = _book(client, pet_name=second, slot_label=slot, appt_date=when)
    after = db.execute("SELECT count(*) AS c FROM appointments").fetchone()["c"]
    if after > before:
        dup = db.execute("SELECT * FROM appointments WHERE pet_name=?", (second,)).fetchone()
        appointment_cleanup.append(dup["id"])
        pytest.fail("the same grooming slot was booked twice on the same day")
    assert resp.status_code == 200


def test_appointment_requires_a_date(client, db):
    before = db.execute("SELECT count(*) AS c FROM appointments").fetchone()["c"]
    resp = _book(client, appt_date="")
    assert resp.status_code == 200
    assert db.execute("SELECT count(*) AS c FROM appointments").fetchone()["c"] == before


def test_appointment_rejects_a_malformed_date(client, db):
    """The prefix-truncation bug fixed in v1.10.1 lived exactly here — a
    value with a valid 10-character start and junk after it."""
    before = db.execute("SELECT count(*) AS c FROM appointments").fetchone()["c"]
    for bad in ("not-a-date", "2026-08-25garbage", "2026-13-99"):
        resp = _book(client, appt_date=bad)
        assert resp.status_code != 500, f"{bad!r} must not raise"
    assert db.execute("SELECT count(*) AS c FROM appointments").fetchone()["c"] == before


def test_appointment_rejects_an_unknown_resource_type(client, db):
    """Backed twice over: the route validates against RESOURCE_TYPES, and
    appointments has a CHECK constraint on the column. Disabling either one
    alone still produces the right answer — which is the design working, not
    a hole. This test asserts the outcome, so it holds whichever layer is
    doing the work."""
    before = db.execute("SELECT count(*) AS c FROM appointments").fetchone()["c"]
    resp = _book(client, resource_type="Spaceship")
    assert resp.status_code == 200
    assert db.execute("SELECT count(*) AS c FROM appointments").fetchone()["c"] == before


def test_appointment_rejects_a_slot_that_is_not_on_the_schedule(client, db):
    """Unlike resource_type and appointment_type, slot_label has NO database
    constraint behind it — generate_slots() is built from the clinic's
    configured opening hours and slot length, so the route's check is the
    only thing standing between a tampered request and an appointment sitting
    outside working hours where the grid will never show it."""
    before = db.execute("SELECT count(*) AS c FROM appointments").fetchone()["c"]
    for bad in ("03:00", "not-a-time", "25:99", ""):
        resp = _book(client, slot_label=bad)
        assert resp.status_code != 500, f"{bad!r} must not raise"
    assert db.execute("SELECT count(*) AS c FROM appointments").fetchone()["c"] == before, (
        "an off-schedule slot must not be bookable")


# ---------------------------------------------------------------------------
# Price list — the table every bill is priced from
# ---------------------------------------------------------------------------

@pytest.fixture
def price_item(client, db):
    name = f"CRUD Item {uuid.uuid4().hex[:6]}"
    client.post("/price-list/new", data={
        "name": name, "category": "Service", "cost_price": "2.000", "sale_price": "10.000"},
        follow_redirects=False)
    row = db.execute("SELECT * FROM price_list WHERE name=?", (name,)).fetchone()
    assert row is not None, "price list item was not created"
    yield row
    db.execute("DELETE FROM price_list WHERE id=?", (row["id"],))
    db.commit()


def test_price_list_item_can_be_created(client, db, price_item):
    assert price_item["sale_price"] == Decimal("10.000")
    assert price_item["category"] == "Service"


def test_price_list_rejects_a_negative_price(client, db):
    """A negative sale price would flow straight into a bill as a credit."""
    before = db.execute("SELECT count(*) AS c FROM price_list").fetchone()["c"]
    resp = client.post("/price-list/new", data={
        "name": "Negative Item", "category": "Service",
        "cost_price": "2.000", "sale_price": "-10.000"}, follow_redirects=False)
    assert resp.status_code == 200
    assert db.execute("SELECT count(*) AS c FROM price_list").fetchone()["c"] == before


def test_price_list_rejects_a_non_numeric_price(client, db):
    before = db.execute("SELECT count(*) AS c FROM price_list").fetchone()["c"]
    resp = client.post("/price-list/new", data={
        "name": "Bad Price Item", "category": "Service",
        "cost_price": "abc", "sale_price": "xyz"}, follow_redirects=False)
    assert resp.status_code == 200
    assert db.execute("SELECT count(*) AS c FROM price_list").fetchone()["c"] == before


def test_price_list_item_can_be_edited(client, db, price_item):
    client.post(f"/price-list/{price_item['id']}/edit", data={
        "name": price_item["name"], "category": "Service",
        "cost_price": "3.000", "sale_price": "15.000"}, follow_redirects=False)
    row = db.execute("SELECT * FROM price_list WHERE id=?", (price_item["id"],)).fetchone()
    assert row["sale_price"] == Decimal("15.000")


def test_editing_a_price_to_something_invalid_leaves_it_alone(client, db, price_item):
    """A rejected edit must not partially apply — the old price stands."""
    client.post(f"/price-list/{price_item['id']}/edit", data={
        "name": price_item["name"], "category": "Service",
        "cost_price": "3.000", "sale_price": "-1.000"}, follow_redirects=False)
    row = db.execute("SELECT * FROM price_list WHERE id=?", (price_item["id"],)).fetchone()
    assert row["sale_price"] == Decimal("10.000"), "the original price must survive a rejected edit"


def test_price_list_item_can_be_deleted(client, db):
    name = f"Doomed Item {uuid.uuid4().hex[:6]}"
    client.post("/price-list/new", data={
        "name": name, "category": "Service", "cost_price": "1.000", "sale_price": "5.000"},
        follow_redirects=False)
    row = db.execute("SELECT * FROM price_list WHERE name=?", (name,)).fetchone()
    assert row is not None
    client.post(f"/price-list/{row['id']}/delete", data={}, follow_redirects=False)
    after = db.execute("SELECT * FROM price_list WHERE id=?", (row["id"],)).fetchone()
    if after is not None:
        # Some builds deactivate rather than delete; either is a valid answer
        # so long as the item stops being sellable.
        assert after["active"] is False, "a deleted item must not remain active"
        db.execute("DELETE FROM price_list WHERE id=?", (row["id"],))
        db.commit()
