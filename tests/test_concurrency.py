"""
The races the locks exist to prevent.

app.py carries a dozen `SELECT ... FOR UPDATE` locks and a dozen
idempotency guards, every one of them written against a specific race the
comments describe in detail: two checkouts overselling the same stock, a
double-clicked form creating two sales, two settlements paying a
distributor twice for one batch. Until now every test in this suite was
single-threaded, so not one of those races had ever actually been run.

These tests are deliberately few. Concurrency tests are the flakiest kind
there is, and a flaky test that gets ignored is worse than no test — so
each one here is written to be deterministic: real threads, real
connections, real contention, but an assertion that holds regardless of
which thread happens to win.

What is asserted is always an invariant, never an ordering. "Exactly one
sale exists" is true no matter who got there first. "Thread A succeeded
and thread B failed" would not be.

Needs a throwaway Postgres; skips cleanly without one. See conftest.py.
"""
import threading
import uuid
from decimal import Decimal as D
from datetime import date, datetime

import pytest

from conftest import needs_db, TEST_DB_URL


pytestmark = needs_db


def _uid(prefix):
    return f"{prefix}{uuid.uuid4().hex[:8].upper()}"


def _run_together(fn, count):
    """Run `fn(i)` on `count` threads released as close to simultaneously as
    a barrier allows, and collect what each returned.

    The barrier matters: without it the first thread usually finishes before
    the second starts, the contention never happens, and the test passes
    while proving nothing about locking at all.
    """
    barrier = threading.Barrier(count)
    results, errors = [None] * count, [None] * count

    def worker(i):
        try:
            barrier.wait(timeout=20)
            results[i] = fn(i)
        except Exception as exc:                       # noqa: BLE001 — reported, not handled
            errors[i] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), (
        "a thread never finished — the lock it was waiting on was never released")
    return results, errors


@pytest.fixture
def sellable(db):
    """An audited item with limited stock, so overselling is possible."""
    inv_id, pl_id = _uid("INV"), _uid("PL")
    db.execute("INSERT INTO inventory_list (id, name, category, unit, track_expiry, cost_price, "
               "ownership_type, active) VALUES (?,?,?,?,?,?,?,?)",
               (inv_id, f"Race Item {inv_id}", "Retail", "unit", False, D("2.000"), "Owned", True))
    db.execute("INSERT INTO price_list (id, name, category, cost_price, sale_price, active, "
               "linked_item_id, can_discount) VALUES (?,?,?,?,?,?,?,?)",
               (pl_id, f"Race Item {inv_id}", "Retail", D("2.000"), D("10.000"), True, inv_id, True))
    cur = db.execute("INSERT INTO audit_sessions (audit_date, performed_by, status, created_at, confirmed_at) "
                     "VALUES (?,?,?,?,?) RETURNING id",
                     (date.today().isoformat(), "U001", "Confirmed",
                      datetime.now().isoformat(timespec="seconds"),
                      datetime.now().isoformat(timespec="microseconds")))
    sid = cur.fetchone()["id"]
    # Exactly 5 in stock: two concurrent sales of 3 cannot both be legitimate.
    db.execute("INSERT INTO audit_session_lines (session_id, item_id, stock_counted, received_since_prior) "
               "VALUES (?,?,?,?)", (sid, inv_id, D("5.000"), D(0)))
    db.commit()
    yield {"inv_id": inv_id, "pl_id": pl_id, "stock": 5}
    for sql in ("DELETE FROM inventory_transactions WHERE item_id=?",
                "DELETE FROM sale_items WHERE item_id=?",
                "DELETE FROM audit_session_lines WHERE item_id=?",
                "DELETE FROM price_list WHERE linked_item_id=?",
                "DELETE FROM inventory_list WHERE id=?"):
        db.execute(sql, (inv_id,))
    db.execute("DELETE FROM audit_sessions WHERE id=?", (sid,))
    db.commit()


def _fresh_client(flask_app):
    """Each thread needs its own logged-in client — a Flask test client is
    not safe to share across threads."""
    c = flask_app.test_client()
    c.post("/login", data={"username": "admin", "password": "Admin12345!"},
           follow_redirects=True)
    return c


# ---------------------------------------------------------------------------

def test_two_simultaneous_checkouts_cannot_oversell_the_same_item(flask_app, db, sellable):
    """The race `pos_checkout`'s FOR UPDATE comment describes: two cashiers
    hit checkout at the same moment, both read "5 in stock", both pass the
    check, and 6 units get sold from a shelf holding 5.

    Asserted as an invariant — at most one of the two 3-unit sales may
    land — so it holds whichever thread wins.
    """
    inv_id = sellable["inv_id"]
    before = db.execute("SELECT count(*) AS c FROM sale_items WHERE item_id=?",
                        (inv_id,)).fetchone()["c"]

    def checkout(_i):
        c = _fresh_client(flask_app)
        r = c.post("/pos/checkout", data={"item_id": inv_id, "quantity": "3",
                                          "payment_method": "Card"},
                   follow_redirects=False)
        return r.status_code

    results, errors = _run_together(checkout, 2)
    assert not any(errors), f"a checkout thread raised: {[e for e in errors if e]}"

    sold = db.execute("SELECT COALESCE(SUM(quantity),0) AS q FROM sale_items WHERE item_id=?",
                      (inv_id,)).fetchone()["q"]
    lines = db.execute("SELECT count(*) AS c FROM sale_items WHERE item_id=?",
                       (inv_id,)).fetchone()["c"]
    assert sold <= sellable["stock"], (
        f"{sold} units sold from a shelf holding {sellable['stock']} — the stock lock did not hold")
    assert lines - before <= 1, "both concurrent checkouts completed; only one could be legitimate"


def test_the_same_idempotency_key_twice_at_once_creates_one_sale(flask_app, db, sellable):
    """A double-clicked Checkout button, arriving as two requests in flight
    together. The fast-path lookup cannot help here — neither request has
    committed when the other reads — so this exercises the unique index and
    the IntegrityError recovery behind it, which is the real guarantee.
    """
    inv_id = sellable["inv_id"]
    key = f"race-{uuid.uuid4().hex}"

    def checkout(_i):
        c = _fresh_client(flask_app)
        r = c.post("/pos/checkout", data={"item_id": inv_id, "quantity": "1",
                                          "payment_method": "Card",
                                          "idempotency_key": key},
                   follow_redirects=False)
        return r.status_code

    _results, errors = _run_together(checkout, 2)
    assert not any(errors), f"a checkout thread raised: {[e for e in errors if e]}"

    n = db.execute("SELECT count(*) AS c FROM sales WHERE idempotency_key=?", (key,)).fetchone()["c"]
    assert n == 1, f"{n} sales share one idempotency key — the double-submit guard did not hold"


def test_two_simultaneous_payments_cannot_overpay_one_visit(flask_app, db):
    """Each payment is individually within the balance; together they exceed
    it. Without the row lock both read the same "already paid" total, both
    pass, and the client is overpaid with no delete route to undo it.
    """
    o_id, p_id, v_id = _uid("O"), _uid("P"), _uid("V")
    db.execute("INSERT INTO owners (id, name) VALUES (?,?)", (o_id, f"Race Owner {o_id}"))
    db.execute("INSERT INTO patients (id, owner_id, animal_name) VALUES (?,?,?)",
               (p_id, o_id, f"Race Pet {p_id}"))
    db.execute("INSERT INTO visits (id, patient_id, date, case_status) VALUES (?,?,?,?)",
               (v_id, p_id, date.today().isoformat(), "Ongoing"))
    db.commit()
    admin = _fresh_client(flask_app)
    admin.post(f"/visits/{v_id}/billing",
               data={"billing_type": "Manual", "manual_amount": "100.000"}, follow_redirects=False)

    try:
        # Two payments of 75.000 against a 100.000 bill: either alone is fine.
        def pay(_i):
            c = _fresh_client(flask_app)
            r = c.post(f"/visits/{v_id}/payment",
                       data={"amount": "75.000", "method": "Cash"}, follow_redirects=False)
            return r.status_code

        _results, errors = _run_together(pay, 2)
        assert not any(errors), f"a payment thread raised: {[e for e in errors if e]}"

        paid = db.execute("SELECT COALESCE(SUM(amount),0) AS s FROM payments WHERE visit_id=?",
                          (v_id,)).fetchone()["s"]
        total = db.execute("SELECT total FROM billing WHERE visit_id=?", (v_id,)).fetchone()["total"]
        # No tolerance in JOD — any excess is real money, not rounding noise.
        assert paid <= total, (
            f"{paid} collected against a {total} bill — the balance lock did not hold")
    finally:
        for sql in ("DELETE FROM payments WHERE visit_id=?",
                    "DELETE FROM visit_billing_lines WHERE visit_id=?",
                    "DELETE FROM billing WHERE visit_id=?",
                    "DELETE FROM visits WHERE id=?"):
            db.execute(sql, (v_id,))
        db.execute("DELETE FROM patients WHERE id=?", (p_id,))
        db.execute("DELETE FROM owners WHERE id=?", (o_id,))
        db.commit()


def test_a_backup_and_an_update_cannot_run_at_the_same_time(flask_app):
    """maintenance_lock is what stops a restore landing halfway through a
    backup, or an update switching releases mid-dump. It is reentrant on
    purpose — the update flow holds it and then calls a backup that takes it
    again — so this checks the property that actually matters: a *second
    thread* is refused while the first holds it.
    """
    import backup

    acquired_by_second = []
    holder_ready = threading.Event()
    release_now = threading.Event()

    def holder():
        backup.maintenance_lock.acquire()
        holder_ready.set()
        release_now.wait(timeout=15)
        backup.maintenance_lock.release()

    def contender():
        holder_ready.wait(timeout=15)
        acquired_by_second.append(backup.maintenance_lock.acquire(blocking=False))

    t1 = threading.Thread(target=holder)
    t2 = threading.Thread(target=contender)
    t1.start(); t2.start()
    t2.join(timeout=20)
    release_now.set()
    t1.join(timeout=20)

    assert acquired_by_second == [False], (
        "a second thread took maintenance_lock while another operation held it — "
        "a backup and an update could run over each other")
