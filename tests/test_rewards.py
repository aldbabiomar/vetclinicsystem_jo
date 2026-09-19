# -*- coding: utf-8 -*-
"""
Rewards card — the member discount across every payment surface (JO).

Deliberately NOT a copy of IQ's file. The figures here are fractional JOD so
they exercise exact 3-decimal arithmetic; IQ's use whole thousands that land
on a 250-note boundary, because IQ rounds to the note and JO has nothing to
round (COMPARISON.md §1.1). A test moved across unchanged would assert IQ's
money model against JO's.

The shape that matters here, and the reason this file is organised by
BEHAVIOUR rather than by route: this feature puts one rule on four payment
paths that were already written differently from one another. A per-surface
test file would test each path against its own expectations and stay green
through exactly the disagreement it is supposed to catch — which is what
happened to the 728/709-test suite in SIMULATION_AUDIT_2026-09-11.md.

Every guard below is paired with a CONTROL asserting the valid case still
succeeds. Without one, "refused for the right reason" and "refused for any
reason at all" are indistinguishable, and this codebase has shipped several
of the latter (CLAUDE.md §7.3).
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest

import logic
from conftest import needs_db

pytestmark = needs_db

TOLERANCE = Decimal("0.001")
HUNDRED = Decimal(100)

RATE = Decimal("10")


def _uid(prefix):
    import uuid
    return f"{prefix}{uuid.uuid4().hex[:8].upper()}"


def _set_rate(db, rate=RATE):
    db.execute("INSERT INTO settings (key,value) VALUES (?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               ("member_discount_percent", str(rate)))
    db.commit()


@pytest.fixture
def rate_on(db):
    _set_rate(db, RATE)
    yield RATE
    _set_rate(db, 0)


@pytest.fixture
def items(db):
    """Two priced services: A discountable, B deliberately not."""
    a, b = _uid("PLA"), _uid("PLB")
    db.execute("INSERT INTO price_list (id, name, category, cost_price, sale_price, active, can_discount) "
               "VALUES (?,?,?,?,?,?,?)", (a, f"Eligible {a}", "Service", Decimal("0.000"), Decimal("10.500"), True, True))
    db.execute("INSERT INTO price_list (id, name, category, cost_price, sale_price, active, can_discount) "
               "VALUES (?,?,?,?,?,?,?)", (b, f"Full price {b}", "Medicine", Decimal("0.000"), Decimal("10.500"), True, False))
    db.commit()
    yield {"a": a, "b": b, "price": Decimal("10.500")}
    for pid in (a, b):
        db.execute("DELETE FROM visit_billing_lines WHERE price_id=?", (pid,))
        db.execute("DELETE FROM price_list WHERE id=?", (pid,))
    db.commit()


def _owner_chain(db, *, member, expires=None):
    o, p, v = _uid("O"), _uid("P"), _uid("V")
    db.execute("INSERT INTO owners (id, name, is_member, member_since, member_expires_on) "
               "VALUES (?,?,?,?,?)",
               (o, f"Rewards Owner {o}", member, date.today().isoformat() if member else None, expires))
    db.execute("INSERT INTO patients (id, owner_id, animal_name) VALUES (?,?,?)", (p, o, f"Pet {p}"))
    db.execute("INSERT INTO visits (id, patient_id, date, case_status) VALUES (?,?,?,?)",
               (v, p, date.today().isoformat(), "Ongoing"))
    db.commit()
    return {"owner_id": o, "patient_id": p, "visit_id": v}


def _cleanup_chain(db, chain):
    for sql in ("DELETE FROM payments WHERE visit_id=?",
                "DELETE FROM visit_billing_lines WHERE visit_id=?",
                "DELETE FROM billing WHERE visit_id=?",
                "DELETE FROM visits WHERE id=?"):
        db.execute(sql, (chain["visit_id"],))
    db.execute("DELETE FROM patients WHERE id=?", (chain["patient_id"],))
    db.execute("DELETE FROM owners WHERE id=?", (chain["owner_id"],))
    db.commit()


@pytest.fixture
def member(db):
    chain = _owner_chain(db, member=True)
    yield chain
    _cleanup_chain(db, chain)


@pytest.fixture
def non_member(db):
    chain = _owner_chain(db, member=False)
    yield chain
    _cleanup_chain(db, chain)


def _bill(client, visit_id, items, which=("a", "b")):
    data = {"billing_type": "Automatic", "date_billed": date.today().isoformat()}
    data["price_id"] = [items[k] for k in which]
    for k in which:
        data[f"qty_{items[k]}"] = "1"
    return client.post(f"/visits/{visit_id}/billing", data=data, follow_redirects=False)


# ---------------------------------------------------------------------------
# §11.2 — one basket through the surfaces
# ---------------------------------------------------------------------------

def test_a_member_bill_discounts_eligible_lines_only(client, db, rate_on, items, member):
    """The whole feature in one assertion: 10% off A, B charged in full."""
    _bill(client, member["visit_id"], items)
    s = logic.visit_billing_summary(db, member["visit_id"])
    assert s["discount_source"] == "member"
    assert s["discount_percent"] == RATE
    assert s["subtotal"] == Decimal("21.000")
    assert s["discountable_subtotal"] == Decimal("10.500")
    # 10.500 * 0.9 = 9.450, plus the full-price 10.500. Exact to the
    # fils, with no denomination rounding anywhere — JO has none.
    assert s["total"] == Decimal("19.950")


def test_the_receipt_adds_up_on_a_mixed_member_bill(client, db, rate_on, items, member):
    """subtotal - discount - Clean Up = total, which the old re-derivation
    (subtotal * (1 - d)) does NOT satisfy once a line is non-discountable."""
    _bill(client, member["visit_id"], items)
    s = logic.visit_billing_summary(db, member["visit_id"])
    printed_discount = s["subtotal"] - s["pre_cleanup_total"]
    assert s["subtotal"] - printed_discount - s["cleanup_amount"] == s["total"]
    # And it is NOT the naive figure, which is the point.
    assert printed_discount != s["subtotal"] * RATE / 100


def test_a_non_member_bill_is_untouched(client, db, rate_on, items, non_member):
    """CONTROL. The same basket, no card: nothing is discounted."""
    _bill(client, non_member["visit_id"], items)
    s = logic.visit_billing_summary(db, non_member["visit_id"])
    assert s["discount_source"] == "staff"
    assert s["discount_percent"] == 0
    assert s["total"] == Decimal("21.000")


def test_a_staff_discount_on_an_all_eligible_bill_is_unchanged(client, db, items, non_member):
    """CONTROL, and the no-op property the whole refactor rests on: with every
    line eligible, discountable_subtotal == subtotal and the new formula
    reduces to exactly the old subtotal * (1 - d)."""
    _bill(client, non_member["visit_id"], items, which=("a",))
    client.post(f"/visits/{non_member['visit_id']}/discount",
                data={"discount_percent": "10"}, follow_redirects=False)
    s = logic.visit_billing_summary(db, non_member["visit_id"])
    assert s["discount_source"] == "staff"
    assert s["total"] == Decimal("9.450")   # 10.500 - 10%, exactly as before the card


def test_a_manual_bill_is_discountable_in_full(client, db, rate_on, member):
    """A4 — no item lines to check, so the card applies to the whole figure."""
    client.post(f"/visits/{member['visit_id']}/billing",
                data={"billing_type": "Manual", "manual_amount": "21.000",
                      "date_billed": date.today().isoformat()}, follow_redirects=False)
    s = logic.visit_billing_summary(db, member["visit_id"])
    assert s["discountable_subtotal"] == Decimal("21.000")
    assert s["total"] == Decimal("18.900")


# ---------------------------------------------------------------------------
# §11.3 — the guards, each with its control
# ---------------------------------------------------------------------------

def test_a_staff_discount_is_refused_on_a_member_bill(client, db, rate_on, items, member):
    """Card only. Refused, not silently ignored."""
    _bill(client, member["visit_id"], items)
    before = logic.visit_billing_summary(db, member["visit_id"])["total"]
    client.post(f"/visits/{member['visit_id']}/discount",
                data={"discount_percent": "25"}, follow_redirects=False)
    after = logic.visit_billing_summary(db, member["visit_id"])
    assert after["total"] == before
    assert after["discount_percent"] == RATE
    assert after["discount_source"] == "member"


def test_setting_a_member_discount_to_zero_is_also_refused(client, db, rate_on, items, member):
    """The refusal blocks REMOVAL through this route too — which is exactly
    why the admin-only removal action below has to exist."""
    _bill(client, member["visit_id"], items)
    client.post(f"/visits/{member['visit_id']}/discount",
                data={"discount_percent": "0"}, follow_redirects=False)
    assert logic.visit_billing_summary(db, member["visit_id"])["discount_percent"] == RATE


def test_the_admin_removal_action_clears_a_member_discount(client, db, rate_on, items, member):
    _bill(client, member["visit_id"], items)
    client.post(f"/rewards/visit/{member['visit_id']}/remove-discount", follow_redirects=False)
    s = logic.visit_billing_summary(db, member["visit_id"])
    assert s["discount_percent"] == 0
    assert s["discount_source"] == "staff"
    assert s["total"] == Decimal("21.000")


def test_the_removal_action_reads_no_percentage_from_the_request(client, db, rate_on, items, member):
    """GUARD on the design itself. It is remove-only: posting a percentage
    must not set one, or it becomes a staff-discount back door that bypasses
    both the role cap and the card-only rule."""
    _bill(client, member["visit_id"], items)
    client.post(f"/rewards/visit/{member['visit_id']}/remove-discount",
                data={"discount_percent": "40"}, follow_redirects=False)
    s = logic.visit_billing_summary(db, member["visit_id"])
    assert s["discount_percent"] == 0
    assert s["total"] == Decimal("21.000")


def test_the_removal_action_refuses_a_bill_with_no_card_discount(client, db, items, non_member):
    """CONTROL. It is not a way to clear a STAFF discount."""
    _bill(client, non_member["visit_id"], items, which=("a",))
    client.post(f"/visits/{non_member['visit_id']}/discount",
                data={"discount_percent": "10"}, follow_redirects=False)
    client.post(f"/rewards/visit/{non_member['visit_id']}/remove-discount", follow_redirects=False)
    assert logic.visit_billing_summary(db, non_member["visit_id"])["discount_percent"] == 10


def test_enrolling_after_a_bill_exists_does_not_discount_it(client, db, rate_on, items, non_member):
    """A2. Membership is snapshotted when the bill is created."""
    _bill(client, non_member["visit_id"], items)
    db.execute("UPDATE owners SET is_member=true WHERE id=?", (non_member["owner_id"],))
    db.commit()
    assert logic.visit_billing_summary(db, non_member["visit_id"])["total"] == Decimal("21.000")


def test_changing_the_rate_does_not_move_an_existing_bill(client, db, rate_on, items, member):
    """A2, the other direction."""
    _bill(client, member["visit_id"], items)
    before = logic.visit_billing_summary(db, member["visit_id"])["total"]
    _set_rate(db, 50)
    try:
        assert logic.visit_billing_summary(db, member["visit_id"])["total"] == before
    finally:
        _set_rate(db, RATE)


def test_flipping_can_discount_after_billing_changes_nothing(client, db, rate_on, items, member):
    """A3. Eligibility is snapshotted per line at insert."""
    _bill(client, member["visit_id"], items)
    before = logic.visit_billing_summary(db, member["visit_id"])["total"]
    db.execute("UPDATE price_list SET can_discount=false WHERE id=?", (items["a"],))
    db.commit()
    try:
        assert logic.visit_billing_summary(db, member["visit_id"])["total"] == before
    finally:
        db.execute("UPDATE price_list SET can_discount=true WHERE id=?", (items["a"],))
        db.commit()


# ---------------------------------------------------------------------------
# Expiry — the boundary is the case an off-by-one silently breaks
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("offset,expected_source", [
    (1, "member"),    # expires tomorrow — still valid
    (0, "member"),    # expires TODAY — valid THROUGH its expiry date
    (-1, "staff"),    # expired yesterday
])
def test_the_card_is_valid_through_its_expiry_date(db, offset, expected_source):
    """`>= today`, not `> today`. A card that dies a day early is not
    something anyone reports as a bug."""
    expires = (date.today() + timedelta(days=offset)).isoformat()
    chain = _owner_chain(db, member=True, expires=expires)
    try:
        _set_rate(db, RATE)
        owner = db.execute("SELECT * FROM owners WHERE id=?", (chain["owner_id"],)).fetchone()
        _percent, source = logic.member_discount_for(db, owner)
        assert source == expected_source
    finally:
        _set_rate(db, 0)
        _cleanup_chain(db, chain)


def test_a_null_expiry_never_lapses(db):
    chain = _owner_chain(db, member=True, expires=None)
    try:
        owner = db.execute("SELECT * FROM owners WHERE id=?", (chain["owner_id"],)).fetchone()
        assert logic.is_active_member(owner) is True
    finally:
        _cleanup_chain(db, chain)


def test_expiry_never_flips_is_member(db):
    """Lapsed and never-joined must stay distinguishable — the owner page and
    the ranking badge both need the difference."""
    chain = _owner_chain(db, member=True, expires=(date.today() - timedelta(days=5)).isoformat())
    try:
        owner = db.execute("SELECT * FROM owners WHERE id=?", (chain["owner_id"],)).fetchone()
        assert owner["is_member"] is True
        assert logic.is_active_member(owner) is False
    finally:
        _cleanup_chain(db, chain)


def test_the_programme_is_off_at_a_zero_rate(db, items):
    """0 means off, and an active card then discounts nothing."""
    chain = _owner_chain(db, member=True)
    try:
        _set_rate(db, 0)
        owner = db.execute("SELECT * FROM owners WHERE id=?", (chain["owner_id"],)).fetchone()
        assert logic.member_discount_for(db, owner) == (Decimal(0), "staff")
    finally:
        _cleanup_chain(db, chain)


# ---------------------------------------------------------------------------
# The five cases below exist because the §11.4 mutation pass reintroduced
# their bugs and the suite stayed GREEN. Each one is written against the
# specific mutation that survived.
# ---------------------------------------------------------------------------

def test_a_member_bill_can_still_be_re_saved_with_a_non_discountable_item(
        client, db, rate_on, items, member):
    """MUTATION GUARD. The non-discountable check fires on the SECOND save of
    a bill, not the first — on the first there is no discount row yet. Left
    unscoped to staff discounts, it would refuse every member's bill the
    moment a full-price item was on it, i.e. break member billing outright.

    The obvious test (bill a member once) does NOT reach this and passed
    through the mutation.
    """
    _bill(client, member["visit_id"], items, which=("a",))
    assert logic.visit_billing_summary(db, member["visit_id"])["discount_source"] == "member"
    _bill(client, member["visit_id"], items, which=("a", "b"))   # add the full-price line
    s = logic.visit_billing_summary(db, member["visit_id"])
    assert len(s["lines"]) == 2, "the non-discountable line was refused on a member's bill"
    assert s["subtotal"] == Decimal("21.000")
    assert s["total"] == Decimal("19.950")


def test_re_saving_a_bill_never_re_stamps_the_membership_snapshot(
        client, db, rate_on, items, non_member):
    """MUTATION GUARD. The discount fields are in the UPSERT's INSERT column
    list and must NOT be in its DO UPDATE clause — otherwise every later save
    re-reads the card and silently applies one the customer did not hold when
    the bill was raised. Enrolling alone does not reach this; it takes a
    re-save afterwards, which is what the mutation exposed.
    """
    _bill(client, non_member["visit_id"], items)
    db.execute("UPDATE owners SET is_member=true, member_since=? WHERE id=?",
               (date.today().isoformat(), non_member["owner_id"]))
    db.commit()
    _bill(client, non_member["visit_id"], items)      # re-save
    s = logic.visit_billing_summary(db, non_member["visit_id"])
    assert s["discount_source"] == "staff"
    assert s["total"] == Decimal("21.000")


def test_compute_bill_totals_refuses_a_missing_discountable_subtotal():
    """MUTATION GUARD. Seam rule 6 checks that every CALL passes the keyword —
    it cannot see a default being added to the signature, and the mutation
    that did exactly that stayed green. This asserts the signature itself.

    The no-default is the point: a caller that forgets it would silently treat
    a non-discountable item as discountable and produce a wrong total.
    """
    with pytest.raises(TypeError):
        logic.compute_bill_totals(Decimal("21.000"), Decimal(10), 0)


def test_a_refund_prices_each_line_by_its_own_eligibility(db, rate_on):
    """MUTATION GUARD. A member's sale can mix discounted and full-price
    lines. Refunding every line at the discounted rate underpays a returned
    full-price item; refunding every line at full price overpays a discounted
    one. Nothing else in the suite reached refundable_sale_items().
    """
    inv_a, inv_b = _uid("INVA"), _uid("INVB")
    for iid in (inv_a, inv_b):
        db.execute("INSERT INTO inventory_list (id, name, category, unit, track_expiry, cost_price, ownership_type, active) "
                   "VALUES (?,?,?,?,?,?,?,?)", (iid, f"Item {iid}", "Retail", "unit", False, Decimal("1.000"), "Owned", True))
    cur = db.execute(
        "INSERT INTO sales (sale_date, subtotal, discount_percent, discount_source, total, payment_method) "
        "VALUES (?,?,?,?,?,?) RETURNING id",
        (date.today().isoformat(), Decimal("21.000"), RATE, "member", Decimal("19.950"), "Cash"))
    sale_id = cur.fetchone()["id"]
    for iid, ok in ((inv_a, True), (inv_b, False)):
        db.execute("INSERT INTO sale_items (sale_id, item_id, quantity, unit_price, line_total, discountable) "
                   "VALUES (?,?,?,?,?,?)", (sale_id, iid, 1, Decimal("10.500"), Decimal("10.500"), ok))
    db.commit()
    try:
        _sale, lines = logic.refundable_sale_items(db, sale_id)
        by_item = {l["item_id"]: l["unit_price"] for l in lines}
        assert by_item[inv_a] == Decimal("9.450"), "the eligible line should refund at the discounted price"
        assert by_item[inv_b] == Decimal("10.500"), "the full-price line should refund in full"
    finally:
        db.execute("DELETE FROM sale_items WHERE sale_id=?", (sale_id,))
        db.execute("DELETE FROM sales WHERE id=?", (sale_id,))
        for iid in (inv_a, inv_b):
            db.execute("DELETE FROM inventory_list WHERE id=?", (iid,))
        db.commit()


def test_the_member_rate_is_not_bounded_by_the_staff_role_cap(client, db, rate_on, items, member):
    """MUTATION GUARD, and the most likely way this feature breaks on day one.

    auth.discount_cap_for() bounds STAFF discretion. The card's rate is clinic
    policy, and routing it through that cap would stop any user whose cap is
    below the rate from creating a member's bill at all — every receptionist,
    on every member, silently.
    """
    with client.session_transaction() as sess:
        original = sess.get("discount_cap")
        sess["discount_cap"] = 5.0          # below the 10% member rate
    try:
        _bill(client, member["visit_id"], items)
        s = logic.visit_billing_summary(db, member["visit_id"])
        assert s["discount_source"] == "member"
        assert s["discount_percent"] == RATE, "the card's rate was clipped to the staff cap"
        assert s["total"] == Decimal("19.950")
    finally:
        with client.session_transaction() as sess:
            sess["discount_cap"] = original


def test_a_low_cap_user_is_still_capped_on_an_ordinary_staff_discount(
        client, db, items, non_member):
    """CONTROL for the above. Bypassing the cap for the card must not bypass
    it for staff — otherwise the guard is just switched off."""
    _bill(client, non_member["visit_id"], items, which=("a",))
    with client.session_transaction() as sess:
        original = sess.get("discount_cap")
        sess["discount_cap"] = 5.0
    try:
        client.post(f"/visits/{non_member['visit_id']}/discount",
                    data={"discount_percent": "25"}, follow_redirects=False)
        assert logic.visit_billing_summary(db, non_member["visit_id"])["discount_percent"] == 0
    finally:
        with client.session_transaction() as sess:
            sess["discount_cap"] = original


# ---------------------------------------------------------------------------
# The reports must agree with the bill. §11.2.
#
# Three reports read a member's bill three different ways — one apportions a
# stored total, one re-derives per line, one reads the stored total directly —
# and on a bill whose discount applies to SOME lines they can disagree while
# each looks individually right. Measured as a DELTA around one new bill, so
# whatever else the seeded database holds is irrelevant.
# ---------------------------------------------------------------------------

def _month_revenue(db):
    """This month's revenue as each of the three reports sees it."""
    this_month = date.today().strftime("%Y-%m")
    cat = logic.revenue_by_category(db, months_back=1)
    grid = cat["grid"].get(this_month, {})
    return {"total": sum(grid.values()), "by_cat": dict(grid), "month": this_month}


def test_the_reports_agree_with_the_stored_total_on_a_member_bill(
        client, db, rate_on, items, member):
    """A member's bill discounts A and charges B in full. Every report that
    counts that bill must move by the bill's STORED total — not by
    subtotal * (1 - d), which is what the old per-line derivations computed
    and which is wrong by the value of every non-discountable line."""
    db.execute("UPDATE visits SET doctor=? WHERE id=?", ("Dr Rewards Test", member["visit_id"]))
    db.commit()

    before_cat = _month_revenue(db)
    before_vets = {v["doctor"]: v["revenue"] for v in logic.vet_performance(db, months_back=1)}

    _bill(client, member["visit_id"], items)
    summary = logic.visit_billing_summary(db, member["visit_id"])
    stored = summary["total"]

    after_cat = _month_revenue(db)
    after_vets = {v["doctor"]: v["revenue"] for v in logic.vet_performance(db, months_back=1)}

    # 1. revenue_by_category, PER CATEGORY. The two items are deliberately in
    #    different Price List categories: summing the row cannot catch a
    #    mis-weighted split, because IQ's apportionment sums to the stored
    #    total whatever weights it uses. The eligible Service line must carry
    #    the discount and the Medicine line must not.
    moved = after_cat["total"] - before_cat["total"]
    assert abs(moved - stored) < TOLERANCE, (
        f"revenue_by_category moved by {moved}, the bill's stored total is {stored}")
    service_moved = after_cat["by_cat"].get("Service", 0) - before_cat["by_cat"].get("Service", 0)
    medicine_moved = after_cat["by_cat"].get("Medicine", 0) - before_cat["by_cat"].get("Medicine", 0)
    assert abs(service_moved - items["price"] * (1 - RATE / HUNDRED)) < TOLERANCE, (
        f"the eligible Service line contributed {service_moved}; it should carry the discount")
    assert abs(medicine_moved - items["price"]) < TOLERANCE, (
        f"the non-discountable Medicine line contributed {medicine_moved}; it should be charged in full")

    # 2. vet_performance — reads the stored total directly.
    vet_moved = after_vets.get("Dr Rewards Test", 0) - before_vets.get("Dr Rewards Test", 0)
    assert abs(vet_moved - stored) < TOLERANCE, (
        f"vet_performance moved by {vet_moved}, the bill's stored total is {stored}")

    # 3. And the receipt still adds up, against the same stored figure.
    assert summary["subtotal"] - (summary["subtotal"] - summary["pre_cleanup_total"]) \
        - summary["cleanup_amount"] == stored


def test_the_inpatient_pl_splits_a_member_case_by_each_line_s_own_eligibility(
        client, db, rate_on, items, member):
    """The inpatient arm of the P&L, across a month boundary.

    Spanning two months is not incidental — it is the only way this is
    observable in IQ. IQ APPORTIONS a stored total, so within a single month
    the weighting cancels in the ratio and the month total is the same
    whatever weights it uses; only the split BETWEEN months moves. (JO
    re-derives, so its total moves too, and the same assertion catches both.)
    A single-month version of this test passed while the weighting was
    reverted — the mutation run said so.

    A is eligible and logged LAST month; B is full-price and logged THIS
    month. Correct: last month carries A's discounted amount, this month
    carries B in full.
    """
    cur = db.execute(
        "INSERT INTO inpatient_cases (patient_id, admission_date, dismissed, created_by, "
        "discount_percent, discount_source) VALUES (?,?,?,?,?,?) RETURNING id",
        (member["patient_id"], date.today().isoformat(), False, "U001", RATE, "member"))
    case_id = cur.fetchone()["id"]
    last_month_day = logic.add_months(date.today().replace(day=1), -1)
    this_month = date.today().strftime("%Y-%m")
    last_month = last_month_day.strftime("%Y-%m")
    try:
        for price_id, discountable, when in (
                (items["a"], True, last_month_day.isoformat() + "T10:00:00"),
                (items["b"], False, date.today().isoformat() + "T10:00:00")):
            db.execute(
                "INSERT INTO inpatient_billing (case_id, price_id, quantity, unit_price, "
                "unit_cost, discountable, logged_by, timestamp) VALUES (?,?,?,?,?,?,?,?)",
                (case_id, price_id, 1, items["price"], 0, discountable, "U001", when))
        db.commit()
        logic.refresh_inpatient_total(db, case_id)
        db.commit()

        summary = logic.inpatient_billing_summary(db, case_id)
        revenue = logic._revenue_and_cogs_by_month(db)[0]

        # This case is the only thing these two fixtures put in either month,
        # but the seeded database may hold more — so measure this case's own
        # contribution by its known shape rather than the raw month figure.
        discounted = items["price"] * (1 - RATE / HUNDRED)
        assert abs(summary["total"] - (discounted + items["price"])) < TOLERANCE, (
            f"the case's stored total is {summary['total']}, expected "
            f"{discounted + items['price']}")
        assert revenue.get(last_month, 0) >= discounted - TOLERANCE, (
            f"{last_month} carries {revenue.get(last_month, 0)}; the eligible line "
            f"logged that month is worth {discounted} after the card")
        # The giveaway: without per-line weighting both months come out equal,
        # because the case total is simply split in proportion to raw amounts.
        assert abs(revenue.get(last_month, 0) - revenue.get(this_month, 0)) > TOLERANCE, (
            "both months carry the same amount — the P&L is splitting this case "
            "by its RAW line amounts, ignoring which line the card applied to")
    finally:
        db.execute("DELETE FROM inpatient_billing WHERE case_id=?", (case_id,))
        db.execute("DELETE FROM inpatient_cases WHERE id=?", (case_id,))
        db.commit()
