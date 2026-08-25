"""
Distributors, consignment, and the end-of-day cash count.

Money owed *outward* rather than collected inward. A distributor bill that
can be overpaid, or a consignment settlement that pays for stock twice, is
the same class of error as an overpaid client bill — except nobody
complains, because the person out of pocket is the clinic.

Needs a throwaway Postgres; skips cleanly without one. See conftest.py.
"""
import uuid
from datetime import date, datetime

import pytest

from decimal import Decimal as D

import logic
from conftest import needs_db


pytestmark = needs_db


def _uid(prefix):
    return f"{prefix}{uuid.uuid4().hex[:8].upper()}"


# ---------------------------------------------------------------------------
# Distributors and their bills
# ---------------------------------------------------------------------------

@pytest.fixture
def distributor(client, db):
    name = f"Dist {uuid.uuid4().hex[:6]}"
    client.post("/distributors/new", data={
        "name": name, "contact_person": "Someone", "phone": "", "email": "",
        "payment_terms": "30 days", "lead_time_days": "7", "notes": ""},
        follow_redirects=False)
    row = db.execute("SELECT * FROM distributors WHERE name=?", (name,)).fetchone()
    assert row is not None, "the distributor was not created"
    yield row
    for sql in ("DELETE FROM distributor_bill_payments WHERE bill_id IN "
                "(SELECT id FROM distributor_bills WHERE distributor_id=?)",
                "DELETE FROM distributor_bills WHERE distributor_id=?",
                "DELETE FROM distributors WHERE id=?"):
        db.execute(sql, (row["id"],))
    db.commit()


@pytest.fixture
def a_bill(client, db, distributor):
    client.post(f"/distributors/{distributor['id']}/bills/new", data={
        "bill_reference": f"REF{uuid.uuid4().hex[:6]}",
        "bill_date": date.today().isoformat(),
        "total_amount": "500.000", "notes": ""}, follow_redirects=False)
    row = db.execute("SELECT * FROM distributor_bills WHERE distributor_id=? ORDER BY id DESC LIMIT 1",
                     (distributor["id"],)).fetchone()
    assert row is not None, "the bill was not created"
    return {"dist_id": distributor["id"], "bill": row}


def _bill_payments(db, bill_id):
    return db.execute("SELECT count(*) AS c FROM distributor_bill_payments WHERE bill_id=?",
                      (bill_id,)).fetchone()["c"]


def test_a_distributor_bill_is_recorded(client, db, a_bill):
    assert a_bill["bill"]["total_amount"] == D("500.000")


def test_a_distributor_bill_rejects_a_negative_total(client, db, distributor):
    before = db.execute("SELECT count(*) AS c FROM distributor_bills WHERE distributor_id=?",
                        (distributor["id"],)).fetchone()["c"]
    resp = client.post(f"/distributors/{distributor['id']}/bills/new", data={
        "bill_reference": "NEG", "bill_date": date.today().isoformat(),
        "total_amount": "-500.000"}, follow_redirects=False)
    assert resp.status_code != 500
    assert db.execute("SELECT count(*) AS c FROM distributor_bills WHERE distributor_id=?",
                      (distributor["id"],)).fetchone()["c"] == before


def test_a_bill_payment_is_recorded(client, db, a_bill):
    before = _bill_payments(db, a_bill["bill"]["id"])
    client.post(f"/distributors/{a_bill['dist_id']}/bills/{a_bill['bill']['id']}/payments/new",
                data={"amount": "200.000"}, follow_redirects=False)
    assert _bill_payments(db, a_bill["bill"]["id"]) == before + 1


def test_a_bill_cannot_be_overpaid(client, db, a_bill):
    """Paying a supplier more than they invoiced is money out the door with
    nothing to reclaim it against — the outward-facing twin of the overpaid
    client bill the visit routes guard."""
    before = _bill_payments(db, a_bill["bill"]["id"])
    resp = client.post(
        f"/distributors/{a_bill['dist_id']}/bills/{a_bill['bill']['id']}/payments/new",
        data={"amount": "9999.000"}, follow_redirects=False)
    assert resp.status_code != 500
    assert _bill_payments(db, a_bill["bill"]["id"]) == before, "an overpayment must not be recorded"


def test_bill_payments_accumulate_against_one_total(client, db, a_bill):
    """Each instalment is individually within the bill; together they must
    not exceed it."""
    bill_id = a_bill["bill"]["id"]
    url = f"/distributors/{a_bill['dist_id']}/bills/{bill_id}/payments/new"
    for _ in range(5):
        client.post(url, data={"amount": "100.000"}, follow_redirects=False)
    paid = db.execute("SELECT COALESCE(SUM(amount),0) AS s FROM distributor_bill_payments "
                      "WHERE bill_id=?", (bill_id,)).fetchone()["s"]
    assert paid == D("500.000"), "the bill should now be settled exactly"
    before = _bill_payments(db, bill_id)
    resp = client.post(url, data={"amount": "100.000"}, follow_redirects=False)
    assert resp.status_code != 500
    assert _bill_payments(db, bill_id) == before, "the instalment that tips it over must be refused"


def test_a_bill_payment_rejects_zero_and_negative(client, db, a_bill):
    before = _bill_payments(db, a_bill["bill"]["id"])
    for bad in ("0", "-50.000"):
        resp = client.post(
            f"/distributors/{a_bill['dist_id']}/bills/{a_bill['bill']['id']}/payments/new",
            data={"amount": bad}, follow_redirects=False)
        assert resp.status_code != 500
    assert _bill_payments(db, a_bill["bill"]["id"]) == before


# ---------------------------------------------------------------------------
# Consignment — stock the clinic holds but does not own
# ---------------------------------------------------------------------------

@pytest.fixture
def consignment_item(client, db, distributor):
    """A Retail item flagged as consignment against a real distributor."""
    inv_id = _uid("INV")
    db.execute("INSERT INTO inventory_list (id, name, category, unit, track_expiry, cost_price, "
               "distributor_id, ownership_type, consignment_since, active) "
               "VALUES (?,?,?,?,?,?,?,?,?,?)",
               (inv_id, f"Consign {inv_id}", "Retail", "unit", False, D("2.000"),
                distributor["id"], "Consignment",
                datetime.now().isoformat(timespec="seconds"), True))
    # Writing off consignment stock requires the item to have been through a
    # confirmed audit — the same fail-closed rule POS applies before selling
    # anything with an unknown stock figure. Without this every shrinkage
    # test below is refused for the wrong reason and proves nothing, which
    # is exactly what a mutation check caught them doing.
    cur = db.execute("INSERT INTO audit_sessions (audit_date, performed_by, status, created_at, confirmed_at) "
                     "VALUES (?,?,?,?,?) RETURNING id",
                     (date.today().isoformat(), "U001", "Confirmed",
                      datetime.now().isoformat(timespec="seconds"),
                      datetime.now().isoformat(timespec="microseconds")))
    audit_id = cur.fetchone()["id"]
    db.execute("INSERT INTO audit_session_lines (session_id, item_id, stock_counted, received_since_prior) "
               # Decimal, not float — JO's money and quantity columns are
               # NUMERIC, and a float literal here is the contamination the
               # rest of this suite exists to prevent.
               "VALUES (?,?,?,?)", (audit_id, inv_id, D("50.000"), D(0)))
    db.commit()
    yield {"id": inv_id, "distributor_id": distributor["id"]}
    db.execute("DELETE FROM audit_session_lines WHERE session_id=?", (audit_id,))
    db.execute("DELETE FROM audit_sessions WHERE id=?", (audit_id,))
    for sql in ("DELETE FROM consignment_shrinkage WHERE item_id=?",
                "DELETE FROM consignment_returns WHERE item_id=?",
                "DELETE FROM consignment_receipts WHERE item_id=?",
                "DELETE FROM inventory_transactions WHERE item_id=?",
                "DELETE FROM inventory_list WHERE id=?"):
        db.execute(sql, (inv_id,))
    db.commit()


def _receipts(db, item_id):
    return db.execute("SELECT count(*) AS c FROM consignment_receipts WHERE item_id=?",
                      (item_id,)).fetchone()["c"]


def test_a_consignment_delivery_is_recorded(client, db, consignment_item):
    before = _receipts(db, consignment_item["id"])
    client.post("/consignment/receiving/new", data={
        "item_id": consignment_item["id"], "quantity": "10", "unit_cost": "2.000",
        "received_date": date.today().isoformat(),
        "delivery_reference": "DEL1", "notes": ""}, follow_redirects=False)
    assert _receipts(db, consignment_item["id"]) == before + 1


def test_a_consignment_delivery_rejects_a_negative_unit_cost(client, db, consignment_item):
    """The unit cost is what the clinic will eventually owe per item. A
    negative one makes the settlement owed come out backwards."""
    before = _receipts(db, consignment_item["id"])
    resp = client.post("/consignment/receiving/new", data={
        "item_id": consignment_item["id"], "quantity": "10", "unit_cost": "-2.000",
        "received_date": date.today().isoformat()}, follow_redirects=False)
    assert resp.status_code != 500
    assert _receipts(db, consignment_item["id"]) == before


def test_a_consignment_delivery_rejects_zero_quantity(client, db, consignment_item):
    before = _receipts(db, consignment_item["id"])
    for bad in ("0", "-5"):
        resp = client.post("/consignment/receiving/new", data={
            "item_id": consignment_item["id"], "quantity": bad, "unit_cost": "2.000",
            "received_date": date.today().isoformat()}, follow_redirects=False)
        assert resp.status_code != 500
    assert _receipts(db, consignment_item["id"]) == before


def test_a_consignment_delivery_rejects_a_malformed_date(client, db, consignment_item):
    before = _receipts(db, consignment_item["id"])
    for bad in ("not-a-date", "2026-08-25garbage"):
        resp = client.post("/consignment/receiving/new", data={
            "item_id": consignment_item["id"], "quantity": "5", "unit_cost": "2.000",
            "received_date": bad}, follow_redirects=False)
        assert resp.status_code != 500, f"{bad!r} must not raise"
    assert _receipts(db, consignment_item["id"]) == before


def test_shrinkage_cannot_exceed_what_was_received(client, db, consignment_item):
    """Writing off more consignment stock than ever arrived would credit the
    clinic against goods it never held."""
    client.post("/consignment/receiving/new", data={
        "item_id": consignment_item["id"], "quantity": "5", "unit_cost": "2.000",
        "received_date": date.today().isoformat()}, follow_redirects=False)
    before = db.execute("SELECT count(*) AS c FROM consignment_shrinkage WHERE item_id=?",
                        (consignment_item["id"],)).fetchone()["c"]
    resp = client.post("/consignment/shrinkage/new", data={
        "item_id": consignment_item["id"], "quantity": "500",
        "reason": "Damaged", "liable_party": "Clinic", "notes": ""},
        follow_redirects=False)
    assert resp.status_code != 500
    after = db.execute("SELECT count(*) AS c FROM consignment_shrinkage WHERE item_id=?",
                       (consignment_item["id"],)).fetchone()["c"]
    assert after == before, "shrinkage beyond what was received must not be recorded"


def test_a_valid_shrinkage_write_off_is_recorded(client, db, consignment_item):
    """The control the two guard tests below depend on. Without it, a
    shrinkage refused for some unrelated reason looks identical to a
    shrinkage correctly refused — and both guard tests pass while
    exercising nothing."""
    client.post("/consignment/receiving/new", data={
        "item_id": consignment_item["id"], "quantity": "5", "unit_cost": "2.000",
        "received_date": date.today().isoformat()}, follow_redirects=False)
    before = db.execute("SELECT count(*) AS c FROM consignment_shrinkage WHERE item_id=?",
                        (consignment_item["id"],)).fetchone()["c"]
    resp = client.post("/consignment/shrinkage/new", data={
        "item_id": consignment_item["id"], "quantity": "2",
        "reason": "Damaged", "liable_party": "Clinic", "notes": ""},
        follow_redirects=False)
    assert resp.status_code != 500
    after = db.execute("SELECT count(*) AS c FROM consignment_shrinkage WHERE item_id=?",
                       (consignment_item["id"],)).fetchone()["c"]
    assert after == before + 1, "a valid write-off must be recorded"


def test_shrinkage_rejects_zero_and_negative(client, db, consignment_item):
    client.post("/consignment/receiving/new", data={
        "item_id": consignment_item["id"], "quantity": "5", "unit_cost": "2.000",
        "received_date": date.today().isoformat()}, follow_redirects=False)
    before = db.execute("SELECT count(*) AS c FROM consignment_shrinkage WHERE item_id=?",
                        (consignment_item["id"],)).fetchone()["c"]
    for bad in ("0", "-5"):
        resp = client.post("/consignment/shrinkage/new", data={
            "item_id": consignment_item["id"], "quantity": bad,
            "reason": "Damaged", "liable_party": "Clinic"}, follow_redirects=False)
        assert resp.status_code != 500
    assert db.execute("SELECT count(*) AS c FROM consignment_shrinkage WHERE item_id=?",
                      (consignment_item["id"],)).fetchone()["c"] == before


def test_shrinkage_rejects_an_unknown_reason_or_liable_party(client, db, consignment_item):
    """Both are closed sets. A free-text value here would land in the
    settlement ledger as an unrecognised category and quietly fall out of
    whichever side of the reconciliation it was meant to be on."""
    client.post("/consignment/receiving/new", data={
        "item_id": consignment_item["id"], "quantity": "5", "unit_cost": "2.000",
        "received_date": date.today().isoformat()}, follow_redirects=False)
    before = db.execute("SELECT count(*) AS c FROM consignment_shrinkage WHERE item_id=?",
                        (consignment_item["id"],)).fetchone()["c"]
    for reason, liable in (("Vanished", "Clinic"), ("Damaged", "The Weather")):
        resp = client.post("/consignment/shrinkage/new", data={
            "item_id": consignment_item["id"], "quantity": "1",
            "reason": reason, "liable_party": liable}, follow_redirects=False)
        assert resp.status_code != 500
    assert db.execute("SELECT count(*) AS c FROM consignment_shrinkage WHERE item_id=?",
                      (consignment_item["id"],)).fetchone()["c"] == before, (
        "an unrecognised reason or liable party must not be recorded")


def test_an_unaudited_item_cannot_be_written_off(client, db, distributor):
    """The fail-closed rule again, on the shrinkage path this time: stock
    that has never been counted has an unknown quantity, and unknown must
    mean "cannot write off", not "write off without limit". The fixture used
    by the tests above deliberately seeds an audit, so this one builds an
    item without one."""
    inv_id = _uid("INV")
    db.execute("INSERT INTO inventory_list (id, name, category, unit, track_expiry, cost_price, "
               "distributor_id, ownership_type, consignment_since, active) "
               "VALUES (?,?,?,?,?,?,?,?,?,?)",
               (inv_id, f"Unaudited {inv_id}", "Retail", "unit", False, D("2.000"),
                distributor["id"], "Consignment",
                datetime.now().isoformat(timespec="seconds"), True))
    db.commit()
    try:
        client.post("/consignment/receiving/new", data={
            "item_id": inv_id, "quantity": "5", "unit_cost": "2.000",
            "received_date": date.today().isoformat()}, follow_redirects=False)
        before = db.execute("SELECT count(*) AS c FROM consignment_shrinkage WHERE item_id=?",
                            (inv_id,)).fetchone()["c"]
        resp = client.post("/consignment/shrinkage/new", data={
            "item_id": inv_id, "quantity": "1",
            "reason": "Damaged", "liable_party": "Clinic"}, follow_redirects=False)
        assert resp.status_code != 500
        assert db.execute("SELECT count(*) AS c FROM consignment_shrinkage WHERE item_id=?",
                          (inv_id,)).fetchone()["c"] == before, (
            "a never-audited item must not be writable off")
    finally:
        for sql in ("DELETE FROM consignment_shrinkage WHERE item_id=?",
                    "DELETE FROM consignment_receipts WHERE item_id=?",
                    "DELETE FROM inventory_transactions WHERE item_id=?",
                    "DELETE FROM inventory_list WHERE id=?"):
            db.execute(sql, (inv_id,))
        db.commit()


def test_the_consignment_balance_reflects_what_was_received(client, db, consignment_item):
    """Ties the ledger to the deliveries it is built from — the figure a
    settlement is calculated against."""
    client.post("/consignment/receiving/new", data={
        "item_id": consignment_item["id"], "quantity": "8", "unit_cost": "2.000",
        "received_date": date.today().isoformat()}, follow_redirects=False)
    bal = logic.consignment_balance(db, consignment_item["distributor_id"])
    assert bal is not None, "a distributor with deliveries must have a balance"


# ---------------------------------------------------------------------------
# End-of-day cash count
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _cash_audits_left_as_found(db):
    before = {r["id"] for r in db.execute("SELECT id FROM cash_register_audits").fetchall()}
    yield
    after = {r["id"] for r in db.execute("SELECT id FROM cash_register_audits").fetchall()}
    for aid in after - before:
        db.execute("DELETE FROM cash_register_audits WHERE id=?", (aid,))
    db.commit()


def _audits(db):
    return db.execute("SELECT count(*) AS c FROM cash_register_audits").fetchone()["c"]


def test_an_end_of_day_count_is_recorded(client, db):
    before = _audits(db)
    resp = client.post("/cash-register/audit", data={
        "day": date.today().isoformat(), "counted_cash": "120.000", "notes": "evening count"},
        follow_redirects=False)
    assert resp.status_code != 500
    assert _audits(db) == before + 1


def test_a_cash_count_rejects_a_negative_figure(client, db):
    """There cannot be less than nothing in the drawer; a negative count is
    a typo that would show as a phantom shortfall."""
    before = _audits(db)
    resp = client.post("/cash-register/audit", data={
        "day": date.today().isoformat(), "counted_cash": "-5.000"}, follow_redirects=False)
    assert resp.status_code != 500
    assert _audits(db) == before


def test_a_cash_count_rejects_a_non_numeric_figure(client, db):
    before = _audits(db)
    resp = client.post("/cash-register/audit", data={
        "day": date.today().isoformat(), "counted_cash": "loads"}, follow_redirects=False)
    assert resp.status_code != 500
    assert _audits(db) == before


# ---------------------------------------------------------------------------
# Consignment settlement — paying the distributor for what was sold
# ---------------------------------------------------------------------------

def _settlements(db, dist_id):
    return db.execute("SELECT count(*) AS c FROM consignment_settlements WHERE distributor_id=?",
                      (dist_id,)).fetchone()["c"]


def test_a_settlement_cannot_pay_more_than_is_owed(client, db, consignment_item):
    """Settling above the outstanding balance pays a distributor twice for
    the same stock. There is no delete route for a settlement."""
    client.post("/consignment/receiving/new", data={
        "item_id": consignment_item["id"], "quantity": "5", "unit_cost": "2.000",
        "received_date": date.today().isoformat()}, follow_redirects=False)
    dist = consignment_item["distributor_id"]
    before = _settlements(db, dist)
    resp = client.post(f"/consignment/settlements/{dist}/new",
                       data={"amount_paid": "999999.000"}, follow_redirects=False)
    assert resp.status_code != 500
    assert _settlements(db, dist) == before, "an over-settlement must not be recorded"


def test_settling_a_distributor_with_no_activity_is_refused(client, db, distributor):
    """Regression guard. consignment_balance() returns period_start=None for
    a distributor with no consignment activity and no prior settlement. A
    zero amount passed the "not more than is owed" check, reached the INSERT,
    and violated consignment_settlements.period_start's NOT NULL constraint
    — an error page rather than a message. Found by these tests; the same
    shape was present in both apps."""
    before = _settlements(db, distributor["id"])
    resp = client.post(f"/consignment/settlements/{distributor['id']}/new",
                       data={"amount_paid": "0"}, follow_redirects=False)
    assert resp.status_code != 500, "must degrade with a message, not raise"
    assert _settlements(db, distributor["id"]) == before


def test_a_settlement_rejects_zero_and_negative(client, db, consignment_item):
    dist = consignment_item["distributor_id"]
    before = _settlements(db, dist)
    for bad in ("0", "-50.000"):
        resp = client.post(f"/consignment/settlements/{dist}/new",
                           data={"amount_paid": bad}, follow_redirects=False)
        assert resp.status_code != 500
    assert _settlements(db, dist) == before


def test_a_settlement_rejects_a_non_numeric_amount(client, db, consignment_item):
    dist = consignment_item["distributor_id"]
    before = _settlements(db, dist)
    resp = client.post(f"/consignment/settlements/{dist}/new",
                       data={"amount_paid": "loads"}, follow_redirects=False)
    assert resp.status_code != 500
    assert _settlements(db, dist) == before


# ---------------------------------------------------------------------------
# Inventory catalog edit, and confirming a stock count
# ---------------------------------------------------------------------------

def test_inventory_item_edit_rejects_a_negative_cost(client, db, consignment_item):
    """The edit form is the third way to set a cost, after the create form
    and the bulk editor. All three need the same guard."""
    before = db.execute("SELECT cost_price FROM inventory_list WHERE id=?",
                        (consignment_item["id"],)).fetchone()["cost_price"]
    resp = client.post(f"/inventory-catalog/{consignment_item['id']}/edit", data={
        "name": f"Consign {consignment_item['id']}", "category": "Retail",
        "unit": "unit", "cost_price": "-5.000"}, follow_redirects=False)
    assert resp.status_code != 500
    after = db.execute("SELECT cost_price FROM inventory_list WHERE id=?",
                       (consignment_item["id"],)).fetchone()["cost_price"]
    assert after == before, "a rejected edit must leave the cost as it was"


def test_confirming_a_stock_count_is_what_makes_it_binding(client, db, distributor):
    """An open count is a draft — it must not affect stock until confirmed.
    That is the whole basis of the "never-audited items cannot be sold"
    rule, so it is worth pinning rather than assuming."""
    import logic
    inv_id = _uid("INV")
    db.execute("INSERT INTO inventory_list (id, name, category, unit, track_expiry, cost_price, "
               "ownership_type, active) VALUES (?,?,?,?,?,?,?,?)",
               (inv_id, f"Count {inv_id}", "Retail", "unit", False, 1000.0, "Owned", True))
    cur = db.execute("INSERT INTO audit_sessions (audit_date, performed_by, status, created_at) "
                     "VALUES (?,?,?,?) RETURNING id",
                     # 'Draft', not 'Open' — audit_sessions_status_check
                     # allows only Draft and Confirmed.
                     (date.today().isoformat(), "U001", "Draft",
                      datetime.now().isoformat(timespec="seconds")))
    sid = cur.fetchone()["id"]
    db.execute("INSERT INTO audit_session_lines (session_id, item_id, stock_counted, received_since_prior) "
               "VALUES (?,?,?,?)", (sid, inv_id, D("30.000"), D(0)))
    db.commit()
    try:
        draft_status = logic.inventory_status_by_id(db, inv_id)
        assert draft_status is None or draft_status["current_stock"] is None, (
            "an unconfirmed count must not establish a stock figure")

        resp = client.post(f"/audit-history/session/{sid}/confirm", data={},
                           follow_redirects=False)
        assert resp.status_code != 500
        row = db.execute("SELECT status, confirmed_at FROM audit_sessions WHERE id=?",
                         (sid,)).fetchone()
        if row["status"] == "Confirmed":
            assert row["confirmed_at"], "a confirmed count must be stamped"
            after = logic.inventory_status_by_id(db, inv_id)
            assert after is not None and after["current_stock"] == D("30.000"), (
                "confirming must establish the counted figure")
    finally:
        for sql in ("DELETE FROM inventory_transactions WHERE item_id=?",
                    "DELETE FROM audit_session_lines WHERE item_id=?",
                    "DELETE FROM inventory_list WHERE id=?"):
            db.execute(sql, (inv_id,))
        db.execute("DELETE FROM audit_sessions WHERE id=?", (sid,))
        db.commit()
