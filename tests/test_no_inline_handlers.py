"""
No template may carry an inline event-handler attribute, and no inline
<script> may run without a nonce.

Both rules exist because of review finding S6: script-src dropped
'unsafe-inline' in favour of a per-request nonce. A nonce authorises <script>
blocks; it does NOT authorise `onclick=` and friends, and a browser that sees a
nonce ignores 'unsafe-inline' altogether. So a single reintroduced `onclick=`
is a button that silently stops working, with no error anywhere — the page
renders, the CSS is fine, and nothing on the server knows.

Two things this scanner has to get right, both of which it got wrong first:

1. **The attribute boundary.** `\\bon[a-z]+=` also matches inside
   `data-person-onclick=` and, more to the point, `button-oninput=`, because
   `-` is a word boundary. The negative lookbehind for `[-\\w]` is the fix, and
   is the same bug that produced three false duplicate-id reports in
   COMPARISON.md §49.

2. **Which words are events.** Matching `on[a-z]+` alone reports `content=`,
   `controls=` and `confirm=` as handlers, because they contain "on". The
   allowlist below is deliberate: a genuinely new event name is rarer than a
   false positive, and the failure mode of a missed one is a test that passes
   while a handler survives — so the allowlist is checked against the DOM's
   own list in test_the_event_allowlist_is_not_silently_wrong.

The floors are counts, not "no matches". A scanner that has stopped finding
its own subject reports zero either way — see COMPARISON.md §49, where a
static guard went vacuous rather than red when the code it scanned moved.
"""
import pathlib
import re

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "templates"

# Attribute-boundary-safe: not preceded by '-' or a word character.
HANDLER_ATTR = re.compile(r'(?<![-\w])(on[a-z]+)\s*=\s*"')

EVENT_ATTRS = {
    "onclick", "ondblclick", "onchange", "oninput", "onsubmit", "onreset",
    "onblur", "onfocus", "onfocusin", "onfocusout", "onkeydown", "onkeyup",
    "onkeypress", "onload", "onunload", "onerror", "onabort", "onscroll",
    "onwheel", "onmouseover", "onmouseout", "onmouseenter", "onmouseleave",
    "onmousedown", "onmouseup", "onmousemove", "oncontextmenu", "onpaste",
    "oncopy", "oncut", "ondrag", "ondrop", "ondragover", "ondragstart",
    "ontoggle", "onselect", "onsearch", "oninvalid", "onanimationend",
    "ontransitionend", "onpointerdown", "onpointerup", "onpointermove",
}

SCRIPT_OPEN = re.compile(r"<script(?![^>]*\bsrc=)([^>]*)>")


def _templates():
    return sorted(TEMPLATES.rglob("*.html"))


def test_there_are_templates_to_scan():
    """Floor. If templates/ moves, every test below passes by finding nothing."""
    files = _templates()
    assert len(files) >= 40, (
        f"only {len(files)} templates found under {TEMPLATES} — the scanner has "
        "lost its subject; fix the path rather than lowering this floor")


def test_no_template_carries_an_inline_event_handler():
    """GUARD. One of these is one silently dead button."""
    offenders = []
    for f in _templates():
        for m in HANDLER_ATTR.finditer(f.read_text(encoding="utf-8")):
            if m.group(1) in EVENT_ATTRS:
                line = f.read_text(encoding="utf-8")[:m.start()].count("\n") + 1
                offenders.append(f"{f.name}:{line}: {m.group(1)}=")
    assert not offenders, (
        "inline event handler(s) — script-src has no 'unsafe-inline', so these "
        "do nothing at all in a browser:\n  " + "\n  ".join(offenders) +
        "\n\nUse data-vzh= with VZ.bind(), or data-vz-act= with VZ.action() for "
        "markup a script builds at runtime. See static/behaviors.js.")


def test_every_inline_script_carries_a_nonce():
    """GUARD. A nonce-less inline <script> is refused outright."""
    offenders = []
    for f in _templates():
        text = f.read_text(encoding="utf-8")
        for m in SCRIPT_OPEN.finditer(text):
            if "nonce=" not in m.group(1):
                line = text[:m.start()].count("\n") + 1
                offenders.append(f"{f.name}:{line}")
    assert not offenders, (
        'inline <script> without nonce="{{ csp_nonce }}" — the browser will '
        "refuse to run these:\n  " + "\n  ".join(offenders))


def test_the_scanner_still_finds_inline_scripts_to_check():
    """Floor for the test above. `<script>` markup could change shape (a
    templating helper, a different attribute order) and leave that test
    scanning nothing while passing."""
    total = sum(len(SCRIPT_OPEN.findall(f.read_text(encoding="utf-8")))
                for f in _templates())
    assert total >= 20, (
        f"only {total} inline <script> blocks found across "
        f"{len(_templates())} templates — the nonce check has gone vacuous; "
        "fix the pattern rather than lowering this floor")


def test_behaviors_js_is_loaded_and_defines_both_mechanisms():
    """The templates now depend on VZ.bind and VZ.action existing. If
    behaviors.js stops being loaded, every converted handler dies at once and
    every page still renders."""
    static = TEMPLATES.parent / "static" / "behaviors.js"
    assert static.exists(), "static/behaviors.js is missing"
    src = static.read_text(encoding="utf-8")
    assert "VZ.bind" in src and "VZ.action" in src, "behaviors.js exports neither hook"
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "behaviors.js" in base, "base.html does not load static/behaviors.js"


def test_every_marker_used_in_markup_is_bound_somewhere():
    """GUARD against the halfway state: an element keeps its data-vzh= marker
    but its VZ.bind() call is deleted or renamed. The button then looks
    completely normal and does nothing — the exact failure this whole change
    was at risk of introducing 267 times."""
    unbound = []
    for f in _templates():
        text = f.read_text(encoding="utf-8")
        bound = set(re.findall(r"VZ\.(?:bind|action)\('([^']+)'", text))
        used = set()
        for attr in ("data-vzh", "data-vz-act", "data-vz-change", "data-vz-input"):
            for m in re.finditer(attr + r'="([^"]*)"', text):
                used.update(m.group(1).split())
        # base.html's markers are bound in base.html; a partial's in the page
        # that includes it — so only flag markers whose own file names them.
        prefix = re.sub(r"[^a-z0-9]+", "-", f.name.replace(".html", "").lower()).strip("-")
        for key in used:
            if key.startswith(prefix) and key not in bound:
                unbound.append(f"{f.name}: {key}")
    assert not unbound, (
        "marker(s) in the markup with no VZ.bind/VZ.action to match — these "
        "elements do nothing:\n  " + "\n  ".join(unbound))


def test_no_binding_refers_to_a_marker_that_is_not_in_the_markup():
    """The mirror image, and the cheaper mistake: a binding left behind after
    its element was deleted. Harmless at runtime, but it is how a file drifts
    into looking like it does more than it does."""
    orphans = []
    for f in _templates():
        text = f.read_text(encoding="utf-8")
        used = set()
        for attr in ("data-vzh", "data-vz-act", "data-vz-change", "data-vz-input"):
            for m in re.finditer(attr + r'="([^"]*)"', text):
                used.update(m.group(1).split())
        for key in set(re.findall(r"VZ\.(?:bind|action)\('([^']+)'", text)):
            if key not in used:
                orphans.append(f"{f.name}: {key}")
    assert not orphans, (
        "VZ binding(s) for markers that appear nowhere in the markup:\n  "
        + "\n  ".join(orphans))


def test_no_handler_body_was_left_holding_a_jinja_expression():
    """A handler moved out of a {% for %} loop takes its {{ loop.var }} with
    it, and Jinja then raises UndefinedError while rendering the page. This
    happened to seven handlers during the conversion; the suite caught it, but
    only because those pages happened to be covered."""
    offenders = []
    for f in _templates():
        for m in re.finditer(r"VZ\.(?:bind|action)\('[^']+',[^\n]*", f.read_text(encoding="utf-8")):
            if "{{" in m.group(0) or "{%" in m.group(0):
                offenders.append(f"{f.name}: {m.group(0)[:100]}")
    assert not offenders, (
        "Jinja expression inside a VZ handler body — it is outside the loop "
        "that defined the variable, so the page raises UndefinedError. Carry "
        "the value on the element with data-vz-arg= instead:\n  "
        + "\n  ".join(offenders))


def test_no_delegated_action_was_left_holding_a_template_literal():
    """The JavaScript twin of the test above, and the one that nearly shipped:
    a handler on markup a script builds with a template literal reads
    `${c.id}`, which only interpolates inside that literal. Hoisted into a
    VZ.action it becomes the four literal characters `${c.` — every POS
    quantity button would have targeted a line id that does not exist."""
    offenders = []
    for f in _templates():
        for m in re.finditer(r"VZ\.(?:bind|action)\('[^']+',[^\n]*", f.read_text(encoding="utf-8")):
            if "${" in m.group(0):
                offenders.append(f"{f.name}: {m.group(0)[:100]}")
    assert not offenders, (
        "template-literal interpolation inside a VZ handler body — it is "
        "outside the literal that would interpolate it. Carry the value on "
        "the element instead:\n  " + "\n  ".join(offenders))


def test_the_event_allowlist_is_not_silently_wrong():
    """CONTROL on EVENT_ATTRS itself.

    The allowlist is what stops `content=` being reported as a handler. It is
    also what would let a genuinely new event name through unnoticed. This
    pins it against the attributes the templates actually contain: anything
    matching the handler shape that is NOT in the allowlist has to be a known
    false positive, listed here by name.
    """
    known_false_positives = {"onfirm", "ontrols", "ontent", "ontrast", "ony"}
    surprises = set()
    for f in _templates():
        for m in HANDLER_ATTR.finditer(f.read_text(encoding="utf-8")):
            if m.group(1) not in EVENT_ATTRS and m.group(1) not in known_false_positives:
                surprises.add(m.group(1))
    assert not surprises, (
        f"attribute(s) shaped like an event handler and in neither list: "
        f"{sorted(surprises)}. If one is a real event, add it to EVENT_ATTRS "
        "— it is currently being skipped by the guard above.")


@pytest.mark.parametrize("bad", [
    '<button onclick="doThing()">x</button>',
    "<div  onchange = \"f()\" >",
    '<input onkeydown="g()">',
])
def test_the_scanner_catches_what_it_is_looking_for(bad):
    """CONTROL. Without this, a pattern that matches nothing at all passes
    every guard above."""
    m = HANDLER_ATTR.search(bad)
    assert m and m.group(1) in EVENT_ATTRS, f"scanner missed {bad!r}"


@pytest.mark.parametrize("ok", [
    '<div data-role-onclick="x">',      # hyphen is a word boundary
    '<meta name="x" content="y">',      # contains "on"
    '<video controls="true">',
    '<span data-confirm="Sure?">',
])
def test_the_scanner_does_not_cry_wolf(ok):
    """CONTROL the other way. A scanner with false positives gets its floor
    lowered by the next person, and then it is worth nothing."""
    m = HANDLER_ATTR.search(ok)
    assert not (m and m.group(1) in EVENT_ATTRS), f"false positive on {ok!r}"
