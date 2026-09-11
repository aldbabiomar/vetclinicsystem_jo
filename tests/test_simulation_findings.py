"""
Regression tests for SIMULATION_AUDIT_2026-09-11.md — VetClinicSystem JO.

**This is NOT a copy of IQ's file of the same name, and must never become
one.** Of the six findings, two (F1, F3) were IQ-only: they are consequences
of 250-IQD note rounding, which does not exist here. JO's assertions for
those paths are the OPPOSITE — that a small sale and a small refund stay
exact to the fils, with no floor and no rounding. COMPARISON.md §1.1.

What JO shares with IQ, and why each still needed its own fix:

  F2/F5  _save_audit_lines parsed counts with float(), which accepts "nan".
         The consequence diverges: in IQ `qty > nan` is False and the POS
         oversell guard silently passes; here `qty` is a Decimal, so the
         same comparison raises decimal.InvalidOperation and 500s the
         checkout — the till stops working. One root cause, two symptoms.
         The fix here uses parse_quantity(), the same parser the POS cart
         uses, so both sides of the comparison share one ceiling.
  F4     "?day=" reached Postgres as a date and 500'd. Identical to IQ.
  F6     an inpatient case accepted a discharge dated before its admission,
         a rule boarding_edit() has always had. Identical to IQ.
  Obs2   a saved cash count with a discrepancy was flashed as an error.

Every guard is paired with a control asserting the valid case still works
(CLAUDE.md §7.3); each was also verified by reverting the fix and watching
the test fail — see scripts/simulation/prove_guards.py.
"""
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal as D

import pytest

import logic

from conftest import needs_db


def _uid(prefix):
    return f"{prefix}{uuid.uuid4().hex[:8].upper()}"


@pytest.fixture
def sellable(db):
    """An inventory item with a price, a confirmed audit, and stock.

    A local copy rather than an import from test_money_routes: a fixture
    defined in a test module is not visible outside it. The confirmed audit
    is not set-dressing — pos_checkout fails closed on an item whose
    current_stock is None.
    """
    inv_id, pl_id = _uid("INV"), _uid("PL")
    db.execute("INSERT INTO inventory_list (id, name, category, unit, track_expiry, cost_price, "
               "ownership_type, active) VALUES (?,?,?,?,?,?,?,?)",
               (inv_id, f"Sim Test Item {inv_id}", "Retail", "unit", False, D("2.000"),
                "Owned", True))
    db.execute("INSERT INTO price_list (id, name, category, cost_price, sale_price, active, "
               "linked_item_id, can_discount) VALUES (?,?,?,?,?,?,?,?)",
               (pl_id, f"Sim Test Item {inv_id}", "Retail", D("2.000"), D("10.000"), True,
                inv_id, True))
    cur = db.execute("INSERT INTO audit_sessions (audit_date, performed_by, status, created_at, "
                     "confirmed_at) VALUES (?,?,?,?,?) RETURNING id",
                     (date.today().isoformat(), "U001", "Confirmed",
                      datetime.now().isoformat(timespec="seconds"),
                      datetime.now().isoformat(timespec="microseconds")))
    session_id = cur.fetchone()["id"]
    db.execute("INSERT INTO audit_session_lines (session_id, item_id, stock_counted, "
               "received_since_prior) VALUES (?,?,?,?)", (session_id, inv_id, 1000.0, 0.0))
    db.commit()
    yield {"inv_id": inv_id, "pl_id": pl_id, "price": D("10.000"), "stock": 1000.0,
           "session_id": session_id}
    for sql, args in (
        ("DELETE FROM refund_items WHERE sale_item_id IN "
         "(SELECT id FROM sale_items WHERE item_id=?)", (inv_id,)),
        ("DELETE FROM refunds WHERE sale_id IN "
         "(SELECT sale_id FROM sale_items WHERE item_id=?)", (inv_id,)),
        ("DELETE FROM inventory_transactions WHERE item_id=?", (inv_id,)),
        ("DELETE FROM sale_items WHERE item_id=?", (inv_id,)),
        ("DELETE FROM audit_session_lines WHERE item_id=?", (inv_id,)),
        ("DELETE FROM audit_sessions WHERE id=?", (session_id,)),
        ("DELETE FROM price_list WHERE id=?", (pl_id,)),
        ("DELETE FROM inventory_list WHERE id=?", (inv_id,)),
    ):
        try:
            db.execute(sql, args)
        except Exception:
            db.rollback()
    db.commit()


# ===========================================================================
# F1 / F3, JO side — the OPPOSITE assertion to IQ's
# ===========================================================================

@needs_db
def test_a_small_pos_sale_stays_exact_and_is_never_floored(client, db, sellable):
    """IQ lifts a sub-note cart to 250 IQD because a 100 IQD charge cannot be
    paid with real notes. The JOD has a fils subunit in everyday use, so
    0.100 is a real payable amount and must be stored exactly as typed.
    If IQ's floor were ever ported across, this total would come back as
    250.000 JOD."""
    db.execute("UPDATE price_list SET sale_price=? WHERE id=?", (D("0.100"), sellable["pl_id"]))
    db.commit()
    resp = client.post("/pos/checkout", data={
        "item_id": sellable["inv_id"], "quantity": "1", "payment_method": "Cash",
        "discount_percent": "0", "cash_received": "50.000",
        "idempotency_key": uuid.uuid4().hex}, follow_redirects=True)
    assert resp.status_code == 200
    row = db.execute("SELECT subtotal, total, change_given FROM sales "
                     "ORDER BY id DESC LIMIT 1").fetchone()
    assert row["subtotal"] == D("0.100")
    assert row["total"] == D("0.100"), "JO must not floor a small sale — no note rounding here"
    assert row["change_given"] == D("49.900"), "change is exact, not floored to a note"


@needs_db
def test_a_small_refund_stays_exact(client, db, sellable):
    """IQ rounds refunds down to a note and needed a guard against reaching
    zero. Here the refund is simply exact, and a sub-note value is normal."""
    db.execute("UPDATE price_list SET sale_price=? WHERE id=?", (D("0.240"), sellable["pl_id"]))
    db.commit()
    client.post("/pos/checkout", data={
        "item_id": sellable["inv_id"], "quantity": "1", "payment_method": "Cash",
        "discount_percent": "0", "cash_received": "50.000",
        "idempotency_key": uuid.uuid4().hex}, follow_redirects=True)
    sale = db.execute("SELECT id, total FROM sales ORDER BY id DESC LIMIT 1").fetchone()
    assert sale["total"] == D("0.240")
    line = db.execute("SELECT id FROM sale_items WHERE sale_id=? LIMIT 1",
                      (sale["id"],)).fetchone()
    client.post("/refunds/retail", data={
        "sale_id": str(sale["id"]), "sale_item_id": str(line["id"]), "quantity": "1",
        "reason": "Returned unopened", "refund_date": date.today().isoformat(),
        "refund_method": "Cash", "restock": "on"}, follow_redirects=True)
    ref = db.execute("SELECT amount FROM refunds WHERE sale_id=? ORDER BY id DESC LIMIT 1",
                     (sale["id"],)).fetchone()
    assert ref is not None and ref["amount"] == D("0.240"), (
        "a JOD refund is exact — neither rounded to zero nor lifted to a note")


def test_this_app_has_no_denomination_rounding_at_all():
    """The anti-port guard. IQ grew money.payable_total() for its floor; if
    that module or its helpers ever appear here, the currency model has been
    broken in a way the arithmetic tests above would only catch by accident."""
    with pytest.raises(ImportError):
        import money  # noqa: F401


# ===========================================================================
# F2 / F5 — counts are physical quantities: finite and non-negative
# ===========================================================================

@needs_db
@pytest.mark.parametrize("bad", ["nan", "NaN", "inf", "-inf", "Infinity", "-5", "1e400"])
def test_audit_counts_reject_non_finite_and_negative(client, db, sellable, bad):
    """Saved into a DRAFT session, so a refusal means the VALUE was refused —
    a confirmed session refuses every save regardless, which would make this
    pass whether or not the guard exists."""
    cur = db.execute("INSERT INTO audit_sessions (audit_date, performed_by, status, created_at) "
                     "VALUES (?,?,?,?) RETURNING id",
                     (date.today().isoformat(), "U001", "Draft",
                      datetime.now().isoformat(timespec="seconds")))
    sid = cur.fetchone()["id"]
    db.commit()
    try:
        client.post(f"/audit-history/session/{sid}/save",
                    data={f"stock_{sellable['inv_id']}": "5",
                          f"received_{sellable['inv_id']}": "0"}, follow_redirects=True)
        seeded = db.execute("SELECT stock_counted FROM audit_session_lines "
                            "WHERE session_id=? AND item_id=?",
                            (sid, sellable["inv_id"])).fetchone()
        assert seeded and seeded["stock_counted"] == 5.0, "control value did not save"

        client.post(f"/audit-history/session/{sid}/save",
                    data={f"stock_{sellable['inv_id']}": bad,
                          f"received_{sellable['inv_id']}": "0"}, follow_redirects=True)
        after = db.execute("SELECT stock_counted FROM audit_session_lines "
                           "WHERE session_id=? AND item_id=?",
                           (sid, sellable["inv_id"])).fetchone()
        assert after["stock_counted"] == 5.0, f"{bad!r} was accepted into the count"
    finally:
        db.execute("DELETE FROM audit_session_lines WHERE session_id=?", (sid,))
        db.execute("DELETE FROM audit_sessions WHERE id=?", (sid,))
        db.commit()


@needs_db
def test_a_valid_audit_count_still_saves(client, db, sellable):
    """CONTROL — the draft is not simply locked; good values go in."""
    cur = db.execute("INSERT INTO audit_sessions (audit_date, performed_by, status, created_at) "
                     "VALUES (?,?,?,?) RETURNING id",
                     (date.today().isoformat(), "U001", "Draft",
                      datetime.now().isoformat(timespec="seconds")))
    sid = cur.fetchone()["id"]
    db.commit()
    try:
        client.post(f"/audit-history/session/{sid}/save",
                    data={f"stock_{sellable['inv_id']}": "42.5",
                          f"received_{sellable['inv_id']}": "3"}, follow_redirects=True)
        row = db.execute("SELECT stock_counted, received_since_prior FROM audit_session_lines "
                         "WHERE session_id=? AND item_id=?",
                         (sid, sellable["inv_id"])).fetchone()
        assert row["stock_counted"] == 42.5
        assert row["received_since_prior"] == 3.0
    finally:
        db.execute("DELETE FROM audit_session_lines WHERE session_id=?", (sid,))
        db.execute("DELETE FROM audit_sessions WHERE id=?", (sid,))
        db.commit()


@needs_db
def test_the_database_itself_refuses_a_nan_count(db, sellable):
    """Defence in depth. A plain `>= 0` CHECK would NOT catch this: in
    Postgres NaN sorts above every value, so 'NaN' >= 0 is true."""
    cur = db.execute("INSERT INTO audit_sessions (audit_date, performed_by, status, created_at) "
                     "VALUES (?,?,?,?) RETURNING id",
                     (date.today().isoformat(), "U001", "Draft",
                      datetime.now().isoformat(timespec="seconds")))
    sid = cur.fetchone()["id"]
    db.commit()
    try:
        for bad in ("NaN", "Infinity", "-1"):
            with pytest.raises(Exception):
                db.execute("INSERT INTO audit_session_lines (session_id, item_id, stock_counted, "
                           "received_since_prior) VALUES (?,?,?::float8,?)",
                           (sid, sellable["inv_id"], bad, 0.0))
                db.commit()
            db.rollback()
        db.execute("INSERT INTO audit_session_lines (session_id, item_id, stock_counted, "
                   "received_since_prior) VALUES (?,?,?,?)",
                   (sid, sellable["inv_id"], 9.0, 0.0))
        db.commit()
        row = db.execute("SELECT stock_counted FROM audit_session_lines WHERE session_id=?",
                         (sid,)).fetchone()
        assert row["stock_counted"] == 9.0
    finally:
        db.rollback()
        db.execute("DELETE FROM audit_session_lines WHERE session_id=?", (sid,))
        db.execute("DELETE FROM audit_sessions WHERE id=?", (sid,))
        db.commit()


@needs_db
def test_a_nan_count_already_stored_does_not_500_the_till(client, db, sellable):
    """JO's distinctive symptom. `qty` here is a Decimal, so `qty > nan` does
    not quietly return False the way it does in IQ — it raises
    decimal.InvalidOperation and takes the whole checkout down with a 500.
    The guard must catch the NaN *before* that comparison. The CHECK
    constraint is dropped so this reaches the app-level guard it names
    (CLAUDE.md §7.4)."""
    db.execute("ALTER TABLE audit_session_lines "
               "DROP CONSTRAINT IF EXISTS audit_session_lines_stock_counted_check")
    db.commit()
    try:
        db.execute("UPDATE audit_session_lines SET stock_counted='NaN'::float8 WHERE item_id=?",
                   (sellable["inv_id"],))
        db.commit()
        planted = db.execute("SELECT stock_counted FROM audit_session_lines WHERE item_id=?",
                             (sellable["inv_id"],)).fetchone()
        assert str(planted["stock_counted"]).lower() == "nan", "could not plant the bad row"

        before = db.execute("SELECT COUNT(*) c FROM sales").fetchone()["c"]
        resp = client.post("/pos/checkout", data={
            "item_id": sellable["inv_id"], "quantity": "500", "payment_method": "Cash",
            "discount_percent": "0", "cash_received": "5000.000",
            "idempotency_key": uuid.uuid4().hex}, follow_redirects=True)
        assert resp.status_code == 200, "the till 500'd instead of refusing cleanly"
        after = db.execute("SELECT COUNT(*) c FROM sales").fetchone()["c"]
        assert after == before, "a sale was recorded against a NaN stock count"
        assert b"audit" in resp.data.lower()
    finally:
        db.execute("UPDATE audit_session_lines SET stock_counted=? WHERE item_id=?",
                   (sellable["stock"], sellable["inv_id"]))
        db.execute("ALTER TABLE audit_session_lines ADD CONSTRAINT "
                   "audit_session_lines_stock_counted_check CHECK (stock_counted IS NULL "
                   "OR (stock_counted >= 0 AND stock_counted < 'Infinity'::float8))")
        db.commit()


# ===========================================================================
# F4 — an empty date filter is not a server error
# ===========================================================================

@needs_db
@pytest.mark.parametrize("query", [
    "?day=", "?week=", "?day=&week=", "?day=%20", "?day=&show_past=1",
])
def test_appointments_survives_an_empty_date_filter(client, query):
    """parse_date("") RETURNS None rather than raising, so the route's
    except-ValueError guard never fired and "" reached Postgres as a date."""
    resp = client.get("/appointments" + query)
    assert resp.status_code < 500, f"/appointments{query} returned {resp.status_code}"


@needs_db
@pytest.mark.parametrize("query", ["", "?day=abc", "?day=2026-13-45", "?week=nonsense"])
def test_appointments_still_handles_absent_and_invalid_dates(client, query):
    """CONTROL — the previously-working cases must keep working."""
    resp = client.get("/appointments" + query)
    assert resp.status_code < 500


# ===========================================================================
# F6 — a stay cannot end before it began
# ===========================================================================

@needs_db
def test_an_inpatient_case_cannot_be_discharged_before_admission(client, db):
    """boarding_edit() has always enforced this; inpatient_edit() did not."""
    o_id, p_id = _uid("O"), _uid("P")
    db.execute("INSERT INTO owners (id, name) VALUES (?,?)", (o_id, f"Date Owner {o_id}"))
    db.execute("INSERT INTO patients (id, owner_id, animal_name) VALUES (?,?,?)",
               (p_id, o_id, f"Date Pet {p_id}"))
    admitted = date.today().isoformat()
    cur = db.execute("INSERT INTO inpatient_cases (patient_id, admission_date, complaint, "
                     "dismissed, updated_at) VALUES (?,?,?,?,?) RETURNING id",
                     (p_id, admitted, "obs", False,
                      datetime.now().isoformat(timespec="seconds")))
    case_id = cur.fetchone()["id"]
    db.commit()
    try:
        def edit(dismissal_date):
            stamp = db.execute("SELECT updated_at FROM inpatient_cases WHERE id=?",
                               (case_id,)).fetchone()["updated_at"]
            return client.post(f"/inpatient/{case_id}/edit", data={
                "complaint": "obs", "exam_findings": "stable", "weight_kg": "10", "bcs": "5",
                "dismissed": "on", "dismissal_date": dismissal_date,
                "expected_updated_at": stamp}, follow_redirects=True)

        edit((date.today() - timedelta(days=400)).isoformat())
        row = db.execute("SELECT admission_date, dismissal_date FROM inpatient_cases WHERE id=?",
                         (case_id,)).fetchone()
        assert (row["dismissal_date"] is None
                or str(row["dismissal_date"]) >= str(row["admission_date"])), (
            f"stored a negative-length stay: admitted {row['admission_date']}, "
            f"discharged {row['dismissal_date']}")

        edit(admitted)
        row = db.execute("SELECT dismissal_date FROM inpatient_cases WHERE id=?",
                         (case_id,)).fetchone()
        assert str(row["dismissal_date"]) == admitted, "a valid discharge was blocked too"
    finally:
        db.execute("DELETE FROM inpatient_cases WHERE id=?", (case_id,))
        db.execute("DELETE FROM patients WHERE id=?", (p_id,))
        db.execute("DELETE FROM owners WHERE id=?", (o_id,))
        db.commit()


# ===========================================================================
# Observation 2 — a saved cash count with a discrepancy is not an "error"
# ===========================================================================

@needs_db
def test_a_cash_discrepancy_is_flashed_as_a_warning_not_an_error(client, db):
    """The audit saved. Flashing it in the same red as a failure reads as
    "that did not work" and invites staff to run the count again."""
    day = date.today().isoformat()
    resp = client.post("/cash-register/audit",
                       data={"day": day, "counted_cash": "7777.000", "notes": "regression"},
                       follow_redirects=True)
    assert resp.status_code == 200
    assert 'class="flash warning"' in resp.data.decode()
    row = db.execute("SELECT status FROM cash_register_audits ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] in ("Surplus", "Deficit"), "the audit was not actually recorded"


@needs_db
def test_a_perfect_cash_count_is_still_a_success(client, db):
    """CONTROL — the success path must not have been turned into a warning."""
    day = date.today().isoformat()
    totals = logic.cash_register_totals(db, day)
    resp = client.post("/cash-register/audit",
                       data={"day": day, "counted_cash": str(totals["Cash"]), "notes": "control"},
                       follow_redirects=True)
    assert 'class="flash success"' in resp.data.decode()
    row = db.execute("SELECT status FROM cash_register_audits ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "Perfect"
