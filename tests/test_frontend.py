"""
Frontend regression tests — VetClinicSystem JO.

**What these do and do not do.** They are static analysis of `style.css`
and `templates/`, not a browser. They cannot tell you a page *looks*
right — nothing short of screenshot comparison can. What they do is catch
the specific classes of breakage that have actually happened in this
codebase, each of which was previously found only by a person noticing
it:

  - a colour hardcoded instead of taken from the palette, so it stays
    wrong in dark mode and in the ChamPet palette;
  - a `var(--token)` that no longer resolves, which renders as nothing
    at all rather than as an obviously wrong colour;
  - a token defined only inside a theme block, so one theme silently
    falls back to another theme's value;
  - a layout guard being removed — the `min-width: 0` flex/grid traps
    that produced the over-narrow modal and the overflowing table;
  - an asset reference pointing at a file that isn't there.

They run in milliseconds, need no browser, no database and no running
app. See the equivalent file in IQ — the two are close but not identical,
because IQ has four palette/theme combinations to keep in step and JO has
two (light and dark). Several of the guards below exist *because* the bug
happened in JO first.
"""
import re
import pathlib

import pytest


ROOT = pathlib.Path(__file__).parent.parent
CSS_PATH = ROOT / "static" / "style.css"
TEMPLATES = sorted((ROOT / "templates").glob("*.html"))

# Pure white and pure black are structural, not palette choices — white
# label text on a coloured button stays white in every theme. Anything
# else with a hex value belongs in a token.
STRUCTURAL_COLOURS = {"#fff", "#ffffff", "#000", "#000000", "#fff0", "#0000"}

# Partials and the base layout itself do not extend anything.
STANDALONE_TEMPLATES = {"base.html", "_visit_fields.html",
                        "_error_dog.html", "_pagination.html"}


@pytest.fixture(scope="module")
def css():
    return CSS_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def css_no_comments():
    """Block comments blanked out but line numbering preserved, so a hex
    value mentioned in prose doesn't read as a hardcoded colour."""
    src = CSS_PATH.read_text(encoding="utf-8")
    return re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group()), src, flags=re.S)


def _tokens_in(block_body):
    return set(re.findall(r"(--[\w-]+)\s*:", block_body))


def _block_bodies(css_text, header_pattern):
    """Maps selector -> token set, for each block whose header matches."""
    out = {}
    for m in re.finditer(header_pattern, css_text):
        body = css_text[m.end():css_text.index("}", m.end())]
        out[m.group(0).rstrip("{").strip()] = _tokens_in(body)
    return out


def _bare_root_tokens(css_text):
    """Every plain `:root {` block — there is more than one — excluding
    the themed `html[data-theme=...]` overrides."""
    toks = set()
    for m in re.finditer(r"(?m)^:root\s*\{", css_text):
        toks |= _tokens_in(css_text[m.end():css_text.index("}", m.end())])
    return toks


# ---------------------------------------------------------------------------
# Palette integrity — the bug class that actually happened (COMPARISON.md §8)
# ---------------------------------------------------------------------------

def test_no_hardcoded_colours_outside_the_palette(css_no_comments):
    """This app once shipped a Settings restore button in a colour that
    existed nowhere in its palette. A hardcoded hex looks fine in the theme it was
    picked in and is wrong in every other one — and nothing reports it,
    because it is valid CSS."""
    offenders = []
    for lineno, line in enumerate(css_no_comments.splitlines(), 1):
        for m in re.finditer(r"#[0-9a-fA-F]{3,8}\b", line):
            if m.group().lower() in STRUCTURAL_COLOURS:
                continue
            # A hex on a `--token: ...` line *is* the palette definition.
            if re.search(r"--[\w-]+\s*:[^;]*$", line[:m.start()]):
                continue
            offenders.append(f"line {lineno}: {m.group()} in {line.strip()[:70]}")
    assert not offenders, (
        "Hardcoded colour(s) outside a palette token — put the value in a "
        "token in :root and reference it with var():\n  " + "\n  ".join(offenders))


def test_every_var_reference_resolves(css):
    """A typo'd or renamed token doesn't error — the property is simply
    dropped, so the element renders with no colour at all. Caught here
    instead of by someone noticing an invisible button."""
    defined = set(re.findall(r"(--[\w-]+)\s*:", css))
    used = set(re.findall(r"var\((--[\w-]+)", css))
    assert not (used - defined), f"var() references with no definition: {sorted(used - defined)}"


def test_no_token_is_defined_only_inside_a_theme_block(css):
    """A token that exists only in the dark block has no light value, so
    light mode falls back to whatever it inherits — usually invisible.
    Every token must have a base value in a bare :root."""
    bare = _bare_root_tokens(css)
    overrides = _block_bodies(css, r'(?m)^html\[data-(?:theme|palette)[^{]*\{')
    theme_only = set().union(*overrides.values()) - bare if overrides else set()
    assert not theme_only, f"tokens with no base :root value: {sorted(theme_only)}"


def test_dark_theme_covers_the_palette_it_overrides():
    """JO has one override block (dark). Every token it sets must exist in
    the base palette, and it must cover enough of it to be a real theme
    rather than a partial one that leaves light-mode colours showing through
    on a dark background — the contrast bug in COMPARISON.md §12."""
    css_text = CSS_PATH.read_text(encoding="utf-8")
    bare = _bare_root_tokens(css_text)
    overrides = _block_bodies(css_text, r'(?m)^html\[data-theme[^{]*\{')
    dark = max(overrides.values(), key=len) if overrides else set()
    assert dark, "no dark-theme token block found"
    assert dark <= bare, f"dark theme defines tokens absent from :root: {sorted(dark - bare)}"
    # Colour tokens specifically — spacing/radius tokens rightly do not vary.
    colourish = {t for t in bare if any(k in t for k in
                 ("bg", "ink", "line", "primary", "accent", "danger", "warn", "ok", "sidebar"))}
    uncovered = colourish - dark
    assert not uncovered, f"colour tokens with no dark-mode value: {sorted(uncovered)}"


def test_css_braces_are_balanced(css):
    """A stray brace silently kills every rule after it — the page keeps
    rendering, just unstyled from that point down."""
    assert css.count("{") == css.count("}"), (
        f"unbalanced braces: {css.count('{')} open, {css.count('}')} close")


# ---------------------------------------------------------------------------
# Layout guards — each one is a fix that a later edit could silently undo
# ---------------------------------------------------------------------------

def test_modal_box_keeps_its_min_width_reset(css):
    """COMPARISON.md §19. A flex item defaults to `min-width: auto`, which
    overrides `max-width` and lets the box size to its widest child — the
    over-narrow/over-wide modal bug. Removing this line brings it straight
    back, and nothing else in the file compensates."""
    body = re.search(r"\.modal-box\s*\{([^}]*)\}", css)
    assert body, ".modal-box rule not found"
    assert re.search(r"min-width:\s*0", body.group(1)), (
        ".modal-box lost `min-width: 0` — the flex trap that caused the modal "
        "width bug will reappear")


def test_main_column_keeps_its_min_width_reset(css):
    """Same trap, grid edition: without it the main column refuses to shrink
    and a wide table pushes the whole page into horizontal scroll."""
    body = re.search(r"\.main\s*\{([^}]*)\}", css)
    assert body, ".main rule not found"
    assert re.search(r"min-width:\s*0", body.group(1)), ".main lost `min-width: 0`"


def test_keyboard_focus_is_visible(css):
    """Without this, the app cannot be used without a mouse — you cannot see
    what you have selected. JO shipped with none of it until v1.8.0; this
    guard is the reason it cannot quietly disappear again."""
    assert "focus-visible" in css, "no :focus-visible styling at all"
    focus_rules = [r for r in re.findall(r"([^{}]*focus-visible[^{}]*)\{([^}]*)\}", css)]
    assert focus_rules, ":focus-visible present but not as a real rule"
    selectors = " ".join(sel for sel, _ in focus_rules)
    for essential in ("a:focus-visible", "button:focus-visible", "input:focus-visible"):
        assert essential in selectors, f"{essential} is not covered"
    outlines = " ".join(body for _, body in focus_rules)
    assert "outline" in outlines, ":focus-visible rules set no outline"


def test_folder_browser_rows_are_styled(css):
    """Shipped unstyled here until v1.8.0 — a wall of undifferentiated text
    in the backup-folder picker. Cheap to lose again in a CSS tidy-up."""
    assert "folder-browser-row" in css, "folder browser rows have no styling"


# ---------------------------------------------------------------------------
# Touch / mobile — the pass in COMPARISON.md §16, easy to erode
# ---------------------------------------------------------------------------

def test_mobile_breakpoints_still_exist(css):
    assert len(re.findall(r"@media", css)) >= 3, "responsive breakpoints have been removed"


def test_touch_targets_meet_the_minimum_size(css):
    """44px is the accessibility floor for a touch target. These were added
    deliberately; a later 'tidy' that drops them is silent on desktop and
    only shows up on a tablet at the front desk."""
    assert len(re.findall(r"44px", css)) >= 4, (
        "the 44px touch-target minimums have been reduced or removed")


def test_mobile_inputs_are_16px_to_stop_ios_zooming(css):
    """iOS Safari zooms the whole page when a focused input's font is under
    16px. The fix is invisible on every desktop browser, which is exactly
    why it gets removed by accident."""
    assert re.search(r"font-size:\s*16px", css), (
        "the 16px mobile input size is gone — iOS will zoom on focus again")


# ---------------------------------------------------------------------------
# Assets and templates
# ---------------------------------------------------------------------------

def test_every_static_reference_points_at_a_real_file():
    """A renamed asset leaves a broken reference that only shows up as a
    missing image or an unstyled page in the browser."""
    missing = []
    for template in TEMPLATES:
        for ref in re.findall(r"url_for\('static',\s*filename='([^']+)'", template.read_text()):
            if "{{" in ref or "~" in ref:
                continue  # dynamically built (palette-branched); checked at runtime
            if not (ROOT / "static" / ref).exists():
                missing.append(f"{template.name} -> static/{ref}")
    assert not missing, "template(s) reference missing static files:\n  " + "\n  ".join(missing)


def test_no_external_resources_are_loaded():
    """The app must keep working on a clinic network with no internet, and
    keep working in ten years. Anchors to wa.me are fine — those are the
    user choosing to navigate. A stylesheet, script or image from a CDN is
    not: it makes the UI depend on someone else's uptime."""
    offenders = []
    for template in TEMPLATES:
        text = template.read_text()
        for tag, attr in (("script", "src"), ("link", "href"), ("img", "src")):
            for m in re.finditer(rf"<{tag}\b[^>]*{attr}=\"(https?://[^\"]+)\"", text):
                offenders.append(f"{template.name}: <{tag}> {m.group(1)[:60]}")
    assert not offenders, "external resource load(s):\n  " + "\n  ".join(offenders)


def test_page_templates_extend_the_base_layout():
    """A page that forgets to extend base.html renders with no navigation,
    no stylesheet and no theme — obvious once opened, invisible in review."""
    orphans = [t.name for t in TEMPLATES
               if t.name not in STANDALONE_TEMPLATES and "extends" not in t.read_text()]
    assert not orphans, f"template(s) not extending a base layout: {orphans}"


def test_templates_are_not_empty():
    empty = [t.name for t in TEMPLATES if not t.read_text().strip()]
    assert not empty, f"empty template(s): {empty}"
