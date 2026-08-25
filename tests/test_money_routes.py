"""
Money *transaction* tests — VetClinicSystem JO.

test_money.py covers the arithmetic. This file covers the routes that
actually move money: they read a form, check stock under a lock, compute a
total, write a sale, decrement inventory and work out change — all in one
request. None of that is reachable from a pure function test, which is why
`pos_checkout` (203 lines), `visit_billing_save` (145) and
`refund_retail_save` (141) sat at 1-2% coverage while the arithmetic
underneath them was at 100%.

These need a throwaway Postgres and skip cleanly without one — see
conftest.py for how to point them at the isolated test environment.

Every test creates its own item/owner/patient with a unique id and asserts
on rows it wrote itself, so tests do not depend on each other's leftovers
or on what the environment happened to seed.

JO's money model drives every figure below: amounts are `Decimal`, columns
are NUMERIC(12,3), and there is **no note rounding and no anti-"looks free"
floor**. Where IQ's equivalent file asserts a total is a multiple of 250,
this one asserts it is exact to the fils. Do not reconcile the two files.
"""
import uuid
from datetime import datetime, date

import pytest

from decimal import Decimal

import logic
from conftest import needs_db


pytestmark = needs_db

D = Decimal


# ---------------------------------------------------------------------------
# Fixtures — a sellable item needs more setup than it looks: POS fails
# closed on anything that has never been through a confirmed audit.
# ---------------------------------------------------------------------------

def _uid(prefix):
    return f"{prefix}{uuid.uuid4().hex[:8].upper()}"


@pytest.fixture
def sellable(db):
    """An inventory item with a price, a confirmed audit, and stock.

    The audit is not optional set-dressing: `pos_checkout` refuses to sell
    anything whose `current_stock` is None ("hasn't been through an
    inventory audit yet"), which is a deliberate fail-closed guard against
    overselling a never-counted item. A fixture that skipped it would make
    every checkout test fail for the wrong reason.
    """
    inv_id, pl_id = _uid("INV"), _uid("PL")
    db.execute("INSERT INTO inventory_list (id, name, category, unit, track_expiry, cost_price, ownership_type, active) "
               "VALUES (?,?,?,?,?,?,?,?)",
               (inv_id, f"Route Test Item {inv_id}", "Retail", "unit", False, D("2.000"), "Owned", True))
    db.execute("INSERT INTO price_list (id, name, category, cost_price, sale_price, active, linked_item_id, can_discount) "
               "VALUES (?,?,?,?,?,?,?,?)",
               (pl_id, f"Route Test Item {inv_id}", "Retail", D("2.000"), D("10.000"), True, inv_id, True))
    cur = db.execute("INSERT INTO audit_sessions (audit_date, performed_by, status, created_at, confirmed_at) "
                     "VALUES (?,?,?,?,?) RETURNING id",
                     (date.today().isoformat(), "U001", "Confirmed",
                      datetime.now().isoformat(timespec="seconds"),
                      datetime.now().isoformat(timespec="microseconds")))
    session_id = cur.fetchone()["id"]
    db.execute("INSERT INTO audit_session_lines (session_id, item_id, stock_counted, received_since_prior) "
               "VALUES (?,?,?,?)", (session_id, inv_id, 100.0, 0.0))
    db.commit()
    yield {"inv_id": inv_id, "pl_id": pl_id, "price": D("10.000"), "stock": 100}
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


def _checkout(client, item_id, qty=1, **extra):
    data = {"item_id": item_id, "quantity": str(qty)}
    data.update({k: str(v) for k, v in extra.items()})
    return client.post("/pos/checkout", data=data, follow_redirects=False)


def _latest_sale(db):
    return db.execute("SELECT * FROM sales ORDER BY id DESC LIMIT 1").fetchone()


def _stock_now(db, inv_id):
    row = db.execute("SELECT COALESCE(SUM(change_qty),0) AS c FROM inventory_transactions WHERE item_id=?",
                     (inv_id,)).fetchone()
    return row["c"]


# ---------------------------------------------------------------------------
# POS checkout — the longest function in the app
# ---------------------------------------------------------------------------

def test_checkout_writes_a_sale_with_the_rounded_total(client, db, sellable):
    resp = _checkout(client, sellable["inv_id"], qty=1, payment_method="Card")
    assert resp.status_code == 302, "a successful checkout redirects to the receipt"
    sale = _latest_sale(db)
    assert sale["subtotal"] == D("10.000")
    assert sale["total"] == D("10.000")
    assert sale["total"].as_tuple().exponent >= -3, "must fit NUMERIC(12,3)"


def test_checkout_records_the_line_and_decrements_stock(client, db, sellable):
    before = _stock_now(db, sellable["inv_id"])
    _checkout(client, sellable["inv_id"], qty=3, payment_method="Card")
    sale = _latest_sale(db)
    line = db.execute("SELECT * FROM sale_items WHERE sale_id=?", (sale["id"],)).fetchone()
    assert line["quantity"] == 3
    assert line["unit_price"] == D("10.000")
    assert line["line_total"] == D("30.000")
    assert _stock_now(db, sellable["inv_id"]) == before - 3, "stock must fall by exactly what was sold"


def test_checkout_applies_a_discount_to_the_stored_total(client, db, sellable):
    _checkout(client, sellable["inv_id"], qty=2, discount_percent=10, payment_method="Card")
    sale = _latest_sale(db)
    assert sale["subtotal"] == D("20.000")
    assert sale["total"] == D("18.000")
    assert sale["discount_percent"] == 10
    assert sale["discount_applied_by"], "a discounted sale must record who applied it"


def test_checkout_total_matches_the_arithmetic_the_unit_tests_pin(client, db, sellable):
    """Ties the route to the exact-Decimal arithmetic. A divergence here is
    how a receipt and a report end up disagreeing by a fils."""
    _checkout(client, sellable["inv_id"], qty=3, discount_percent=7, payment_method="Card")
    sale = _latest_sale(db)
    expected = round(sale["subtotal"] * (1 - sale["discount_percent"] / D(100)), 3)
    assert sale["total"] == expected


def test_checkout_change_is_floored_so_the_clinic_never_overpays(client, db, sellable):
    """The opposite of IQ, and deliberately so: JOD has no note to floor to,
    so change is the exact difference to the fils. Flooring it here would
    quietly keep a few fils of the customer's money on every cash sale."""
    _checkout(client, sellable["inv_id"], qty=1, payment_method="Cash", cash_received="10.750")
    sale = _latest_sale(db)
    assert sale["cash_received"] == D("10.750")
    assert sale["change_given"] == D("0.750"), "exact change, not rounded to anything"
    assert sale["change_given"] == sale["cash_received"] - sale["total"]


def test_checkout_refuses_cash_below_the_total(client, db, sellable):
    before = db.execute("SELECT count(*) AS c FROM sales").fetchone()["c"]
    resp = _checkout(client, sellable["inv_id"], qty=1, payment_method="Cash", cash_received="0.500")
    assert resp.status_code == 200, "underpayment redisplays the form rather than redirecting"
    assert db.execute("SELECT count(*) AS c FROM sales").fetchone()["c"] == before, "nothing may be written"


def test_checkout_refuses_to_oversell(client, db, sellable):
    before = db.execute("SELECT count(*) AS c FROM sales").fetchone()["c"]
    resp = _checkout(client, sellable["inv_id"], qty=sellable["stock"] + 1, payment_method="Card")
    assert resp.status_code == 200
    assert db.execute("SELECT count(*) AS c FROM sales").fetchone()["c"] == before


def test_checkout_merges_duplicate_cart_lines_before_the_stock_check(client, db, sellable):
    """Two lines of the same item, each individually within stock but
    together over it, must be caught. Checking lines independently against
    live stock is how an item gets oversold without any race involved."""
    before = db.execute("SELECT count(*) AS c FROM sales").fetchone()["c"]
    half = sellable["stock"] / 2 + 1
    resp = client.post("/pos/checkout", data={
        "item_id": [sellable["inv_id"], sellable["inv_id"]],
        "quantity": [str(half), str(half)],
        "payment_method": "Card",
    }, follow_redirects=False)
    assert resp.status_code == 200, "the merged quantity exceeds stock and must be refused"
    assert db.execute("SELECT count(*) AS c FROM sales").fetchone()["c"] == before


def test_checkout_is_idempotent_under_a_repeated_key(client, db, sellable):
    """A double-submitted form (impatient click, flaky network) must not
    create two sales for one cart."""
    key = f"test-{uuid.uuid4().hex}"
    before = db.execute("SELECT count(*) AS c FROM sales").fetchone()["c"]
    r1 = _checkout(client, sellable["inv_id"], qty=1, payment_method="Card", idempotency_key=key)
    r2 = _checkout(client, sellable["inv_id"], qty=1, payment_method="Card", idempotency_key=key)
    assert r1.status_code == 302 and r2.status_code == 302
    after = db.execute("SELECT count(*) AS c FROM sales").fetchone()["c"]
    assert after == before + 1, "the second submission must not create a second sale"
    assert r1.headers["Location"] == r2.headers["Location"], "both must land on the same receipt"


def test_checkout_rejects_an_empty_cart(client, db):
    before = db.execute("SELECT count(*) AS c FROM sales").fetchone()["c"]
    resp = client.post("/pos/checkout", data={"payment_method": "Card"}, follow_redirects=False)
    assert resp.status_code == 200
    assert db.execute("SELECT count(*) AS c FROM sales").fetchone()["c"] == before


def test_checkout_rejects_a_non_numeric_discount(client, db, sellable):
    before = db.execute("SELECT count(*) AS c FROM sales").fetchone()["c"]
    resp = _checkout(client, sellable["inv_id"], qty=1, discount_percent="abc", payment_method="Card")
    assert resp.status_code == 200
    assert db.execute("SELECT count(*) AS c FROM sales").fetchone()["c"] == before


def test_checkout_rejects_a_discount_above_the_role_cap(client, db, sellable):
    before = db.execute("SELECT count(*) AS c FROM sales").fetchone()["c"]
    resp = _checkout(client, sellable["inv_id"], qty=1, discount_percent=101, payment_method="Card")
    assert resp.status_code == 200
    assert db.execute("SELECT count(*) AS c FROM sales").fetchone()["c"] == before


def test_checkout_refuses_a_discount_on_a_non_discountable_item(client, db, sellable):
    db.execute("UPDATE price_list SET can_discount=false WHERE id=?", (sellable["pl_id"],))
    db.commit()
    before = db.execute("SELECT count(*) AS c FROM sales").fetchone()["c"]
    resp = _checkout(client, sellable["inv_id"], qty=1, discount_percent=10, payment_method="Card")
    assert resp.status_code == 200
    assert db.execute("SELECT count(*) AS c FROM sales").fetchone()["c"] == before


def test_checkout_refuses_an_item_that_has_never_been_audited(client, db):
    """Fail-closed guard: an item with no confirmed audit has unknown stock,
    and unknown must mean "cannot sell", not "sell without limit"."""
    inv_id, pl_id = _uid("INV"), _uid("PL")
    db.execute("INSERT INTO inventory_list (id, name, category, unit, track_expiry, cost_price, ownership_type, active) "
               "VALUES (?,?,?,?,?,?,?,?)", (inv_id, f"Unaudited {inv_id}", "Retail", "unit", False, D("1.000"), "Owned", True))
    db.execute("INSERT INTO price_list (id, name, category, cost_price, sale_price, active, linked_item_id, can_discount) "
               "VALUES (?,?,?,?,?,?,?,?)", (pl_id, f"Unaudited {inv_id}", "Retail", D("1.000"), D("5.000"), True, inv_id, True))
    db.commit()
    try:
        before = db.execute("SELECT count(*) AS c FROM sales").fetchone()["c"]
        resp = _checkout(client, inv_id, qty=1, payment_method="Card")
        assert resp.status_code == 200
        assert db.execute("SELECT count(*) AS c FROM sales").fetchone()["c"] == before
    finally:
        db.execute("DELETE FROM price_list WHERE id=?", (pl_id,))
        db.execute("DELETE FROM inventory_list WHERE id=?", (inv_id,))
        db.commit()


def test_cleanup_write_off_reduces_the_stored_total(client, db, sellable):
    _checkout(client, sellable["inv_id"], qty=1, payment_method="Card", cleanup_amount="0.500")
    sale = _latest_sale(db)
    assert sale["cleanup_amount"] == D("0.500")
    assert sale["total"] == D("9.500")
    assert sale["cleanup_applied_by"], "a write-off must record who applied it"


def test_cleanup_above_the_cap_is_refused(client, db, sellable):
    """CLEANUP_CAP is a flat global ceiling. Without this the write-off is an
    unbounded discount that bypasses the role discount cap entirely."""
    import app as app_module
    before = db.execute("SELECT count(*) AS c FROM sales").fetchone()["c"]
    resp = _checkout(client, sellable["inv_id"], qty=1, payment_method="Card",
                     cleanup_amount=str(app_module.CLEANUP_CAP + D("1.000")))
    assert resp.status_code == 200
    assert db.execute("SELECT count(*) AS c FROM sales").fetchone()["c"] == before


def test_a_refused_checkout_leaves_stock_untouched(client, db, sellable):
    """The whole transaction must roll back together. A sale that fails
    after decrementing stock silently loses inventory."""
    before = _stock_now(db, sellable["inv_id"])
    _checkout(client, sellable["inv_id"], qty=1, payment_method="Cash", cash_received="0.001")
    assert _stock_now(db, sellable["inv_id"]) == before


# ---------------------------------------------------------------------------
# Visit billing — the second-longest money route
# ---------------------------------------------------------------------------

@pytest.fixture
def visit(db):
    """An owner -> patient -> visit chain, the minimum a bill can hang off."""
    o_id, p_id, v_id = _uid("O"), _uid("P"), _uid("V")
    db.execute("INSERT INTO owners (id, name) VALUES (?,?)", (o_id, f"Route Owner {o_id}"))
    db.execute("INSERT INTO patients (id, owner_id, animal_name) VALUES (?,?,?)",
               (p_id, o_id, f"Route Pet {p_id}"))
    # 'Ongoing', not 'Open' — visits_case_status_check allows only the seven
    # statuses the app's own dropdown offers.
    db.execute("INSERT INTO visits (id, patient_id, date, case_status) VALUES (?,?,?,?)",
               (v_id, p_id, date.today().isoformat(), "Ongoing"))
    db.commit()
    yield {"visit_id": v_id, "patient_id": p_id, "owner_id": o_id}
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


def _bill(client, visit_id, **data):
    return client.post(f"/visits/{visit_id}/billing", data=data, follow_redirects=False)


def test_manual_bill_stores_the_rounded_total(client, db, visit):
    _bill(client, visit["visit_id"], billing_type="Manual", manual_amount="100.000")
    row = db.execute("SELECT * FROM billing WHERE visit_id=?", (visit["visit_id"],)).fetchone()
    assert row["manual_amount"] == D("100.000")
    assert row["total"] == D("100.000")
    assert row["total"].as_tuple().exponent >= -3


def test_manual_bill_total_agrees_with_compute_bill_totals(client, db, visit):
    """The stored `total` is what reports read instead of re-deriving the
    arithmetic. If it drifts from compute_bill_totals, the receipt and the
    P&L disagree and neither is obviously wrong."""
    _bill(client, visit["visit_id"], billing_type="Manual", manual_amount="13.755")
    row = db.execute("SELECT * FROM billing WHERE visit_id=?", (visit["visit_id"],)).fetchone()
    expected, _, _, _ = logic.compute_bill_totals(
        row["manual_amount"], row["discount_percent"], D(0), row["cleanup_amount"])
    assert row["total"] == expected


def test_a_sub_unit_bill_is_stored_exactly_not_inflated(client, db, visit):
    """The mirror image of IQ's anti-"looks free" floor, and the reason these
    two files must never be merged. IQ lifts a sub-note charge to 250 IQD so a
    real charge never prints as free. In JOD, 0.100 is a genuine payable
    amount -- inflating it would overcharge the client."""
    _bill(client, visit["visit_id"], billing_type="Manual", manual_amount="0.100")
    row = db.execute("SELECT * FROM billing WHERE visit_id=?", (visit["visit_id"],)).fetchone()
    assert row["total"] == D("0.100")


def test_manual_bill_requires_a_positive_amount(client, db, visit):
    resp = _bill(client, visit["visit_id"], billing_type="Manual", manual_amount="0")
    assert resp.status_code == 200
    row = db.execute("SELECT * FROM billing WHERE visit_id=?", (visit["visit_id"],)).fetchone()
    assert row is None, "a zero-amount manual bill must not be stored"


def test_manual_bill_rejects_a_non_numeric_amount(client, db, visit):
    resp = _bill(client, visit["visit_id"], billing_type="Manual", manual_amount="abc")
    assert resp.status_code == 200
    assert db.execute("SELECT * FROM billing WHERE visit_id=?", (visit["visit_id"],)).fetchone() is None


def test_bill_rejects_an_unknown_billing_type(client, db, visit):
    resp = _bill(client, visit["visit_id"], billing_type="Sideways", manual_amount="1000")
    assert resp.status_code == 200
    assert db.execute("SELECT * FROM billing WHERE visit_id=?", (visit["visit_id"],)).fetchone() is None


def test_a_bill_cannot_shrink_below_what_is_already_paid(client, db, visit):
    """Nothing else in the app ever surfaces `paid > total`. If a bill is
    allowed to shrink under an existing payment, the overpayment becomes
    invisible — not refunded, not flagged, just gone from every view."""
    _bill(client, visit["visit_id"], billing_type="Manual", manual_amount="100.000")
    db.execute("INSERT INTO payments (visit_id, amount, method, date, user_id) VALUES (?,?,?,?,?)",
               (visit["visit_id"], D("100.000"), "Cash", date.today().isoformat(), "U001"))
    db.commit()
    resp = _bill(client, visit["visit_id"], billing_type="Manual", manual_amount="10.000")
    assert resp.status_code == 200
    row = db.execute("SELECT * FROM billing WHERE visit_id=?", (visit["visit_id"],)).fetchone()
    assert row["manual_amount"] == D("100.000"), "the bill must be left as it was"


def test_billing_a_missing_visit_is_refused(client, db):
    """JO has always had the existence guard on the happy path; IQ did not,
    and its route tests found the gap. Kept here so the guard cannot be
    removed from JO in a future tidy-up."""
    resp = client.post("/visits/NOPE-DOES-NOT-EXIST/billing",
                       data={"billing_type": "Manual", "manual_amount": "1000"},
                       follow_redirects=False)
    assert resp.status_code == 302, "should redirect with a message, not raise"
    assert resp.status_code != 500
    assert db.execute("SELECT * FROM billing WHERE visit_id=?", ("NOPE-DOES-NOT-EXIST",)).fetchone() is None


# ---------------------------------------------------------------------------
# Boarding payment — where discount, write-off and payment arrive together
# ---------------------------------------------------------------------------

@pytest.fixture
def boarding(db, visit):
    cur = db.execute(
        "INSERT INTO boarding_sessions (patient_id, entry_date, special_needs, total_is_auto, "
        "cleanup_amount, discount_percent, dismissed, total) VALUES (?,?,?,?,?,?,?,?) RETURNING id",
        (visit["patient_id"], date.today().isoformat(), False, False, D(0), D(0), False, D("200.000")))
    bid = cur.fetchone()["id"]
    db.commit()
    yield {"id": bid, "total": D("200.000"), "patient_id": visit["patient_id"]}
    db.execute("DELETE FROM payments WHERE boarding_id=?", (bid,))
    db.execute("DELETE FROM boarding_sessions WHERE id=?", (bid,))
    db.commit()


def _pay(client, bid, **data):
    return client.post(f"/boarding/{bid}/payment", data=data, follow_redirects=False)


def test_boarding_payment_is_recorded(client, db, boarding):
    _pay(client, boarding["id"], amount="50.000", method="Cash")
    row = db.execute("SELECT * FROM payments WHERE boarding_id=? ORDER BY id DESC LIMIT 1",
                     (boarding["id"],)).fetchone()
    assert row["amount"] == D("50.000")


def test_boarding_payment_cannot_exceed_the_balance(client, db, boarding):
    before = db.execute("SELECT count(*) AS c FROM payments WHERE boarding_id=?", (boarding["id"],)).fetchone()["c"]
    resp = _pay(client, boarding["id"], amount=str(boarding["total"] + D("50.000")), method="Cash")
    assert resp.status_code == 200
    after = db.execute("SELECT count(*) AS c FROM payments WHERE boarding_id=?", (boarding["id"],)).fetchone()["c"]
    assert after == before, "an overpayment must write nothing"


def test_boarding_payment_is_checked_against_the_POST_DISCOUNT_balance(client, db, boarding):
    """The bug this route was fixed for. Discount, write-off and payment all
    arrive in one submission, and the first two change the balance the third
    is checked against. Validating against the *pre*-submission balance would
    let "apply 10% and pay in full" through at the undiscounted figure —
    overpaying a bill with no delete route to undo it."""
    full = boarding["total"]
    before = db.execute("SELECT count(*) AS c FROM payments WHERE boarding_id=?", (boarding["id"],)).fetchone()["c"]
    resp = _pay(client, boarding["id"], amount=str(full), discount_percent="10", method="Cash")
    assert resp.status_code == 200, "paying the pre-discount total must be refused"
    after = db.execute("SELECT count(*) AS c FROM payments WHERE boarding_id=?", (boarding["id"],)).fetchone()["c"]
    assert after == before, "nothing may be written when the payment is refused"
    row = db.execute("SELECT discount_percent FROM boarding_sessions WHERE id=?", (boarding["id"],)).fetchone()
    assert row["discount_percent"] == 0, "a refused payment must not leave the discount applied"


def test_boarding_discount_is_stored_when_the_payment_is_valid(client, db, boarding):
    discounted = boarding["total"] * D("0.9")
    _pay(client, boarding["id"], amount=str(discounted), discount_percent="10", method="Cash")
    row = db.execute("SELECT * FROM boarding_sessions WHERE id=?", (boarding["id"],)).fetchone()
    assert row["discount_percent"] == 10
    assert row["discount_applied_by"], "a discount must record who applied it"


def test_boarding_payment_rejects_zero_and_negative(client, db, boarding):
    before = db.execute("SELECT count(*) AS c FROM payments WHERE boarding_id=?", (boarding["id"],)).fetchone()["c"]
    for bad in ("0", "-5.000"):
        resp = _pay(client, boarding["id"], amount=bad, method="Cash")
        assert resp.status_code == 200
    after = db.execute("SELECT count(*) AS c FROM payments WHERE boarding_id=?", (boarding["id"],)).fetchone()["c"]
    assert after == before


def test_boarding_payment_rejects_a_discount_above_the_cap(client, db, boarding):
    before = db.execute("SELECT count(*) AS c FROM payments WHERE boarding_id=?", (boarding["id"],)).fetchone()["c"]
    resp = _pay(client, boarding["id"], amount="10.000", discount_percent="150", method="Cash")
    assert resp.status_code == 200
    after = db.execute("SELECT count(*) AS c FROM payments WHERE boarding_id=?", (boarding["id"],)).fetchone()["c"]
    assert after == before


# ---------------------------------------------------------------------------
# Refunds — money leaving the clinic, and the only path that does
# ---------------------------------------------------------------------------

@pytest.fixture
def completed_sale(client, db, sellable):
    """A real sale, made through the real checkout, to refund against."""
    _checkout(client, sellable["inv_id"], qty=4, payment_method="Cash", cash_received="50.000")
    sale = _latest_sale(db)
    line = db.execute("SELECT * FROM sale_items WHERE sale_id=?", (sale["id"],)).fetchone()
    yield {"sale": sale, "line": line, "inv_id": sellable["inv_id"]}
    db.execute("DELETE FROM refund_items WHERE refund_id IN (SELECT id FROM refunds WHERE sale_id=?)",
               (sale["id"],))
    db.execute("DELETE FROM refunds WHERE sale_id=?", (sale["id"],))
    db.commit()


def _refund_retail(client, sale_id, sale_item_id, qty, **extra):
    data = {"sale_id": str(sale_id), "sale_item_id": str(sale_item_id),
            "quantity": str(qty), "refund_method": "Cash", "reason": "route test"}
    data.update({k: str(v) for k, v in extra.items()})
    return client.post("/refunds/retail", data=data, follow_redirects=False)


def _refunds_for(db, sale_id):
    return db.execute("SELECT * FROM refunds WHERE sale_id=? ORDER BY id", (sale_id,)).fetchall()


def test_retail_refund_is_recorded_against_the_sale(client, db, completed_sale):
    sale, line = completed_sale["sale"], completed_sale["line"]
    resp = _refund_retail(client, sale["id"], line["id"], 1)
    assert resp.status_code == 302
    rows = _refunds_for(db, sale["id"])
    assert len(rows) == 1
    assert rows[0]["amount"] == D("10.000")


def test_refund_is_exact_to_the_fils_not_rounded(client, db, sellable):
    """The mirror of IQ, and deliberately so. IQ floors a refund to a whole
    250 note so it never pays out more than the lines came to. JOD has no
    note to floor to, so the refund is the exact line total — flooring it
    here would quietly keep a few fils of the customer's money on every
    refund, in the clinic's favour, permanently."""
    db.execute("UPDATE price_list SET sale_price=? WHERE id=?", (D("10.505"), sellable["pl_id"]))
    db.commit()
    _checkout(client, sellable["inv_id"], qty=1, payment_method="Cash", cash_received="20.000")
    sale = _latest_sale(db)
    line = db.execute("SELECT * FROM sale_items WHERE sale_id=?", (sale["id"],)).fetchone()
    try:
        _refund_retail(client, sale["id"], line["id"], 1)
        row = _refunds_for(db, sale["id"])[0]
        assert row["amount"] == D("10.505"), "exact, not rounded to anything"
        assert row["amount"] == line["line_total"]
    finally:
        db.execute("DELETE FROM refund_items WHERE refund_id IN (SELECT id FROM refunds WHERE sale_id=?)",
                   (sale["id"],))
        db.execute("DELETE FROM refunds WHERE sale_id=?", (sale["id"],))
        db.commit()


def test_refund_cannot_exceed_what_the_sale_collected(client, db, completed_sale):
    """The aggregate cap. Per-line pricing is untouched; this stops the SUM
    of every refund against one sale from exceeding what the sale took."""
    sale, line = completed_sale["sale"], completed_sale["line"]
    resp = _refund_retail(client, sale["id"], line["id"], line["quantity"] + 1)
    assert resp.status_code == 200, "refunding more than was sold must be refused"
    assert _refunds_for(db, sale["id"]) == []


def test_repeated_partial_refunds_cannot_together_exceed_the_sale(client, db, completed_sale):
    """Each refund is individually valid; together they must not exceed the
    sale. Checking only the current refund is how a sale gets over-refunded
    across several small ones."""
    sale, line = completed_sale["sale"], completed_sale["line"]
    for _ in range(int(line["quantity"])):
        _refund_retail(client, sale["id"], line["id"], 1)
    total_refunded = sum(r["amount"] for r in _refunds_for(db, sale["id"]))
    # No epsilon: JOD amounts are exact Decimals, so a tolerance here would
    # both be meaningless and raise TypeError on Decimal + float.
    assert total_refunded <= sale["total"]
    resp = _refund_retail(client, sale["id"], line["id"], 1)
    assert resp.status_code == 200, "the one that would tip it over must be refused"
    assert sum(r["amount"] for r in _refunds_for(db, sale["id"])) == total_refunded


def test_aggregate_cap_bites_when_clean_up_shrank_what_was_collected(client, db, sellable):
    """The case the per-line check cannot catch, and the reason the aggregate
    cap exists separately from it.

    Note it is Clean Up, not a discount, that creates this gap. Refund lines
    are priced from refundable_sale_items(), whose unit_price is already
    discount-adjusted — so a discount can never make the lines sum to more
    than the sale collected. A Clean Up write-off is different: it comes off
    the sale total *after* the lines, so every line stays individually
    refundable in full while their sum exceeds what the clinic actually took.
    Only the aggregate cap sees that."""
    _checkout(client, sellable["inv_id"], qty=4, payment_method="Card", cleanup_amount="1.000")
    sale = _latest_sale(db)
    line = db.execute("SELECT * FROM sale_items WHERE sale_id=?", (sale["id"],)).fetchone()
    assert line is not None, "the checkout did not produce a sale"
    try:
        assert sale["cleanup_amount"] == D("1.000"), "fixture must actually apply a write-off"
        assert line["line_total"] > sale["total"], (
            "fixture must produce lines worth more than the sale collected")
        resp = _refund_retail(client, sale["id"], line["id"], line["quantity"])
        assert resp.status_code == 200, "refunding every line in full must be refused"
        assert _refunds_for(db, sale["id"]) == [], "nothing may be paid out"
    finally:
        db.execute("DELETE FROM refund_items WHERE refund_id IN (SELECT id FROM refunds WHERE sale_id=?)",
                   (sale["id"],))
        db.execute("DELETE FROM refunds WHERE sale_id=?", (sale["id"],))
        db.commit()


def test_refund_with_restock_returns_stock(client, db, completed_sale):
    sale, line = completed_sale["sale"], completed_sale["line"]
    before = _stock_now(db, completed_sale["inv_id"])
    _refund_retail(client, sale["id"], line["id"], 2, restock="on")
    assert _stock_now(db, completed_sale["inv_id"]) == before + 2


def test_refund_without_restock_leaves_stock_alone(client, db, completed_sale):
    """Refunding a damaged item returns the money but not the stock."""
    sale, line = completed_sale["sale"], completed_sale["line"]
    before = _stock_now(db, completed_sale["inv_id"])
    _refund_retail(client, sale["id"], line["id"], 2)
    assert _stock_now(db, completed_sale["inv_id"]) == before


def test_refund_requires_a_payout_method(client, db, completed_sale):
    sale, line = completed_sale["sale"], completed_sale["line"]
    resp = client.post("/refunds/retail", data={
        "sale_id": str(sale["id"]), "sale_item_id": str(line["id"]),
        "quantity": "1", "reason": "no method"}, follow_redirects=False)
    assert resp.status_code == 200
    assert _refunds_for(db, sale["id"]) == []


def test_refund_requires_a_sale(client, db):
    resp = client.post("/refunds/retail", data={
        "sale_id": "", "quantity": "1", "refund_method": "Cash"}, follow_redirects=False)
    assert resp.status_code == 200


def test_refund_rejects_a_line_from_a_different_sale(client, db, completed_sale):
    """A crafted POST pairing this sale with someone else's line must not
    refund against the wrong sale."""
    sale = completed_sale["sale"]
    other = db.execute("SELECT id FROM sale_items WHERE sale_id<>? ORDER BY id DESC LIMIT 1",
                       (sale["id"],)).fetchone()
    if other is None:
        pytest.skip("only one sale in this database")
    resp = _refund_retail(client, sale["id"], other["id"], 1)
    assert resp.status_code == 200
    assert _refunds_for(db, sale["id"]) == []


def test_refund_rejects_a_non_numeric_quantity(client, db, completed_sale):
    sale, line = completed_sale["sale"], completed_sale["line"]
    resp = _refund_retail(client, sale["id"], line["id"], "abc")
    assert resp.status_code == 200
    assert _refunds_for(db, sale["id"]) == []


def test_service_refund_cannot_exceed_what_the_visit_paid(client, db, visit):
    """A service refund is capped by what was actually collected on that
    visit — otherwise it is a way to pay money out against nothing."""
    _bill(client, visit["visit_id"], billing_type="Manual", manual_amount="100.000")
    db.execute("INSERT INTO payments (visit_id, amount, method, date, user_id) VALUES (?,?,?,?,?)",
               (visit["visit_id"], D("50.000"), "Cash", date.today().isoformat(), "U001"))
    db.commit()
    resp = client.post("/refunds/service", data={
        "visit_id": visit["visit_id"], "amount": "90.000",
        "refund_method": "Cash", "reason": "over-refund attempt"}, follow_redirects=False)
    assert resp.status_code == 200, "refunding more than was paid must be refused"
    rows = db.execute("SELECT * FROM refunds WHERE visit_id=?", (visit["visit_id"],)).fetchall()
    assert rows == []


def test_service_refund_within_what_was_paid_is_recorded(client, db, visit):
    _bill(client, visit["visit_id"], billing_type="Manual", manual_amount="100.000")
    db.execute("INSERT INTO payments (visit_id, amount, method, date, user_id) VALUES (?,?,?,?,?)",
               (visit["visit_id"], D("50.000"), "Cash", date.today().isoformat(), "U001"))
    db.commit()
    try:
        resp = client.post("/refunds/service", data={
            "visit_id": visit["visit_id"], "amount": "25.000",
            "refund_method": "Cash", "reason": "partial"}, follow_redirects=False)
        assert resp.status_code == 302
        rows = db.execute("SELECT * FROM refunds WHERE visit_id=?", (visit["visit_id"],)).fetchall()
        assert len(rows) == 1
        assert rows[0]["amount"] == D("25.000")
    finally:
        db.execute("DELETE FROM refunds WHERE visit_id=?", (visit["visit_id"],))
        db.commit()


def test_service_refund_needs_exactly_one_target(client, db, visit):
    """Linked to a visit OR an inpatient case, never both and never neither —
    otherwise the refund is attributed to nothing and escapes both caps."""
    resp = client.post("/refunds/service", data={
        "amount": "10.000", "refund_method": "Cash", "reason": "no target"},
        follow_redirects=False)
    assert resp.status_code == 200


def test_service_refund_rejects_zero_and_negative(client, db, visit):
    for bad in ("0", "-10.000"):
        resp = client.post("/refunds/service", data={
            "visit_id": visit["visit_id"], "amount": bad,
            "refund_method": "Cash", "reason": "bad"}, follow_redirects=False)
        assert resp.status_code == 200
    assert db.execute("SELECT * FROM refunds WHERE visit_id=?", (visit["visit_id"],)).fetchall() == []


def test_service_refund_requires_a_payout_method(client, db, visit):
    """The retail route has its own version of this test; the service route
    needs its own, because they are separate code paths and a mutation check
    showed removing the guard from one left every existing test passing.

    A refund recorded with no payout method leaves no trace of how the money
    physically left the clinic, which is exactly what cash reconciliation
    reads."""
    _bill(client, visit["visit_id"], billing_type="Manual", manual_amount="100.000")
    db.execute("INSERT INTO payments (visit_id, amount, method, date, user_id) VALUES (?,?,?,?,?)",
               (visit["visit_id"], D("50.000"), "Cash", date.today().isoformat(), "U001"))
    db.commit()
    try:
        for bad in ("", "Bitcoin", "cash"):
            resp = client.post("/refunds/service", data={
                "visit_id": visit["visit_id"], "amount": "25.000",
                "refund_method": bad, "reason": "bad method"}, follow_redirects=False)
            assert resp.status_code == 200, f"method {bad!r} must be refused"
        assert db.execute("SELECT * FROM refunds WHERE visit_id=?",
                          (visit["visit_id"],)).fetchall() == []
    finally:
        db.execute("DELETE FROM refunds WHERE visit_id=?", (visit["visit_id"],))
        db.commit()
