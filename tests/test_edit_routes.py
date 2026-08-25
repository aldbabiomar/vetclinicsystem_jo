"""
Editing records that already carry money, and the operating-cost figures
the profit report is built from.

An edit is riskier than a create. A create that fails leaves nothing
behind; an edit that half-applies leaves a record that looks complete and
is wrong — a boarding stay whose daily rate changed but whose total did
not, a case marked dismissed with its bill still open.

Needs a throwaway Postgres; skips cleanly without one. See conftest.py.
"""
import uuid
from datetime import date, datetime, timedelta

import pytest

from decimal import Decimal as D

import logic
from conftest import needs_db


pytestmark = needs_db


def _uid(prefix):
    return f"{prefix}{uuid.uuid4().hex[:8].upper()}"


@pytest.fixture
def patient(db):
    o_id, p_id = _uid("O"), _uid("P")
    db.execute("INSERT INTO owners (id, name) VALUES (?,?)", (o_id, f"Edit Owner {o_id}"))
    db.execute("INSERT INTO patients (id, owner_id, animal_name) VALUES (?,?,?)",
               (p_id, o_id, f"Edit Pet {p_id}"))
    db.commit()
    yield {"owner_id": o_id, "patient_id": p_id}
    db.execute("DELETE FROM patients WHERE id=?", (p_id,))
    db.execute("DELETE FROM owners WHERE id=?", (o_id,))
    db.commit()


# ---------------------------------------------------------------------------
# Boarding stays
# ---------------------------------------------------------------------------

@pytest.fixture
def stay(db, patient):
    cur = db.execute(
        "INSERT INTO boarding_sessions (patient_id, entry_date, special_needs, total_is_auto, "
        "cleanup_amount, discount_percent, dismissed, total, price_per_day) "
        "VALUES (?,?,?,?,?,?,?,?,?) RETURNING id",
        (patient["patient_id"], date.today().isoformat(), False, True, D(0), D(0), False,
         D("200.000"), D("50.000")))
    bid = cur.fetchone()["id"]
    db.commit()
    yield {"id": bid, "patient_id": patient["patient_id"]}
    db.execute("DELETE FROM payments WHERE boarding_id=?", (bid,))
    db.execute("DELETE FROM boarding_sessions WHERE id=?", (bid,))
    db.commit()


def _stamp(db, table, row_id):
    row = db.execute(f"SELECT updated_at FROM {table} WHERE id=?", (row_id,)).fetchone()
    return "" if row is None or row["updated_at"] is None else str(row["updated_at"])


def _edit_stay(client, db, bid, **data):
    payload = {"entry_date": date.today().isoformat(), "dismissal_date": "",
               "price_per_day": "50.000", "total": "", "room": "R1",
               "special_needs": "", "special_needs_notes": "", "admitted_items": "",
               "expected_updated_at": _stamp(db, "boarding_sessions", bid)}
    payload.update(data)
    return client.post(f"/boarding/{bid}/edit", data=payload, follow_redirects=False)


def test_a_stay_can_be_edited(client, db, stay):
    _edit_stay(client, db, stay["id"], price_per_day="60.000")
    row = db.execute("SELECT * FROM boarding_sessions WHERE id=?", (stay["id"],)).fetchone()
    assert row["price_per_day"] == D("60.000")


def test_a_stay_rejects_a_negative_daily_rate(client, db, stay):
    _edit_stay(client, db, stay["id"], price_per_day="-60.000")
    row = db.execute("SELECT * FROM boarding_sessions WHERE id=?", (stay["id"],)).fetchone()
    assert row["price_per_day"] == D("50.000"), "a rejected edit must leave the rate alone"


def test_a_stay_rejects_a_dismissal_before_the_entry_date(client, db, stay):
    """A stay that ends before it began produces a negative night count, and
    the nightly total calculated from it goes the same way."""
    yesterday = (date.today() - timedelta(days=5)).isoformat()
    _edit_stay(client, db, stay["id"], dismissal_date=yesterday)
    row = db.execute("SELECT * FROM boarding_sessions WHERE id=?", (stay["id"],)).fetchone()
    assert row["dismissal_date"] is None or str(row["dismissal_date"]) >= str(row["entry_date"]), (
        "a stay must not end before it starts")


def test_a_stay_rejects_a_malformed_date(client, db, stay):
    original = db.execute("SELECT entry_date FROM boarding_sessions WHERE id=?",
                          (stay["id"],)).fetchone()["entry_date"]
    for bad in ("not-a-date", "2026-08-25garbage"):
        resp = _edit_stay(client, db, stay["id"], entry_date=bad)
        assert resp.status_code != 500, f"{bad!r} must not raise"
    row = db.execute("SELECT entry_date FROM boarding_sessions WHERE id=?", (stay["id"],)).fetchone()
    assert row["entry_date"] == original


def test_a_stale_stay_edit_is_refused(client, db, stay):
    """Same optimistic locking as visits: two people with the stay open, the
    second save must not silently erase the first."""
    stale = _stamp(db, "boarding_sessions", stay["id"])
    _edit_stay(client, db, stay["id"], price_per_day="70.000")
    assert db.execute("SELECT price_per_day FROM boarding_sessions WHERE id=?",
                      (stay["id"],)).fetchone()["price_per_day"] == D("70.000")
    resp = client.post(f"/boarding/{stay['id']}/edit", data={
        "entry_date": date.today().isoformat(), "dismissal_date": "",
        "price_per_day": "99.000", "total": "", "room": "R1",
        "expected_updated_at": stale}, follow_redirects=False)
    assert resp.status_code != 500
    after = db.execute("SELECT price_per_day FROM boarding_sessions WHERE id=?",
                       (stay["id"],)).fetchone()["price_per_day"]
    assert after == D("70.000"), "the stale save must not overwrite the first"


# ---------------------------------------------------------------------------
# Inpatient cases
# ---------------------------------------------------------------------------

@pytest.fixture
def case(db, patient):
    cur = db.execute(
        "INSERT INTO inpatient_cases (patient_id, admission_date, dismissed, discount_percent, "
        "total, cleanup_amount) VALUES (?,?,?,?,?,?) RETURNING id",
        (patient["patient_id"], date.today().isoformat(), False, D(0), D(0), D(0)))
    cid = cur.fetchone()["id"]
    db.commit()
    yield {"id": cid, "patient_id": patient["patient_id"]}
    for sql in ("DELETE FROM payments WHERE inpatient_case_id=?",
                "DELETE FROM refunds WHERE inpatient_case_id=?",
                "DELETE FROM attachments WHERE inpatient_case_id=?",
                "DELETE FROM inpatient_billing WHERE case_id=?",
                "DELETE FROM inpatient_updates WHERE case_id=?",
                "DELETE FROM inpatient_contact_log WHERE case_id=?",
                "DELETE FROM inpatient_cases WHERE id=?"):
        db.execute(sql, (cid,))
    db.commit()


def _edit_case(client, db, cid, **data):
    payload = {"admission_date": date.today().isoformat(), "dismissal_date": "",
               "weight_kg": "10", "bcs": "5", "complaint": "", "exam_findings": "",
               "admitted_items": "", "dismissed": "",
               "expected_updated_at": _stamp(db, "inpatient_cases", cid)}
    payload.update(data)
    return client.post(f"/inpatient/{cid}/edit", data=payload, follow_redirects=False)


def test_a_case_can_be_edited(client, db, case):
    _edit_case(client, db, case["id"], complaint="Updated complaint")
    row = db.execute("SELECT * FROM inpatient_cases WHERE id=?", (case["id"],)).fetchone()
    assert row["complaint"] == "Updated complaint"


def test_a_case_rejects_a_negative_weight(client, db, case):
    _edit_case(client, db, case["id"], weight_kg="-10")
    row = db.execute("SELECT weight_kg FROM inpatient_cases WHERE id=?", (case["id"],)).fetchone()
    assert row["weight_kg"] is None or row["weight_kg"] >= 0


def test_a_case_rejects_an_out_of_range_bcs(client, db, case):
    for bad in ("0", "10"):
        resp = _edit_case(client, db, case["id"], bcs=bad)
        assert resp.status_code != 500
    row = db.execute("SELECT bcs FROM inpatient_cases WHERE id=?", (case["id"],)).fetchone()
    assert row["bcs"] is None or 1 <= row["bcs"] <= 9


def test_a_case_rejects_a_dismissal_before_admission(client, db, case):
    before = (date.today() - timedelta(days=5)).isoformat()
    _edit_case(client, db, case["id"], dismissal_date=before)
    row = db.execute("SELECT * FROM inpatient_cases WHERE id=?", (case["id"],)).fetchone()
    assert row["dismissal_date"] is None or str(row["dismissal_date"]) >= str(row["admission_date"])


def test_an_inpatient_discount_is_applied_and_capped(client, db, case, priced_service_for_case):
    """The discount route is a separate entry point from the billing form,
    with the same cap and the same "not below what is paid" hazard."""
    client.post(f"/inpatient/{case['id']}/billing",
                data={"price_id": priced_service_for_case["id"],
                      f"qty_{priced_service_for_case['id']}": "2"}, follow_redirects=False)
    client.post(f"/inpatient/{case['id']}/discount",
                data={"discount_percent": "10"}, follow_redirects=False)
    row = db.execute("SELECT * FROM inpatient_cases WHERE id=?", (case["id"],)).fetchone()
    assert row["discount_percent"] == 10

    client.post(f"/inpatient/{case['id']}/discount",
                data={"discount_percent": "95"}, follow_redirects=False)
    after = db.execute("SELECT * FROM inpatient_cases WHERE id=?", (case["id"],)).fetchone()
    assert after["discount_percent"] == 10, "an over-cap discount must not replace a valid one"


@pytest.fixture
def priced_service_for_case(db):
    pl_id = _uid("PL")
    db.execute("INSERT INTO price_list (id, name, category, cost_price, sale_price, active, can_discount) "
               "VALUES (?,?,?,?,?,?,?)",
               (pl_id, f"Case Service {pl_id}", "Service", D("4.000"), D("12.000"), True, True))
    db.commit()
    yield {"id": pl_id}
    db.execute("DELETE FROM inpatient_billing WHERE price_id=?", (pl_id,))
    db.execute("DELETE FROM price_list WHERE id=?", (pl_id,))
    db.commit()


# ---------------------------------------------------------------------------
# Operating costs — the other half of every profit figure
# ---------------------------------------------------------------------------

@pytest.fixture
def opex_snapshot(db):
    month = date.today().strftime("%Y-%m")
    rows = db.execute("SELECT * FROM monthly_opex WHERE month=?", (month,)).fetchall()
    original = [dict(r) for r in rows]
    yield month
    db.execute("DELETE FROM monthly_opex WHERE month=?", (month,))
    for r in original:
        cols = ", ".join(r.keys())
        marks = ", ".join(["?"] * len(r))
        db.execute(f"INSERT INTO monthly_opex ({cols}) VALUES ({marks})", tuple(r.values()))
    db.commit()


def _opex(client, month, **data):
    payload = {"month": month, "rent": "1000.000", "salaries": "5000.000",
               "utilities": "500.000", "marketing": "200.000", "other": "0"}
    payload.update(data)
    return client.post("/reports/opex", data=payload, follow_redirects=False)


def test_operating_costs_can_be_recorded(client, db, opex_snapshot):
    _opex(client, opex_snapshot, rent="1200.000")
    row = db.execute("SELECT * FROM monthly_opex WHERE month=?", (opex_snapshot,)).fetchone()
    assert row is not None, "the month's costs were not saved"
    assert row["rent"] == D("1200.000")


def test_operating_costs_reject_a_negative_figure(client, db, opex_snapshot):
    """A negative cost reads as income in the profit calculation — it would
    inflate the clinic's apparent margin rather than reduce it."""
    _opex(client, opex_snapshot, rent="1000.000")
    resp = _opex(client, opex_snapshot, rent="-1000.000")
    assert resp.status_code != 500
    row = db.execute("SELECT * FROM monthly_opex WHERE month=?", (opex_snapshot,)).fetchone()
    if row is not None:
        assert row["rent"] >= 0, "a negative operating cost must not be stored"


def test_operating_costs_reject_a_non_numeric_figure(client, db, opex_snapshot):
    resp = _opex(client, opex_snapshot, salaries="loads")
    assert resp.status_code != 500
    row = db.execute("SELECT * FROM monthly_opex WHERE month=?", (opex_snapshot,)).fetchone()
    if row is not None:
        assert row["salaries"] != "loads"


# ---------------------------------------------------------------------------
# Distributors
# ---------------------------------------------------------------------------

def test_a_distributor_can_be_edited(client, db):
    name = f"EditDist {uuid.uuid4().hex[:6]}"
    client.post("/distributors/new", data={
        "name": name, "contact_person": "Before", "lead_time_days": "5"},
        follow_redirects=False)
    row = db.execute("SELECT * FROM distributors WHERE name=?", (name,)).fetchone()
    assert row is not None
    try:
        client.post(f"/distributors/{row['id']}/edit", data={
            "name": name, "contact_person": "After", "lead_time_days": "9",
            "phone": "", "email": "", "payment_terms": "", "notes": "", "catalog_link": ""},
            follow_redirects=False)
        after = db.execute("SELECT * FROM distributors WHERE id=?", (row["id"],)).fetchone()
        assert after["contact_person"] == "After"
        assert after["lead_time_days"] == 9
    finally:
        db.execute("DELETE FROM distributors WHERE id=?", (row["id"],))
        db.commit()


def test_a_distributor_rejects_a_negative_lead_time(client, db):
    """Lead time drives the reorder point. A negative one would make the
    ordering sheet ask for stock in the past."""
    name = f"EditDist {uuid.uuid4().hex[:6]}"
    client.post("/distributors/new", data={
        "name": name, "contact_person": "X", "lead_time_days": "5"}, follow_redirects=False)
    row = db.execute("SELECT * FROM distributors WHERE name=?", (name,)).fetchone()
    assert row is not None
    try:
        client.post(f"/distributors/{row['id']}/edit", data={
            "name": name, "contact_person": "X", "lead_time_days": "-5",
            "phone": "", "email": "", "payment_terms": "", "notes": "", "catalog_link": ""},
            follow_redirects=False)
        after = db.execute("SELECT * FROM distributors WHERE id=?", (row["id"],)).fetchone()
        assert after["lead_time_days"] is None or after["lead_time_days"] >= 0
    finally:
        db.execute("DELETE FROM distributors WHERE id=?", (row["id"],))
        db.commit()
