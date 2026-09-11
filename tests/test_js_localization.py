# -*- coding: utf-8 -*-
"""Two rules for translated text inside an inline <script>.

1. It must go through `|tojson`. Jinja autoescapes for HTML, not for
   JavaScript, so `'{{ _("...") }}'` in bare quotes turns an apostrophe in a
   translation into `&#39;` — the script still parses, the button still works,
   and the user reads "Cart isn&#39;t empty." on screen. That is worse than a
   crash, because nothing reports it. `|tojson` emits the quotes itself and
   escapes the contents for JS.

2. A msgid must hold prose, not markup. The first JS pass wrapped whole
   fragments — `<div class="empty-note">No matches.</div>` became one msgid —
   which asks a translator to reproduce HTML correctly in every language and
   renders as literal text the moment they do not.

Syntax is covered live by scripts/simulation/check_rendered_js.py, which
parses every inline script of every rendered page with node, in both
languages. These two are the static half, because both failures render a
perfectly valid page.
"""
import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.S)
GETTEXT = re.compile(r"\{\{\s*_\(.*?\)\s*((?:\|\s*\w+\s*)*)\}\}", re.S)


def _script_bodies():
    for f in sorted(TEMPLATES.rglob("*.html")):
        src = f.read_text(encoding="utf-8")
        for m in SCRIPT.finditer(src):
            if "src=" in m.group(1):
                continue
            yield f, src, m


def test_gettext_in_script_goes_through_tojson():
    offenders = []
    for f, src, m in _script_bodies():
        for g in GETTEXT.finditer(m.group(2)):
            if "tojson" in g.group(1):
                continue
            line = (src[:m.start(2)].count("\n") + 1
                    + m.group(2)[:g.start()].count("\n"))
            offenders.append(f"{f.name}:{line}: {g.group(0)[:70]}")
    assert not offenders, (
        "Translated text inside <script> must use |tojson:\n  " + "\n  ".join(offenders))


def test_no_msgid_contains_markup():
    offenders = []
    for f in sorted(TEMPLATES.rglob("*.html")):
        src = f.read_text(encoding="utf-8")
        for m in re.finditer(r"_\(\s*(['\"])(.*?)\1", src, re.S):
            text = m.group(2)
            if re.search(r"<\s*/?\s*[a-zA-Z]", text):
                line = src[:m.start()].count("\n") + 1
                offenders.append(f"{f.name}:{line}: {text[:70]}")
    assert not offenders, (
        "These msgids contain markup — keep the tags in the template and "
        "translate only the prose:\n  " + "\n  ".join(offenders))


def test_control_the_shapes_we_actually_ship_are_accepted():
    """Without this, both guards above pass by matching nothing at all."""
    good_js = "<script nonce=\"x\">alert({{ _('Cart is empty.')|tojson }});</script>"
    m = SCRIPT.search(good_js)
    g = GETTEXT.search(m.group(2))
    assert g and "tojson" in g.group(1)

    bad_js = "<script nonce=\"x\">alert('{{ _('Cart is empty.') }}');</script>"
    m2 = SCRIPT.search(bad_js)
    g2 = GETTEXT.search(m2.group(2))
    assert g2 and "tojson" not in g2.group(1), "the guard would not see a bare-quoted call"

    assert not re.search(r"<\s*/?\s*[a-zA-Z]", "No matches.")
    assert re.search(r"<\s*/?\s*[a-zA-Z]", '<div class="empty-note">No matches.</div>')


def test_the_suite_actually_sees_some_script_localization():
    """A count, so the two guards cannot pass because the regex stopped
    matching the codebase entirely."""
    total = sum(len(GETTEXT.findall(m.group(2))) for _, _, m in _script_bodies())
    assert total > 40, f"only {total} translated strings found in <script> blocks"
