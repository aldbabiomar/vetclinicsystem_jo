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
# Patients — microchip number
# ---------------------------------------------------------------------------
# Optional, unique when present, and searchable however it was typed. Each
# guard below is paired with a control, because "refused" and "refused for
# the right reason" are otherwise indistinguishable (CLAUDE.md §7.3): a field
# that had silently become required, or an index that rejected everything,
# would pass a suite made only of the negative cases.


def _chip(suffix):
    """A unique 15-digit chip. Uniqueness is enforced by a real index, so a
    hardcoded number would make these tests fail the second time they run
    against the same database."""
    return ("9851" + uuid.uuid4().int.__str__())[:15 - len(suffix)] + suffix


@pytest.fixture
def chip_patient(db):
    """One owner + one patient, removed afterwards. Created directly rather
    than through the visit form: these tests are about the microchip field,
    not about visit creation."""
    oid, pid = _uid("OW"), _uid("PT")
    db.execute("INSERT INTO owners (id,name) VALUES (?,?)", (oid, "Chip Test Owner"))
    db.execute("INSERT INTO patients (id,owner_id,animal_name,species) VALUES (?,?,?,?)",
               (pid, oid, "Chip Test Pet", "Dog"))
    db.commit()
    yield pid
    db.execute("DELETE FROM patients WHERE owner_id=?", (oid,))
    db.execute("DELETE FROM owners WHERE id=?", (oid,))
    db.commit()


def _edit(client, pid, **extra):
    data = {"animal_name": "Chip Test Pet", "species": "Dog", "notes": ""}
    data.update(extra)
    return client.post(f"/patients/{pid}/edit", data=data, follow_redirects=False)


def test_a_microchip_is_stored_normalized_not_as_typed(client, db, chip_patient):
    """Staff type a chip the way it is grouped on the scanner. Stored
    verbatim, two spellings of one chip are two different values — the
    search misses them and the unique index cannot see them as duplicates."""
    chip = _chip("11")
    spaced = f"{chip[:3]} {chip[3:6]}-{chip[6:9]} {chip[9:]}"
    resp = _edit(client, chip_patient, microchip=spaced)
    assert resp.status_code == 302, "a valid save redirects"
    stored = db.execute("SELECT microchip FROM patients WHERE id=?", (chip_patient,)).fetchone()["microchip"]
    assert stored == chip, f"expected the separators stripped, stored {stored!r}"


def test_a_patient_saves_with_no_microchip_at_all(client, db, chip_patient):
    """The control for every rejection test below: the field is OPTIONAL.
    Without this, a change that made it required would still pass all of
    them."""
    resp = _edit(client, chip_patient, microchip="", age_note="no chip on this one")
    assert resp.status_code == 302, "a patient with no microchip must still save"
    row = db.execute("SELECT microchip, age_note FROM patients WHERE id=?", (chip_patient,)).fetchone()
    assert row["microchip"] is None, "blank must store NULL, not an empty string"
    assert row["age_note"] == "no chip on this one", "the rest of the form must have saved"


def test_an_existing_microchip_can_be_cleared(client, db, chip_patient):
    """Recorded against the wrong animal, or simply mistyped — staff have to
    be able to take it off again. Blank must land as NULL rather than an empty
    string: idx_patients_microchip_unique ignores NULLs but treats two empty
    strings as the same value, so storing '' would mean the SECOND patient
    anyone cleared could not be saved."""
    chip = _chip("77")
    assert _edit(client, chip_patient, microchip=chip).status_code == 302
    assert db.execute("SELECT microchip FROM patients WHERE id=?",
                      (chip_patient,)).fetchone()["microchip"] == chip

    assert _edit(client, chip_patient, microchip="").status_code == 302
    assert db.execute("SELECT microchip FROM patients WHERE id=?",
                      (chip_patient,)).fetchone()["microchip"] is None, (
        "clearing the field must remove the chip, not store a blank")


def test_a_malformed_microchip_is_refused_and_nothing_is_written(client, db, chip_patient):
    _edit(client, chip_patient, microchip=_chip("22"))
    before = db.execute("SELECT * FROM patients WHERE id=?", (chip_patient,)).fetchone()

    resp = _edit(client, chip_patient, microchip="12", animal_name="Renamed By A Bad Save")
    assert resp.status_code == 200, "should redisplay the form, not save"
    after = db.execute("SELECT * FROM patients WHERE id=?", (chip_patient,)).fetchone()
    assert after["microchip"] == before["microchip"], "the old chip must survive a rejected save"
    assert after["animal_name"] == before["animal_name"], (
        "a rejected save must not write ANY field — not just the invalid one")


def test_resaving_a_patient_does_not_report_it_as_its_own_duplicate(client, db, chip_patient):
    """The exclude-self case. Without it, opening a chipped patient's form and
    pressing Save — changing nothing — reports the animal as a duplicate of
    itself and refuses, which is the shape this kind of check usually fails
    in."""
    chip = _chip("33")
    assert _edit(client, chip_patient, microchip=chip).status_code == 302
    resp = _edit(client, chip_patient, microchip=chip, age_note="second save")
    assert resp.status_code == 302, "re-saving a patient's own chip must be allowed"
    assert db.execute("SELECT age_note FROM patients WHERE id=?",
                      (chip_patient,)).fetchone()["age_note"] == "second save"


def test_a_duplicate_microchip_is_refused_and_leaves_nothing_behind(client, db, chip_patient):
    """One chip, one animal. This goes through the new-patient form rather
    than the edit form because that path writes an OWNER before it writes the
    patient — so a duplicate chip caught at the patient INSERT has to roll the
    owner back too, or every rejected attempt leaves an ownerless-pet-shaped
    orphan behind (ORPHANED_RECORDS_AUDIT.md F-03)."""
    chip = _chip("44")
    assert _edit(client, chip_patient, microchip=chip).status_code == 302

    owners_before = db.execute("SELECT count(*) AS c FROM owners").fetchone()["c"]
    patients_before = db.execute("SELECT count(*) AS c FROM patients").fetchone()["c"]

    resp = client.post("/visits/new/new-patient", data={
        "owner_name": f"Duplicate Chip Owner {uuid.uuid4().hex[:6]}", "owner_phone": _phone(),
        "animal_name": "Second Pet", "species": "Dog", "microchip": chip,
        "complaint": "checkup"}, follow_redirects=False)

    assert resp.status_code == 200, "should redisplay the form, not create the visit"
    assert db.execute("SELECT count(*) AS c FROM patients").fetchone()["c"] == patients_before, (
        "a second patient must not be created for a chip already on file")
    assert db.execute("SELECT count(*) AS c FROM owners").fetchone()["c"] == owners_before, (
        "the owner written before the patient must be rolled back with it")


def test_a_patient_is_found_by_microchip_however_it_is_typed(client, db, chip_patient):
    import logic
    chip = _chip("55")
    assert _edit(client, chip_patient, microchip=chip).status_code == 302

    def ids(term):
        return {r["id"] for r in logic.search_patients(db, term)}

    assert chip_patient in ids(chip), "searching the stored chip must find the patient"
    spaced = f"{chip[:3]} {chip[3:9]}-{chip[9:]}"
    assert chip_patient in ids(spaced), (
        "a chip typed the way it is printed must find the record it is on")
    # The control: the search is not simply returning everything.
    assert chip_patient not in ids(_chip("66")), "a different chip must not match"

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
