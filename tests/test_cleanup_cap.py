"""
"Clean Up" writes off part of a bill. Two rules bound it — a per-bill ceiling
and the remaining balance — and until 2026-09-10 neither had a single test in
either app.

That was found by extracting the checks into one helper and then removing one
of them: nothing failed. Four copies of a rule, none of them covered, is worse
than one copy uncovered, because it also hides how far the copies had drifted.

Amounts here are JOD: exact three-decimal Decimal against app.CLEANUP_CAP,
with no denomination rounding. IQ's copy uses whole IQD against
money.CLEANUP_CAP and steps in multiples of 250, because a service amount
there passes through round_to_denomination(). Same rules, each stated in its
own app's money model; the two must not be merged (COMPARISON.md §1.1).
"""
import pytest

from decimal import Decimal

from core import CLEANUP_CAP as CAP, cleanup_amount_error


# ---------------------------------------------------------------------------
# GUARDS
# ---------------------------------------------------------------------------

def test_a_negative_write_off_is_refused():
    """A negative Clean Up would ADD to what the client owes, under a control
    labelled as a write-off."""
    assert "negative" in cleanup_amount_error(Decimal("-0.001"), 0, Decimal("100.000"))


def test_a_write_off_over_the_cap_is_refused():
    assert "exceed" in cleanup_amount_error(CAP + Decimal("0.001"), 0, Decimal("100.000"))


def test_the_cap_accumulates_across_submissions():
    """GUARD. A bill can be cleaned up more than once; the ceiling is on the
    total, not on each submission. Checking only the new amount would let an
    unlimited write-off through in small pieces."""
    assert cleanup_amount_error(CAP, CAP, Decimal("100.000")) is not None
    assert cleanup_amount_error(Decimal("0.001"), CAP, Decimal("100.000")) is not None


def test_a_write_off_larger_than_the_balance_is_refused():
    """GUARD. Writing off more than is owed would turn a bill negative."""
    err = cleanup_amount_error(Decimal("0.500"), 0, Decimal("0.250"))
    assert err is not None and "remaining balance" in err


# ---------------------------------------------------------------------------
# CONTROLS — the feature still works
# ---------------------------------------------------------------------------

def test_an_ordinary_write_off_is_allowed():
    """Without this, 'refuse everything' passes every guard above."""
    assert cleanup_amount_error(Decimal("0.250"), 0, Decimal("100.000")) is None


def test_exactly_the_cap_is_allowed():
    """Boundary: the rule is 'may not exceed', not 'must be under'."""
    assert cleanup_amount_error(CAP, 0, Decimal("100.000")) is None


def test_exactly_the_balance_is_allowed():
    """Boundary: clearing the remainder exactly is the common case."""
    assert cleanup_amount_error(Decimal("0.250"), 0, Decimal("0.250")) is None


def test_zero_is_allowed():
    """Every payment form posts this field whether or not it was used."""
    assert cleanup_amount_error(0, 0, Decimal("100.000")) is None


def test_a_new_sale_has_nothing_to_accumulate_against():
    """CONTROL for POS, which passes existing_amount=0 — a brand-new sale has
    no prior Clean Up, unlike the three surfaces paid off over time."""
    assert cleanup_amount_error(CAP, 0, Decimal("100.000")) is None


# ---------------------------------------------------------------------------
# The helper is actually wired to a route
# ---------------------------------------------------------------------------

def test_the_helper_is_used_by_every_payment_surface():
    """GUARD. The rules being right is worth nothing if a surface still
    carries its own copy — which is how the four drifted in the first place."""
    import pathlib
    import re

    root = pathlib.Path(__file__).parent.parent
    sources = [root / "app.py", root / "core.py"] + sorted((root / "routes").glob("*.py"))
    src = "\n".join(p.read_text(encoding="utf-8") for p in sources)
    assert src.count("cleanup_amount_error(") >= 5, (
        "expected the helper plus at least four call sites")
    body = src.split("def cleanup_amount_error(", 1)[1].split("\ndef ", 1)[0]
    inline = [m for m in re.finditer(r'Clean Up can\'t exceed the remaining balance', src)]
    assert len(inline) == 1, (
        f"{len(inline)} copies of the balance message — it should live only in "
        f"cleanup_amount_error()")
    assert "remaining balance" in body
