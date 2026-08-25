"""
Money-path tests — VetClinicSystem JO (JOD).

Why this file exists: the money math is the longest-lived, least-visible
code in the app. A wrong colour is obvious the moment someone looks at
it; a wrong total is a bill a client actually paid. Until this file, all
of it was verified by hand.

JO's money model, which every assertion below depends on:
  - Amounts are `Decimal`, columns are NUMERIC(12,3).
  - The JOD has a fils subunit in everyday use, so amounts are exact to
    3 decimal places. There is no note-rounding step and no "smallest
    note" — 0.100 JOD is a real, payable amount.
  - Mixing Decimal and float raises TypeError *on purpose*. A silent
    float coercion would reintroduce exactly the precision loss the
    Decimal model exists to prevent.
  - A leftover balance is real debt, however small. There is no
    tolerance threshold.

IQ is deliberately different (float, 250 IQD note rounding, an
anti-"looks free" floor). Its equivalent file makes the opposite
assertions on purpose — see COMPARISON.md §1.1 before copying anything
between them. The single most expensive mistake available in this
codebase is porting a money fix across without re-deriving it.
"""
from decimal import Decimal

import pytest

import app
import logic


D = Decimal


# ---------------------------------------------------------------------------
# parse_money — the front door. Everything downstream trusts its output.
# ---------------------------------------------------------------------------

def test_parse_money_returns_decimal_never_float():
    """The load-bearing assertion of the whole JO money model. If this ever
    returns a float, every Decimal guard downstream silently stops guarding
    and 3-decimal precision is lost without anything raising."""
    value = app.parse_money("10.500")
    assert isinstance(value, Decimal)
    assert not isinstance(value, float)


def test_parse_money_preserves_fils_precision():
    """0.1 + 0.2 != 0.3 in binary floating point. This is the entire reason
    JO does not use float: a fils must survive the round trip exactly."""
    assert app.parse_money("0.001") == D("0.001")
    assert app.parse_money("10.505") == D("10.505")
    assert app.parse_money("0.1") + app.parse_money("0.2") == D("0.3")


def test_parse_money_blank_is_none():
    assert app.parse_money("") is None
    assert app.parse_money("   ") is None
    assert app.parse_money(None) is None


def test_parse_money_blank_but_required_raises():
    with pytest.raises(app.BadNumber):
        app.parse_money("", required=True)


@pytest.mark.parametrize("hostile", ["nan", "NaN", "inf", "-inf", "Infinity"])
def test_parse_money_rejects_nan_and_infinity(hostile):
    """The trap parse_money's own comment documents: Decimal("nan") parses
    happily, and every bound check downstream (`x > cap`, `x < 0`) is False
    against NaN — so an unchecked NaN doesn't merely slip past validation,
    it appears to *pass* every check. Must be rejected at the door."""
    with pytest.raises(app.BadNumber):
        app.parse_money(hostile)


@pytest.mark.parametrize("garbage", ["abc", "1,000", "10.0.0", "JD50", "12 34"])
def test_parse_money_rejects_non_numeric(garbage):
    with pytest.raises(app.BadNumber):
        app.parse_money(garbage)


@pytest.mark.parametrize("arabic,expected", [("١٠٠", 100), ("٢٥٠", 250), ("1٠0", 100)])
def test_parse_money_accepts_arabic_indic_digits(arabic, expected):
    """Not an accident worth "fixing": Python's Decimal() parses Arabic-Indic
    digits, so a clinic can type ٢٥٠ into a price field and get 250. IQ
    behaves identically. Locked in so that restricting input to ASCII digits
    later is a deliberate decision with a failing test to justify it, rather
    than a silent regression for the people this app was built for."""
    assert app.parse_money(arabic) == expected


def test_parse_money_rejects_values_too_large_for_the_column():
    """NUMERIC(12,3) has a real ceiling. Proactive rejection gives a usable
    message instead of a Postgres error. (IQ has no equivalent cap — a
    documented divergence, see its own test file.)"""
    assert app.parse_money(str(app.MAX_MONEY)) == app.MAX_MONEY
    with pytest.raises(app.BadNumber):
        app.parse_money("1000000000000000000")


def test_parse_money_allows_negative_by_design():
    """Negative is not rejected here — has_negative() is the separate guard
    for the fields where negative is never valid. Locking this in so nobody
    "helpfully" adds a sign check here and silently breaks refunds."""
    assert app.parse_money("-500.250") == D("-500.250")


# ---------------------------------------------------------------------------
# parse_quantity / parse_int / has_negative — the other input guards
# ---------------------------------------------------------------------------

def test_parse_quantity_returns_decimal_and_bounds_at_its_own_ceiling():
    """Quantities are NUMERIC(10,3) — a narrower column than money, so it
    has its own, lower cap rather than borrowing MAX_MONEY's."""
    assert isinstance(app.parse_quantity("2.5"), Decimal)
    assert app.parse_quantity(str(app.MAX_QUANTITY)) == app.MAX_QUANTITY
    with pytest.raises(app.BadNumber):
        app.parse_quantity(str(app.MAX_QUANTITY + 1))


def test_parse_quantity_rejects_nan():
    with pytest.raises(app.BadNumber):
        app.parse_quantity("nan")


def test_parse_int_blank_and_bounds():
    assert app.parse_int("") is None
    with pytest.raises(app.BadNumber):
        app.parse_int("", required=True)
    assert app.parse_int(str(app.MAX_INT)) == app.MAX_INT
    with pytest.raises(app.BadNumber):
        app.parse_int(str(app.MAX_INT + 1))


def test_has_negative():
    assert app.has_negative(D("-1")) is True
    assert app.has_negative(D(0), D(5), D(100)) is False
    assert app.has_negative(None, D(5)) is False   # absent is not negative
    assert app.has_negative(D(5), None, D("-0.001")) is True


# ---------------------------------------------------------------------------
# compute_bill_totals — the single shared entry point for every bill in
# the app (visits, inpatient, boarding). Returns (total, paid, balance, status).
# ---------------------------------------------------------------------------

def test_bill_unpaid():
    total, paid, balance, status = logic.compute_bill_totals(D("100.000"), D(0), D(0))
    assert (total, paid, balance, status) == (D("100.000"), D(0), D("100.000"), "Unpaid")


def test_bill_applies_percentage_discount_exactly():
    total, _, balance, _ = logic.compute_bill_totals(D("100.000"), D(10), D(0))
    assert total == D("90.000")
    assert balance == D("90.000")


def test_bill_full_waiver_is_free():
    total, _, _, status = logic.compute_bill_totals(D("100.000"), D(100), D(0))
    assert total == 0
    assert status == "N/A"


@pytest.mark.parametrize("subtotal", ["0.001", "0.100", "0.500", "1.000"])
def test_small_amounts_are_kept_exactly_not_floored(subtotal):
    """The direct opposite of IQ, and the reason these two files must never
    be merged: IQ lifts anything under half a note to 250 IQD so a real
    charge never prints as free. In JOD, 0.100 is a genuine payable amount
    — inflating it would overcharge the client."""
    total, _, _, status = logic.compute_bill_totals(D(subtotal), D(0), D(0))
    assert total == D(subtotal)
    assert status == "Unpaid"


def test_bill_fully_paid():
    _, _, balance, status = logic.compute_bill_totals(D("100.000"), D(0), D("100.000"))
    assert balance == 0
    assert status == "Fully Paid"


def test_bill_partially_paid():
    _, _, balance, status = logic.compute_bill_totals(D("100.000"), D(0), D("50.000"))
    assert balance == D("50.000")
    assert status == "Partially Paid"


def test_bill_overpayment_reads_as_paid_with_negative_balance():
    """A negative balance is money owed back to the client, not a bug — the
    Refunds module is what settles it. Status must not read "Partially Paid"."""
    _, _, balance, status = logic.compute_bill_totals(D("100.000"), D(0), D("105.000"))
    assert balance == D("-5.000")
    assert status == "Fully Paid"


def test_discount_result_is_rounded_to_three_places():
    """33.333 less 3% is 32.33301 exactly. Storage is NUMERIC(12,3), so the
    total must already be 3dp before it gets there."""
    total, _, _, _ = logic.compute_bill_totals(D("33.333"), D(3), D(0))
    assert total == D("32.333")
    assert total.as_tuple().exponent >= -3


def test_cleanup_writes_off_a_flat_amount():
    total, _, _, _ = logic.compute_bill_totals(D("100.000"), D(0), D(0), cleanup_amount=D("10.000"))
    assert total == D("90.000")


def test_cleanup_cannot_drive_a_bill_negative():
    """A write-off larger than the bill clamps at zero — it must never turn
    into the clinic owing the client money."""
    total, _, _, status = logic.compute_bill_totals(D("10.000"), D(0), D(0), cleanup_amount=D("50.000"))
    assert total == 0
    assert status == "N/A"


def test_discount_and_cleanup_apply_in_that_order():
    """Discount is a percentage of the subtotal; Clean Up is a flat amount
    off what remains. Reversing them would make the write-off itself
    discountable and quietly change what the client pays."""
    total, _, _, _ = logic.compute_bill_totals(D("100.000"), D(10), D(0), cleanup_amount=D("1.000"))
    assert total == D("89.000")          # 100 -10% = 90.000, then -1.000
    assert total != (D("100.000") - D("1.000")) * D("0.9")


def test_cleanup_cap_is_a_decimal_not_a_float():
    """A float constant here would poison every expression it touches — the
    TypeError guard only fires on float/Decimal *arithmetic*, and a float
    cap compared against a Decimal total compares fine while silently
    reintroducing binary rounding."""
    assert isinstance(app.CLEANUP_CAP, Decimal)
    assert app.CLEANUP_CAP == D("1.000")


# ---------------------------------------------------------------------------
# Decimal discipline — the guard rails that make the model self-enforcing.
# ---------------------------------------------------------------------------

def test_float_contamination_raises_rather_than_silently_coercing():
    """Deliberate design: a float reaching the money math must fail loudly at
    that line. Silent coercion is how a 3-decimal currency quietly becomes a
    binary-float currency, and nothing downstream would ever report it."""
    with pytest.raises(TypeError):
        logic.compute_bill_totals(D("100.000"), 10.5, D(0))
    with pytest.raises(TypeError):
        logic.compute_bill_totals(10.5, D(0), D(0))


def test_plain_integers_still_mix_with_decimal():
    """Only *float* is forbidden. int literals and parse_int() results must
    keep working, or every quantity calculation breaks."""
    total, _, _, _ = logic.compute_bill_totals(D("100.000"), 10, 0)
    assert total == D("90.000")


def test_no_float_ever_appears_in_the_returned_tuple():
    for result in logic.compute_bill_totals(D("100.000"), D(10), D("50.000")):
        assert not isinstance(result, float)


# ---------------------------------------------------------------------------
# Invariants — these are the ones that catch a bug nobody thought to write
# a specific case for.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("subtotal", ["0.000", "0.001", "13.700", "100.000", "33333.333"])
@pytest.mark.parametrize("discount", [0, 7, 33, 50, 99, 100])
def test_totals_never_exceed_three_decimal_places(subtotal, discount):
    """NUMERIC(12,3) silently rounds a 4th decimal on write. Anything with
    more precision than the column means the stored figure and the displayed
    figure can disagree by a fils."""
    total, _, balance, _ = logic.compute_bill_totals(D(subtotal), D(discount), D(0))
    assert total.as_tuple().exponent >= -3
    assert balance.as_tuple().exponent >= -3


@pytest.mark.parametrize("subtotal", ["0.000", "10.000", "100.000"])
@pytest.mark.parametrize("cleanup", ["0.000", "1.000", "99999.000"])
def test_total_is_never_negative(subtotal, cleanup):
    total, _, _, _ = logic.compute_bill_totals(D(subtotal), D(0), D(0), cleanup_amount=D(cleanup))
    assert total >= 0


@pytest.mark.parametrize("subtotal", ["10.000", "13.755", "100.000", "77777.777"])
def test_paying_the_stated_total_always_settles_the_bill(subtotal):
    """Round-trip: whatever total the app shows, paying exactly that must
    read as Fully Paid with nothing left over."""
    total, _, _, _ = logic.compute_bill_totals(D(subtotal), D(0), D(0))
    _, _, balance, status = logic.compute_bill_totals(D(subtotal), D(0), total)
    assert status == "Fully Paid"
    assert balance == 0


def test_status_is_always_one_of_the_four_known_values():
    seen = set()
    for subtotal in ("0.000", "0.100", "100.000"):
        for paid in ("0.000", "0.100", "100.000", "99999.000"):
            seen.add(logic.compute_bill_totals(D(subtotal), D(0), D(paid))[3])
    assert seen <= {"N/A", "Unpaid", "Partially Paid", "Fully Paid"}


# ---------------------------------------------------------------------------
# Regression guards — each of these is a bug that actually happened.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("leftover", ["0.001", "0.100", "0.500"])
def test_regression_any_leftover_balance_is_real_debt(leftover):
    """COMPARISON.md §1.1, the failure mode that has already bitten this
    codebase once. The `balance <= 0.5` threshold was carried over unchanged
    from the IQD fork, where it absorbed genuine rounding artifact. In JOD
    there is no rounding artifact to absorb, so it silently marked bills with
    up to 500 fils still owing as "Fully Paid" — uncollected money that
    disappeared from every outstanding-balance view."""
    total = D("100.000")
    paid = total - D(leftover)
    _, _, balance, status = logic.compute_bill_totals(total, D(0), paid)
    assert balance == D(leftover)
    assert status == "Partially Paid", f"{leftover} JOD still owing must not read as paid"


def test_regression_no_smallest_note_rounding_leaked_in_from_iq():
    """Guards the copy-paste direction that would do the most damage: if
    IQ's 250-rounding were ever ported across, this exact bill would come
    back as 250.000 JOD instead of 100.000."""
    total, _, _, _ = logic.compute_bill_totals(D("100.000"), D(0), D(0))
    assert total == D("100.000")
    assert not hasattr(logic, "SMALLEST_NOTE")


def test_regression_parse_date_validates_the_whole_value_not_a_prefix():
    """v1.8.1. parse_date truncated to 10 characters *before* validating, so
    "2026-08-25garbage" parsed clean and the untruncated string reached a
    DATE column. IQ had a reproducible 500 from this; JO shared the flawed
    helper."""
    with pytest.raises(ValueError):
        logic.parse_date("2026-08-25garbage")
    assert logic.parse_date("2026-08-25").isoformat() == "2026-08-25"
    # ...while the ISO timestamps that TEXT columns really store still parse.
    assert logic.parse_date("2026-08-25T02:00:00").isoformat() == "2026-08-25"
