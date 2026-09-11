# -*- coding: utf-8 -*-
"""A translated string must never sit in an INLINE box pinned to one line.

`white-space: nowrap` was fine for the single English word "Grooming" and
wrong the moment it became four Arabic words. The badge is an inline-block in
a table cell that shrinks with the table: its BOX shrank, its text could not,
so the text rendered outside its own box and over the neighbouring column.
Measured at 28px of spill in a 900px-wide table and 50px at 700px.

**`<td style="white-space:nowrap">` is deliberately NOT flagged**, and that
distinction is measured rather than assumed: a table cell is sized by the
table layout algorithm to fit its content, so it grows instead of clipping.
Checked in Arabic at 900px across /price-list, /pos/history and /distributors
— four such cells, zero overflow. Keeping a row of action buttons on one line
is a legitimate use; pinning a word you are going to translate is not.

Static rather than a browser check because reproducing it needs the right
language AND the right column width at the same time, while the rule itself
is simple and checkable everywhere at once.
"""
import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"

# Inline-level elements only — see the module docstring for why <td> is out.
INLINE = r"span|a|button|em|strong|small|code|label|b|i"
NOWRAP_INLINE = re.compile(
    r"<(" + INLINE + r")\b[^>]*style=\"[^\"]*white-space:\s*nowrap[^\"]*\"[^>]*>(.*?)</\1>",
    re.S)


def _offenders():
    out = []
    for f in sorted(TEMPLATES.rglob("*.html")):
        src = f.read_text(encoding="utf-8")
        for m in NOWRAP_INLINE.finditer(src):
            body = m.group(2)
            if "_(" in body or "|tr" in body:
                line = src[:m.start()].count("\n") + 1
                out.append(f"{f.name}:{line}: {body.strip()[:60]}")
    return out


def test_no_translated_text_is_pinned_to_one_line():
    offenders = _offenders()
    assert not offenders, (
        "These inline elements force translated text onto one line. A "
        "translation is usually longer than its English source, and the text "
        "will render outside its own box and over whatever is beside it:\n  "
        + "\n  ".join(offenders))


def test_control_the_scanner_recognises_the_shape_it_guards():
    """Without this the guard passes hardest when its regex matches nothing."""
    bad = '<span class="badge" style="white-space:nowrap;">{{ _(\'Grooming\') }}</span>'
    m = NOWRAP_INLINE.search(bad)
    assert m and "_(" in m.group(2), "the guard would not have caught the original bug"

    value = '<span class="badge" style="white-space:nowrap;">{{ v.id }}</span>'
    m2 = NOWRAP_INLINE.search(value)
    assert m2 and "_(" not in m2.group(2), "an untranslated value should be allowed"

    cell = '<td style="white-space:nowrap;">{{ _(\'Edit\') }}</td>'
    assert not NOWRAP_INLINE.search(cell), (
        "a <td> grows to fit its content and must not be flagged")
