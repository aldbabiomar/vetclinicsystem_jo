"""
PDF export tests.

Every one of these routes hands a document to a client or a distributor —
a bill, a receipt, a patient file. A broken export is invisible until
someone tries to print one in front of the person waiting for it, and
"invisible until the worst moment" is exactly what a test is for.

The smoke suite already requests these routes, but only if a suitable row
happens to exist in the database at the time. These tests create the row
first, so the export is exercised against real content on every run rather
than by luck — and they check the response is actually a PDF, not an error
page served with a 200.
"""
import uuid
from datetime import date
from decimal import Decimal

import pytest

from conftest import needs_db


pytestmark = needs_db


def _uid(prefix):
    return f"{prefix}{uuid.uuid4().hex[:8].upper()}"


def _assert_is_a_real_pdf(resp, what):
    """A 200 is not enough. A route that catches its own error and renders a
    message still returns 200 with an HTML body — which would look like a
    passing test and print as a blank page."""
    assert resp.status_code == 200, f"{what}: HTTP {resp.status_code}"
    body = resp.get_data()
    assert body[:4] == b"%PDF", (
        f"{what}: response is not a PDF (starts {body[:40]!r})")
    assert len(body) > 800, f"{what}: PDF is suspiciously small ({len(body)} bytes)"
    assert b"%%EOF" in body[-1024:], f"{what}: PDF has no end marker — truncated"


@pytest.fixture
def billed_visit(client, db):
    """An owner -> patient -> visit with a real bill and a real payment, so
    the exported document has content rather than empty sections."""
    o_id, p_id, v_id = _uid("O"), _uid("P"), _uid("V")
    db.execute("INSERT INTO owners (id, name, phone) VALUES (?,?,?)",
               (o_id, f"Export Owner {o_id}", None))
    db.execute("INSERT INTO patients (id, owner_id, animal_name, species) VALUES (?,?,?,?)",
               (p_id, o_id, f"Export Pet {p_id}", "Dog"))
    db.execute("INSERT INTO visits (id, patient_id, date, case_status, complaint) VALUES (?,?,?,?,?)",
               (v_id, p_id, date.today().isoformat(), "Ongoing", "Export test complaint"))
    db.commit()
    client.post(f"/visits/{v_id}/billing",
                data={"billing_type": "Manual", "manual_amount": "100.000"}, follow_redirects=False)
    client.post(f"/visits/{v_id}/payment",
                data={"amount": "40.000", "method": "Cash"}, follow_redirects=False)
    yield {"owner_id": o_id, "patient_id": p_id, "visit_id": v_id}
    for sql, args in (
        ("DELETE FROM payments WHERE visit_id=?", (v_id,)),
        ("DELETE FROM visit_billing_lines WHERE visit_id=?", (v_id,)),
        ("DELETE FROM billing WHERE visit_id=?", (v_id,)),
        ("DELETE FROM visits WHERE id=?", (v_id,)),
        ("DELETE FROM patients WHERE id=?", (p_id,)),
        ("DELETE FROM owners WHERE id=?", (o_id,)),
    ):
        db.execute(sql, args)
    db.commit()


def test_visit_export_produces_a_pdf(client, billed_visit):
    resp = client.get(f"/visits/{billed_visit['visit_id']}/export")
    _assert_is_a_real_pdf(resp, "visit export")


def test_patient_file_export_produces_a_pdf(client, billed_visit):
    resp = client.get(f"/patients/{billed_visit['patient_id']}/export/file")
    _assert_is_a_real_pdf(resp, "patient file export")


def test_patient_billing_export_produces_a_pdf(client, billed_visit):
    """The one a client actually receives and pays against."""
    resp = client.get(f"/patients/{billed_visit['patient_id']}/export/billing")
    _assert_is_a_real_pdf(resp, "patient billing export")


def test_exports_do_not_error_for_a_record_that_does_not_exist(client):
    """A made-up id must give a message or a 404, never a server error."""
    for url in ("/visits/NOPE-NOT-A-VISIT/export",
                "/patients/NOPE-NOT-A-PATIENT/export/file",
                "/patients/NOPE-NOT-A-PATIENT/export/billing",
                "/pos/receipt/999999",
                "/pos/history/999999/export",
                "/boarding/999999/export",
                "/inpatient/999999/export"):
        resp = client.get(url)
        assert resp.status_code < 500, f"{url} -> HTTP {resp.status_code}"


def test_a_patient_with_no_visits_still_exports(client, db):
    """Empty-state export. A brand-new patient with nothing on file is the
    first thing a new clinic will print, and a report that assumes at least
    one visit breaks precisely there."""
    o_id, p_id = _uid("O"), _uid("P")
    db.execute("INSERT INTO owners (id, name) VALUES (?,?)", (o_id, f"Empty Owner {o_id}"))
    db.execute("INSERT INTO patients (id, owner_id, animal_name) VALUES (?,?,?)",
               (p_id, o_id, f"Empty Pet {p_id}"))
    db.commit()
    try:
        _assert_is_a_real_pdf(client.get(f"/patients/{p_id}/export/file"), "empty patient file")
        _assert_is_a_real_pdf(client.get(f"/patients/{p_id}/export/billing"), "empty patient billing")
    finally:
        db.execute("DELETE FROM patients WHERE id=?", (p_id,))
        db.execute("DELETE FROM owners WHERE id=?", (o_id,))
        db.commit()


@pytest.fixture
def completed_sale_for_receipt(client, db):
    """A sale made through the real checkout, so the receipt has lines."""
    inv_id, pl_id = _uid("INV"), _uid("PL")
    db.execute("INSERT INTO inventory_list (id, name, category, unit, track_expiry, cost_price, "
               "ownership_type, active) VALUES (?,?,?,?,?,?,?,?)",
               (inv_id, f"Receipt Item {inv_id}", "Retail", "unit", False, Decimal("2.000"), "Owned", True))
    db.execute("INSERT INTO price_list (id, name, category, cost_price, sale_price, active, "
               "linked_item_id, can_discount) VALUES (?,?,?,?,?,?,?,?)",
               (pl_id, f"Receipt Item {inv_id}", "Retail", Decimal("2.000"), Decimal("10.000"), True, inv_id, True))
    from datetime import datetime
    cur = db.execute("INSERT INTO audit_sessions (audit_date, performed_by, status, created_at, confirmed_at) "
                     "VALUES (?,?,?,?,?) RETURNING id",
                     (date.today().isoformat(), "U001", "Confirmed",
                      datetime.now().isoformat(timespec="seconds"),
                      datetime.now().isoformat(timespec="microseconds")))
    session_id = cur.fetchone()["id"]
    db.execute("INSERT INTO audit_session_lines (session_id, item_id, stock_counted, received_since_prior) "
               "VALUES (?,?,?,?)", (session_id, inv_id, 50.0, 0.0))
    db.commit()
    client.post("/pos/checkout", data={
        "item_id": inv_id, "quantity": "2",
        "payment_method": "Cash", "cash_received": "30.000"}, follow_redirects=False)
    sale = db.execute("SELECT * FROM sales ORDER BY id DESC LIMIT 1").fetchone()
    yield sale
    for sql, args in (
        ("DELETE FROM inventory_transactions WHERE item_id=?", (inv_id,)),
        ("DELETE FROM sale_items WHERE item_id=?", (inv_id,)),
        ("DELETE FROM audit_session_lines WHERE item_id=?", (inv_id,)),
        ("DELETE FROM audit_sessions WHERE id=?", (session_id,)),
        ("DELETE FROM price_list WHERE id=?", (pl_id,)),
        ("DELETE FROM inventory_list WHERE id=?", (inv_id,)),
    ):
        db.execute(sql, args)
    db.commit()


def test_pos_receipt_renders(client, completed_sale_for_receipt):
    """The receipt is an HTML page, not a PDF — it is what gets handed over
    at the counter, so it must at least render for a real sale."""
    resp = client.get(f"/pos/receipt/{completed_sale_for_receipt['id']}")
    assert resp.status_code == 200
    assert len(resp.get_data()) > 200


def test_pos_sale_export_produces_a_pdf(client, completed_sale_for_receipt):
    resp = client.get(f"/pos/history/{completed_sale_for_receipt['id']}/export")
    _assert_is_a_real_pdf(resp, "POS sale export")


def test_boarding_export_produces_a_pdf(client, db, billed_visit):
    cur = db.execute(
        "INSERT INTO boarding_sessions (patient_id, entry_date, special_needs, total_is_auto, "
        "cleanup_amount, discount_percent, dismissed, total) VALUES (?,?,?,?,?,?,?,?) RETURNING id",
        (billed_visit["patient_id"], date.today().isoformat(), False, False, Decimal(0), Decimal(0), False, Decimal("200.000")))
    bid = cur.fetchone()["id"]
    db.commit()
    try:
        _assert_is_a_real_pdf(client.get(f"/boarding/{bid}/export"), "boarding export")
    finally:
        db.execute("DELETE FROM payments WHERE boarding_id=?", (bid,))
        db.execute("DELETE FROM boarding_sessions WHERE id=?", (bid,))
        db.commit()
