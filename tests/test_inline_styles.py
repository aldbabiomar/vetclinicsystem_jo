"""
A ratchet on inline `style=` attributes (review finding M8).

M8 is explicitly not a big-bang sweep: ~490 attributes per app, and converting
them mechanically is unsafe in ways that do not show up in any other test.
Three ways it went wrong when it was first attempted here, all found by
diffing computed styles in a real browser before and after:

  * A page script reveals a panel with `el.style.display = ''`. That only
    unhides the element while the inline style is the ONLY thing hiding it.
    Moved to a class, the Settings Updates panel would never have appeared
    again — and nothing would have raised an error.
  * `.field label` is specificity 0,0,1,1 and outranks a single utility class.
    The inline style it replaced was the only thing winning, so a label
    quietly went from font-weight 500 to 620.
  * `margin:0 0 8px` is not `margin-bottom:8px` — it also zeroes the top
    margin, and a <p>'s default came back.

So the rule is: convert opportunistically, when you are editing a template
anyway, and never in bulk. This file holds the line rather than the sweep — it
fails if a file gets *worse*, and tells you to lower its own number when you
make one better.
"""
import pathlib
import re

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "templates"
STYLE_ATTR = re.compile(r'(?<![-\w])style\s*=\s*"([^"]*)"')

# Converted deliberately as M8's first pass. What is left in each is entirely
# `display:` toggles — the one category that CANNOT move to a class, because
# the page scripts reveal these elements by clearing the inline style. Anything
# else reappearing in these two files is a regression.
#
# The counts differ between IQ and JO because the templates do: JO's Inventory
# Catalog has two fewer script-toggled panels. Do not sync these numbers.
CONVERTED = {"settings.html": 8, "inventory_catalog.html": 6}

# Everything else, capped at what it was when this ratchet was written. Lower
# a number when you convert a file; never raise one.
CEILING = 380


def _static_styles(path):
    """Inline styles that a class could hold — server-computed ones excluded,
    because those genuinely have to stay inline."""
    return [m.group(1) for m in STYLE_ATTR.finditer(path.read_text(encoding="utf-8"))
            if "{{" not in m.group(1) and "{%" not in m.group(1)]


def test_there_are_templates_to_scan():
    """Floor. A scanner that has lost its subject reports zero and passes."""
    assert len(list(TEMPLATES.rglob("*.html"))) >= 40


@pytest.mark.parametrize("name", sorted(CONVERTED))
def test_a_converted_file_stays_converted(name):
    """GUARD. These two are the ones M8 named. Regrowing an inline style here
    means the convention is already drifting back."""
    path = TEMPLATES / name
    assert path.exists(), f"{name} has moved — update this test"
    found = _static_styles(path)
    assert len(found) <= CONVERTED[name], (
        f"{name} has {len(found)} static inline style(s), expected "
        f"{CONVERTED[name]}:\n  " + "\n  ".join(found[:10]) +
        "\n\nMove them into style.css. The utilities are documented at the "
        "foot of that file and in the README.")


def test_the_rest_does_not_get_worse():
    """Ratchet. Not a demand to convert everything — a demand not to add more."""
    total = sum(len(_static_styles(f)) for f in TEMPLATES.rglob("*.html"))
    assert total <= CEILING, (
        f"{total} static inline style= attributes across the templates, above "
        f"the {CEILING} ceiling. Move some into style.css rather than raising "
        "this number.")


def test_the_ratchet_is_close_enough_to_bite():
    """CONTROL on the ceiling itself.

    A ceiling far above the real number is not a ratchet, it is decoration —
    it would let dozens of new inline styles in before it ever failed. If this
    fails, the templates got better: lower CEILING to the reported number.
    """
    total = sum(len(_static_styles(f)) for f in TEMPLATES.rglob("*.html"))
    assert CEILING - total <= 40, (
        f"only {total} static inline styles remain but the ceiling is {CEILING} "
        f"— lower CEILING to {total} so it keeps meaning something")


def test_server_computed_styles_are_not_counted():
    """CONTROL on the scanner. If it counted `style="display:{{ ... }}"` the
    ratchet could never reach zero and would be quietly abandoned."""
    sample = '<div style="display:{{ \'none\' if x }}"><p style="margin:0">'
    found = [m.group(1) for m in STYLE_ATTR.finditer(sample)
             if "{{" not in m.group(1) and "{%" not in m.group(1)]
    assert found == ["margin:0"], f"scanner mis-classified: {found}"


def test_the_scanner_finds_a_plain_inline_style():
    """CONTROL. Without this, a broken pattern makes every test above pass."""
    assert _static_styles.__doc__  # the helper exists
    sample = '<div style="margin-top:10px">'
    assert [m.group(1) for m in STYLE_ATTR.finditer(sample)] == ["margin-top:10px"]


def test_the_scanner_ignores_a_hyphenated_lookalike():
    """CONTROL. `data-chart-style="x"` is not an inline style; `-` is a word
    boundary, which is how a `\\b`-anchored version of this pattern would have
    reported it. Same bug as the duplicate-id scanner in COMPARISON.md §49."""
    assert not STYLE_ATTR.findall('<div data-chart-style="bar">')


# getElementById('x').style.display = <something that can be ''>
_REVEAL = re.compile(
    r"""getElementById\(\s*['"]([\w-]+)['"]\s*\)\s*\.style\.display\s*=\s*([^;\n]+)""")


def _elements_revealed_by_clearing_display(text):
    """Ids whose display a script sets to the empty string, directly or through
    a ternary. Those are the ones that depend on an inline style existing."""
    out = set()
    for m in _REVEAL.finditer(text):
        rhs = m.group(2)
        if "''" in rhs or '""' in rhs:
            out.add(m.group(1))
    return out


STYLESHEET = TEMPLATES.parent / "static" / "style.css"


def _classes_that_hide():
    """Single class names whose rule in style.css sets display:none.

    Comments are stripped first. Without that, the doc comment above a rule is
    swallowed into the selector capture and the rule is silently skipped — the
    first version of this helper found `.u-hidden` (no comment above it) but
    not `.reveal-panel` (comment above it), which made the guard below pass
    against exactly the mutation it exists to catch.
    """
    css = re.sub(r"/\*.*?\*/", "", STYLESHEET.read_text(encoding="utf-8"), flags=re.S)
    hiding = set()
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        if not re.search(r"display\s*:\s*none", m.group(2)):
            continue
        for selector in m.group(1).split(","):
            selector = selector.strip()
            # only a bare single-class selector can hide an element outright
            if re.fullmatch(r"\.[\w-]+", selector):
                hiding.add(selector[1:])
    return hiding


def _classes_on(text, element_id):
    for m in re.finditer(r"<[^<>]*\bid\s*=\s*[\"']" + re.escape(element_id) + r"[\"'][^<>]*>", text):
        cm = re.search(r'(?<![-\w])class\s*=\s*"([^"]*)"', m.group(0))
        if cm:
            return set(cm.group(1).split())
    return set()


def test_no_element_revealed_by_clearing_display_is_hidden_by_a_class():
    """GUARD on the specific breakage that made this whole finding dangerous.

    `el.style.display = ''` removes the INLINE display and nothing else. An
    element hidden by an inline `display:none` becomes visible; an element
    hidden by a CLASS stays hidden for good — no error, no failed request, no
    clue except a panel that never appears. The Settings Updates panel is
    exactly this, and moving its inline style to a class is precisely the
    tempting, obvious-looking edit this finding invites.

    Elements with neither are fine: they are visible by default, and the empty
    string just restores that. An earlier version of this test demanded an
    inline display on every one of them and flagged two long-standing,
    perfectly correct elements — the rule is about classes, not about inline
    styles being present.
    """
    hiding = _classes_that_hide()
    assert hiding, "no display:none classes found in style.css — scanner is broken"
    offenders = []
    for f in TEMPLATES.rglob("*.html"):
        text = f.read_text(encoding="utf-8")
        for element_id in sorted(_elements_revealed_by_clearing_display(text)):
            clash = _classes_on(text, element_id) & hiding
            if clash:
                offenders.append(f"{f.name}: #{element_id} carries {sorted(clash)}")
    assert not offenders, (
        "element(s) a script reveals with style.display = '' but which a CSS "
        "class hides — clearing the inline style cannot unhide them, so they "
        "will never appear:\n  " + "\n  ".join(offenders))


def test_the_reveal_scanner_finds_the_elements_it_is_meant_to():
    """Floor. If the idiom is spelled differently tomorrow this scanner finds
    nothing and the guard above passes while checking zero elements."""
    found = set()
    for f in TEMPLATES.rglob("*.html"):
        found |= _elements_revealed_by_clearing_display(f.read_text(encoding="utf-8"))
    assert len(found) >= 3, (
        f"only {len(found)} script-revealed element(s) found ({sorted(found)}) — "
        "the scanner has lost its subject; fix the pattern rather than "
        "lowering this floor")
