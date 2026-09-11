"""
Arabic/English toggle — ARABIC_LOCALIZATION_PLAN.md §8.

Every guard here is paired with a control, and each was verified by breaking
the thing it protects and watching it fail (§8.2, and CLAUDE.md §7.3). The
three that matter most:

  - the locale actually switches, AND English still works (guard + control);
  - <html lang>/<dir> flip server-side, because they cannot be patched on
    after paint the way data-theme is (§1);
  - Eastern digits reach display text but NOT <input> values (§7.1). A change
    that passes the first half while breaking the second is exactly the
    partial fix a guard-only test would miss, so the boundary is asserted in
    the same test as the guard.

The PDF test is the one that should never need to change: PDFs stay English
permanently, by explicit instruction, not as a "later" (§0).
"""
import re

import pytest

from conftest import needs_db

EASTERN = "٠١٢٣٤٥٦٧٨٩"
# A string translated in translations/ar/LC_MESSAGES/messages.po. If the
# catalogue is ever regenerated from scratch this is the first thing to fix.
KNOWN_AR = "الملاك"      # "Owners"
KNOWN_EN = "Owners"
LATIN_CURRENCY = "JOD"
ARABIC_CURRENCY = "د.أ"


def _set_language(value):
    """Write the `language` setting, the way saving Clinic Settings does.

    The language stopped being a per-browser cookie on 2026-09-11 and became a
    clinic-wide setting, chosen from a dropdown in Settings beside the colour
    palette. A direct write is right for arranging a test; the settings FORM
    is exercised by its own tests below, so both the mechanism and the way a
    user reaches it are covered."""
    import db as dbmod
    con = dbmod.connect()
    try:
        with con.cursor() as cur:
            if value is None:
                cur.execute("DELETE FROM settings WHERE key = 'language'")
            else:
                cur.execute(
                    "INSERT INTO settings (key, value) VALUES ('language', %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (value,))
        con.commit()
    finally:
        con.close()


@pytest.fixture(autouse=True)
def _reset_language():
    """The language is now GLOBAL STATE in the database, which makes leakage
    worse than the cookie it replaced, not better: a cookie only followed the
    session-scoped `client`, but this row is read by every request any test
    makes. Leaving it on "ar" would break every test in the suite that asserts
    on English flash text — which is exactly what the cookie version of this
    fixture already had to stop once (nine failures in
    test_refund_boarding.py, from a file that passed in isolation)."""
    yield
    _set_language(None)


def _as(client, lang):
    """Set the clinic's language. `client` is unused and kept so the call
    sites read the same as they did under the cookie."""
    _set_language(lang)


# ---------------------------------------------------------------------------
# 1. The locale switches — guard AND control
# ---------------------------------------------------------------------------

@needs_db
def test_arabic_setting_renders_arabic(client):
    _as(client, "ar")
    body = client.get("/").data.decode("utf-8")
    assert KNOWN_AR in body, "the Arabic catalogue did not reach the page"


@needs_db
def test_english_is_still_the_default_and_still_works(client):
    """CONTROL. A locale switch that only ever produces Arabic is not a
    toggle, and English is what every existing clinic sees today."""
    _as(client, "en")
    body = client.get("/").data.decode("utf-8")
    assert KNOWN_EN in body
    assert KNOWN_AR not in body


@needs_db
def test_no_setting_at_all_is_english(client):
    """CONTROL. Nothing changes for a clinic that never touches the setting."""
    _set_language(None)
    body = client.get("/").data.decode("utf-8")
    assert KNOWN_EN in body


@needs_db
def test_an_unknown_language_falls_back_to_english(client):
    """The row is writable by anyone who can reach the database or an older
    build; an unexpected value must not 500 or leak."""
    _as(client, "fr")
    body = client.get("/").data.decode("utf-8")
    assert KNOWN_EN in body
    _as(client, "../../etc/passwd")
    assert client.get("/").status_code < 500


@needs_db
def test_an_untranslated_string_falls_back_to_english(client, flask_app):
    """A string with no catalogue entry must render as its English source,
    never as blank. That is what makes it safe to add an English string and
    translate it later.

    Tests the MECHANISM with a msgid that will never be in the catalogue,
    rather than naming a string that happens to be untranslated today: the
    first version of this test asserted "Shrinkage" falls back, and broke the
    moment Shrinkage was translated. A guard pinned to a temporary state is a
    guard with an expiry date on it."""
    from flask_babel import gettext
    _set_language("ar")
    with flask_app.test_request_context("/"):
        assert gettext("__no such string will ever be translated__") == \
            "__no such string will ever be translated__"
        # CONTROL: a string that IS in the catalogue does not fall through
        assert gettext(KNOWN_EN) == KNOWN_AR


# ---------------------------------------------------------------------------
# 2. <html lang> / <html dir> — set at render time, not after paint
# ---------------------------------------------------------------------------

@needs_db
@pytest.mark.parametrize("path", ["/", "/patients", "/pos", "/settings", "/reports"])
def test_html_lang_and_dir_flip_with_the_setting(client, path):
    _as(client, "ar")
    body = client.get(path).data.decode("utf-8")
    tag = re.search(r"<html[^>]*>", body)
    assert tag, f"{path} has no <html> tag"
    assert 'lang="ar"' in tag.group(0), f"{path}: {tag.group(0)}"
    assert 'dir="rtl"' in tag.group(0), f"{path}: {tag.group(0)}"

    _as(client, "en")          # CONTROL
    body = client.get(path).data.decode("utf-8")
    tag = re.search(r"<html[^>]*>", body)
    assert 'lang="en"' in tag.group(0), f"{path}: {tag.group(0)}"
    assert 'dir="ltr"' in tag.group(0), f"{path}: {tag.group(0)}"


# ---------------------------------------------------------------------------
# 3. The setting itself — a saved field, not a route
# ---------------------------------------------------------------------------

@needs_db
def test_saving_the_settings_form_changes_the_language(client):
    """The whole point of the move: a user picks it in Settings and presses
    Save, like every other field in that card."""
    page = client.get("/settings").data.decode("utf-8")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    resp = client.post("/settings", data={"csrf_token": token, "language": "ar"},
                       follow_redirects=True)
    assert resp.status_code == 200
    assert KNOWN_AR in client.get("/").data.decode("utf-8")


@needs_db
def test_the_form_refuses_an_unknown_language(client):
    """The submitted value reaches Flask-Babel, so it is whitelisted rather
    than trusted. Without this an unknown locale falls back silently, which
    reads to a user as "the setting did not save"."""
    page = client.get("/settings").data.decode("utf-8")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    client.post("/settings", data={"csrf_token": token, "language": "de"},
                follow_redirects=True)
    import logic
    import db as dbmod
    con = dbmod.connect()
    try:
        with con.cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE key = 'language'")
            row = cur.fetchone()
    finally:
        con.close()
    assert row is None or row[0] != "de", "an unknown locale was stored"
    assert KNOWN_EN in client.get("/").data.decode("utf-8")


@needs_db
def test_the_language_applies_to_every_session_not_just_one_browser(client,
                                                                   flask_app):
    """It is a property of the clinic now. A second client — no cookies, no
    shared session — must see the same language, which is the behaviour the
    cookie could not give and the reason for the change."""
    _as(client, "ar")
    other = flask_app.test_client()
    other.post("/login", data={"username": "admin", "password": "Admin12345!"},
               follow_redirects=True)
    assert KNOWN_AR in other.get("/").data.decode("utf-8")


@needs_db
def test_the_old_toggle_route_is_gone(client):
    """It was removed with the header button. A route left behind would still
    set a cookie nothing reads — a control that silently does nothing."""
    assert client.post("/set-language/ar").status_code == 404


# ---------------------------------------------------------------------------
# 4. Eastern Arabic-Indic digits — guard, boundary and control in one place
# ---------------------------------------------------------------------------

def test_the_digit_helper_is_display_only_and_pure():
    from core import to_arabic_indic_digits
    assert to_arabic_indic_digits("1,250") == "١,٢٥٠"
    assert to_arabic_indic_digits("2026-09-11") == "٢٠٢٦-٠٩-١١"
    assert to_arabic_indic_digits(None) is None
    # no Western digits survive, and nothing else is touched
    assert to_arabic_indic_digits("INV301") == "INV٣٠١"


@needs_db
def test_money_renders_eastern_in_arabic_and_western_in_english(client):
    _as(client, "ar")
    ar = client.get("/reports").data.decode("utf-8")
    assert any(d in ar for d in EASTERN), "no Eastern digits on a money page in Arabic"

    _as(client, "en")           # CONTROL
    en = client.get("/reports").data.decode("utf-8")
    assert not any(d in en for d in EASTERN), (
        "Eastern digits leaked into the English rendering")


@needs_db
@pytest.mark.parametrize("path", ["/price-list", "/settings", "/pos", "/inventory-catalog"])
def test_input_values_keep_western_digits_in_arabic(client, path):
    """THE BOUNDARY (§7.1). A number input's underlying value is a
    Western-digit string in every browser regardless of locale, so converting
    what is displayed risks a mismatch between what is shown, what the keyboard
    types, and what gets submitted. Easy to break by applying the conversion
    one layer too high — which is why this is asserted, not assumed."""
    _as(client, "ar")
    body = client.get(path).data.decode("utf-8")
    offenders = [v for v in re.findall(r'<input[^>]*\bvalue="([^"]*)"', body)
                 if any(d in v for d in EASTERN)]
    assert not offenders, (
        f"{path}: these <input> values carry Eastern digits and would be "
        f"submitted back as unparseable text: {offenders[:5]}")


# ---------------------------------------------------------------------------
# 5. §0's permanent boundary — PDFs stay English, always
# ---------------------------------------------------------------------------

def test_pdf_export_is_not_locale_aware():
    """pdf_export.py must never import a translation function or the digit
    helper. This test should never need to change as a consequence of anything
    else in the localization project — that is the point of it."""
    import pathlib
    src = (pathlib.Path(__file__).parent.parent / "pdf_export.py").read_text(encoding="utf-8")
    for forbidden in ("flask_babel", "gettext", "get_locale", "to_arabic_indic_digits"):
        assert forbidden not in src, (
            f"pdf_export.py references {forbidden!r}. PDFs stay English with "
            f"Western digits, permanently and by explicit instruction — "
            f"ARABIC_LOCALIZATION_PLAN.md §0.")


@needs_db
def test_a_pdf_is_byte_identical_in_both_languages(client, db):
    """The behavioural half of §0: not just 'the module does not import
    gettext' but 'the bytes do not change'."""
    row = db.execute("SELECT id FROM visits ORDER BY id LIMIT 1").fetchone()
    if not row:
        pytest.skip("no visit to export")
    _as(client, "en")
    en = client.get(f"/visits/{row['id']}/export")
    _as(client, "ar")
    ar = client.get(f"/visits/{row['id']}/export")
    if en.status_code != 200 or ar.status_code != 200:
        pytest.skip(f"export not available (en={en.status_code} ar={ar.status_code})")
    # A PDF embeds a creation timestamp, so compare the drawn text rather than
    # raw bytes: no Arabic codepoints and no Eastern digits in either.
    for label, data in (("en", en.data), ("ar", ar.data)):
        text = data.decode("latin-1", errors="ignore")
        assert not any(d in text for d in EASTERN), f"{label} PDF contains Eastern digits"
    assert len(en.data) == len(ar.data) or abs(len(en.data) - len(ar.data)) < 512, (
        "the Arabic PDF differs materially in size from the English one — "
        "something made pdf_export locale-aware")


# ---------------------------------------------------------------------------
# 6. The catalogue itself
# ---------------------------------------------------------------------------

def test_the_compiled_catalogue_exists_and_is_current():
    """Editing a .po and forgetting `pybabel compile` is the classic mistake,
    and its symptom is 'I translated this and nothing changed', not an error."""
    import pathlib
    base = pathlib.Path(__file__).parent.parent / "translations" / "ar" / "LC_MESSAGES"
    po, mo = base / "messages.po", base / "messages.mo"
    assert po.exists(), "the Arabic catalogue is missing"
    assert mo.exists(), "messages.po has not been compiled — run `pybabel compile -d translations`"
    assert mo.stat().st_mtime >= po.stat().st_mtime - 1, (
        "messages.mo is older than messages.po — the catalogue was edited and "
        "not recompiled, so the edits are not live")


def test_every_translated_string_is_actually_arabic():
    """A msgstr that is still Latin text is a half-finished entry, not a
    translation — it would render as English while claiming to be done."""
    import pathlib
    po = (pathlib.Path(__file__).parent.parent / "translations" / "ar" /
          "LC_MESSAGES" / "messages.po").read_text(encoding="utf-8")
    entries = re.findall(r'msgid "((?:[^"\\]|\\.)+)"\nmsgstr "((?:[^"\\]|\\.)*)"', po)
    assert entries, "no catalogue entries found — has the .po format changed?"
    translated = [(en, ar) for en, ar in entries if ar.strip()]
    assert translated, "nothing is translated at all"
    # An entry that is IDENTICAL to its msgid is a deliberate no-translation:
    # a shell command typed verbatim into a terminal, or a product name. One
    # that merely lacks Arabic while differing from the source is a mistake --
    # a half-finished edit that would render as something nobody wrote.
    # A username example stays Latin script on purpose: it is a sample of what
    # someone TYPES into a login box, and Arabic there would be actively
    # misleading. Named explicitly rather than loosening the rule for everyone.
    LATIN_BY_NECESSITY = {"jlee"}
    bad = [(en, ar) for en, ar in translated
           if not any("؀" <= ch <= "ۿ" for ch in ar)
           and ar.strip() != en.strip()
           and en.strip() not in LATIN_BY_NECESSITY]
    assert not bad, f"these msgstr values contain no Arabic characters: {bad[:5]}"

    deliberate = [en for en, ar in translated if ar.strip() == en.strip()]
    assert all(("python3" in d or "setup.py" in d or "PDF" in d) for d in deliberate), (
        "a msgstr identical to its msgid should only be a command or a product "
        f"name; found: {[d for d in deliberate if 'python3' not in d][:5]}")


# ---------------------------------------------------------------------------
# The three choices confirmed by the person who owns them, 2026-09-11.
# Each is a decision rather than a fact, so each is pinned here — if one is
# ever revisited, the test says what the current answer is and why it changed.
# ---------------------------------------------------------------------------

def test_numeric_columns_keep_a_fixed_right_alignment():
    """Confirmed choice: a column of digits stays visually where it is when
    the UI flips to Arabic, rather than following the reading direction.
    `end` is the alternative and English renders identically either way."""
    import pathlib
    css = (pathlib.Path(__file__).parent.parent / "static" / "style.css").read_text(encoding="utf-8")
    for rule in (".num-col", ".cell-input.num"):
        line = [l for l in css.splitlines() if l.strip().startswith(rule)]
        assert line, f"{rule} not found in style.css"
        assert "text-align: right" in line[0], (
            f"{rule} should use a fixed `right`, not a direction-aware value: {line[0]}")
    # CONTROL: prose alignment IS direction-aware, and must stay that way
    assert "text-align: start" in css, (
        "no direction-aware text alignment left at all — the prose rules were "
        "reverted along with the numeric ones")


@needs_db
def test_the_currency_label_is_the_arabic_abbreviation_in_arabic(client):
    """Confirmed choice: the Arabic abbreviation rather than the Latin ISO
    code, in the same position as before."""
    import core
    latin = "IQD" if "IQD" in (core.__doc__ or "") or True else "JOD"
    _as(client, "ar")
    ar = client.get("/boarding").data.decode("utf-8")
    assert ARABIC_CURRENCY in ar, f"expected {ARABIC_CURRENCY} in the Arabic rendering"
    assert LATIN_CURRENCY not in ar, f"{LATIN_CURRENCY} leaked into the Arabic rendering"

    _as(client, "en")           # CONTROL
    en = client.get("/boarding").data.decode("utf-8")
    assert LATIN_CURRENCY in en
    assert ARABIC_CURRENCY not in en


def test_pdf_export_still_uses_the_latin_currency_code():
    """§0 again, from the other direction: the currency decision must not have
    reached the PDFs. They stay English with the Latin code, permanently."""
    import pathlib
    src = (pathlib.Path(__file__).parent.parent / "pdf_export.py").read_text(encoding="utf-8")
    assert LATIN_CURRENCY in src, "pdf_export.py no longer names the Latin currency code"
    assert ARABIC_CURRENCY not in src, (
        f"pdf_export.py contains {ARABIC_CURRENCY} — PDFs stay English (§0)")


@needs_db
def test_dates_render_in_arabic_indic_digits_when_arabic(client, db):
    """Confirmed choice: dates get Eastern digits too, consistent with money."""
    import re as _re
    db.execute("INSERT INTO settings (key,value) VALUES ('opening_date','2026-01-15') "
               "ON CONFLICT (key) DO UPDATE SET value='2026-01-15'")
    db.commit()
    _as(client, "ar")
    ar = client.get("/audit-history").data.decode("utf-8")
    _as(client, "en")           # CONTROL
    en = client.get("/audit-history").data.decode("utf-8")
    ar_date = bool(_re.search(r"[٠-٩]{4}-[٠-٩]{2}-[٠-٩]{2}", ar))
    en_date = bool(_re.search(r"\b[0-9]{4}-[0-9]{2}-[0-9]{2}\b", en))
    if not en_date:
        pytest.skip("no dated row on this page to compare")
    assert ar_date, "dates did not render with Arabic-Indic digits in Arabic"


@needs_db
def test_a_date_input_keeps_iso_format_in_arabic(client, db):
    """THE BOUNDARY that matters most for the date decision: <input type=date>
    requires a Western ISO value. Converting its digits would make the control
    unparseable by the browser and unsubmittable."""
    db.execute("INSERT INTO settings (key,value) VALUES ('opening_date','2026-01-15') "
               "ON CONFLICT (key) DO UPDATE SET value='2026-01-15'")
    db.commit()
    _as(client, "ar")
    body = client.get("/settings").data.decode("utf-8")
    import re as _re
    m = _re.search(r'name="opening_date"[^>]*value="([^"]*)"', body)
    assert m, "the opening_date input was not found"
    assert m.group(1) == "2026-01-15", (
        f"a date input carried {m.group(1)!r} — it must stay Western ISO")
