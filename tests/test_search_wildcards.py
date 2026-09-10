"""
Search boxes must look for what was typed.

Every search in this app built f"%{term}%" and passed it to ILIKE. The query
is parameterised, so this was never an injection route — but % and _ are
wildcards inside the pattern however the pattern was built. Searching for
"50%" matched every row in the table, and an item called "A_B" also matched
"AxB". Wrong results, with nothing to indicate anything had gone wrong.

Postgres treats backslash as LIKE's default escape character, so escaping the
pattern is sufficient and no ESCAPE clause is needed at the ten call sites —
asserted directly below rather than taken on faith, because the whole fix
rests on it.
"""
import uuid

import pytest

import logic
from conftest import needs_db

pytestmark = needs_db


# ---------------------------------------------------------------------------
# The helper itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("plain",      "%plain%"),
    ("A_B",        r"%A\_B%"),
    ("50%",        r"%50\%%"),
    ("a%b_c",      r"%a\%b\_c%"),
    ("back\\slash", r"%back\\slash%"),
    ("",           "%%"),
    (None,         "%%"),
])
def test_like_pattern_escapes_wildcards(raw, expected):
    assert logic.like_pattern(raw) == expected


def test_backslash_is_escaped_before_the_others():
    r"""GUARD on ordering. Escaping % or _ first turns the backslash this
    inserts into an escape for the next replacement, and '\%' becomes '\\%' —
    a literal backslash followed by a live wildcard."""
    assert logic.like_pattern("100%") == r"%100\%%"
    assert logic.like_pattern("\\") == r"%\\%"


# ---------------------------------------------------------------------------
# Against the real database — the assumption the fix rests on
# ---------------------------------------------------------------------------

def test_postgres_treats_backslash_as_the_default_like_escape(db):
    """GUARD on the premise. If a future Postgres or a changed
    standard_conforming_strings broke this, every test below would still pass
    while real searches silently went back to wildcarding."""
    row = db.execute(
        r"SELECT ('a_b' ILIKE '%a\_b%') AS matches, ('axb' ILIKE '%a\_b%') AS wildcards"
    ).fetchone()
    assert row["matches"] is True
    assert row["wildcards"] is False, (
        "backslash is not acting as LIKE's escape character — like_pattern() "
        "needs an explicit ESCAPE clause at every call site")


@pytest.fixture
def owners(db):
    """Three owners whose names differ only in the characters that used to be
    wildcards."""
    tag = uuid.uuid4().hex[:6]
    names = [f"A_B{tag}", f"AXB{tag}", f"50%off{tag}"]
    ids = []
    for name in names:
        oid = f"OW{uuid.uuid4().hex[:8].upper()}"
        db.execute("INSERT INTO owners (id, name, phone) VALUES (?,?,?)",
                   (oid, name, f"0770{uuid.uuid4().int % 1000000:06d}"))
        ids.append(oid)
    db.commit()
    yield tag, names
    for oid in ids:
        db.execute("DELETE FROM owners WHERE id=?", (oid,))
    db.commit()


def _search(db, term):
    return [r["name"] for r in db.execute(
        "SELECT name FROM owners WHERE name ILIKE ? ORDER BY name",
        (logic.like_pattern(term),)).fetchall()]


def test_underscore_matches_only_a_literal_underscore(db, owners):
    """GUARD. Unescaped, "A_B" also matches "AXB"."""
    tag, _ = owners
    found = _search(db, f"A_B{tag}")
    assert found == [f"A_B{tag}"], f"underscore behaved as a wildcard: {found}"


def test_percent_does_not_match_everything(db, owners):
    """GUARD. Unescaped, "50%" becomes the pattern %50%% — which matches every
    row containing "50" anywhere, and in a real clinic that is most of them.

    Searching the FULL name "50%off<tag>" is not a guard: unescaped it becomes
    %50%off<tag>%, where the stray % sits between two literals that still pin
    the one row, so it passes either way. Searching the prefix alone is what
    actually separates the two behaviours.
    """
    tag, names = owners
    # A decoy that shares the "50" but not the rest of the name.
    decoy = f"5000 Dinars Clinic {tag}"
    db.execute("INSERT INTO owners (id, name, phone) VALUES (?,?,?)",
               (f"OWDECOY{tag.upper()}", decoy, f"0771{tag[:6]}"))
    db.commit()
    try:
        found = _search(db, "50%")
        assert found == [f"50%off{tag}"], (
            f"percent behaved as a wildcard and pulled in unrelated rows: {found}")
        assert decoy not in found
    finally:
        db.execute("DELETE FROM owners WHERE id=?", (f"OWDECOY{tag.upper()}",))
        db.commit()


def test_an_ordinary_substring_search_still_works(db, owners):
    """CONTROL. Escaping everything, or escaping the wrapping %%, would make
    every guard above pass while breaking search completely."""
    tag, names = owners
    found = _search(db, tag)
    assert sorted(found) == sorted(names), f"substring search broke: {found}"


def test_a_partial_word_still_matches(db, owners):
    """CONTROL. The pattern must still be a substring match, not exact."""
    tag, _ = owners
    assert f"AXB{tag}" in _search(db, "AXB")
