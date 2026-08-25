"""
Every page, in a real browser, checked for the ways a page is *broken*.

Deliberately NOT screenshot comparison. That would mean roughly 1,100
baseline images across both apps once palettes, themes and viewports are
multiplied out, every one of them invalidated by any deliberate change,
each difference needing a human to approve. When that becomes tedious
people approve them in bulk, and the suite quietly stops checking
anything — which is worse than not having it.

What these assert instead is true of any correct page regardless of how it
looks, so nothing here ever needs regenerating or eyeballing:

  - the page does not scroll sideways (the tablet-overflow bug)
  - no JavaScript errors, and no failed asset requests
  - no interactive element rendered at zero size (the collapsed-modal bug)
  - touch targets are big enough on a phone
  - every page still renders in dark mode

They cannot tell you a page looks *ugly*. Only that it is not broken.

NOT HERE: an automated colour-contrast check, deliberately.

Two attempts at one produced 117 and then 142 "failures", essentially all
of them false. Getting it right needs three things at once — skipping
elements that are in the DOM but not on screen (the mobile topbar carries
dark text meant for a light bar), normalising modern `color(srgb r g b / a)`
syntax that a number-scraping regex reads as near-black, and compositing
translucent backgrounds against what is behind them. The sidebar here is
translucent, so its effective background is a blend, and every attempt to
compute it from CSS strings alone got it wrong.

Reading the painted pixel would be accurate, but that means capturing
images at test time, and a check nobody can trust is worse than no check at
all -- it is precisely the theatre this file exists to avoid. Contrast is
instead covered by test_frontend.py, which pins the palette tokens
themselves, and by the measured hand-audit recorded in COMPARISON.md
§13/§14. If this comes back, it should sample rendered pixels rather than
parse stylesheets.

These need Playwright and a running app, and skip cleanly without either,
exactly as the database tests skip without TEST_DATABASE_URL. Playwright is
a test-only dependency and is deliberately absent from requirements.txt —
the app itself has no build step and no browser dependency, which is a
property worth keeping.

Run with:
    APP_URL=http://127.0.0.1:5091 venv/bin/python -m pytest tests/test_browser.py -q
"""
import os

import pytest

playwright_api = pytest.importorskip(
    "playwright.sync_api",
    reason="browser tests need playwright: pip install playwright && playwright install chromium")

APP_URL = os.environ.get("APP_URL")
ADMIN_USER = os.environ.get("APP_USER", "admin")
ADMIN_PASS = os.environ.get("APP_PASS", "Admin12345!")

pytestmark = pytest.mark.skipif(
    not APP_URL,
    reason="no APP_URL — start the app (scripts/isolated_test_env.sh up iq) and set "
           "APP_URL=http://127.0.0.1:5091")

# Phone, tablet, laptop. The clinic works on the last one; the first two are
# where layout breaks without anyone noticing.
VIEWPORTS = [("phone", 390, 844), ("tablet", 768, 1024), ("laptop", 1440, 900)]

# Pages reachable without an id, i.e. everything on the navigation.
PAGES = [
    "/", "/owners", "/patients", "/visits", "/followups", "/wellness", "/grooming",
    "/boarding", "/appointments", "/inpatient", "/pos", "/pos/history", "/price-list",
    "/inventory-status", "/inventory-catalog", "/ordering-sheet", "/audit-history",
    "/distributors", "/consignment", "/billing", "/refunds", "/cash-register",
    "/reports", "/reports/yearly", "/insights", "/retention", "/admin/users",
    "/admin/logs", "/settings",
]


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def _login(page):
    page.goto(f"{APP_URL}/login", wait_until="domcontentloaded")
    page.fill('input[name="username"]', ADMIN_USER)
    page.fill('input[name="password"]', ADMIN_PASS)
    page.click('button[type="submit"], input[type="submit"]')
    page.wait_for_load_state("domcontentloaded")


@pytest.fixture(scope="module")
def signed_in(browser):
    """One logged-in context per viewport, reused across pages — logging in
    29 times per viewport would triple the runtime for nothing."""
    contexts = {}
    for name, w, h in VIEWPORTS:
        ctx = browser.new_context(viewport={"width": w, "height": h})
        page = ctx.new_page()
        _login(page)
        assert "/login" not in page.url, f"could not log in at {name} size"
        contexts[name] = page
    yield contexts
    for page in contexts.values():
        page.context.close()


def _visit(page, path):
    errors, failed_requests = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("requestfailed", lambda r: failed_requests.append(f"{r.method} {r.url}"))
    page.goto(f"{APP_URL}{path}", wait_until="networkidle")
    return errors, failed_requests


# ---------------------------------------------------------------------------

def test_the_app_is_actually_reachable(signed_in):
    """Guard on the guards: if login silently failed, every test below would
    be checking the login page 29 times and passing."""
    page = signed_in["laptop"]
    page.goto(f"{APP_URL}/", wait_until="domcontentloaded")
    assert "/login" not in page.url, "not signed in — every other browser test would be vacuous"
    assert page.locator("nav, .sidebar, aside").count() > 0, "no navigation on the dashboard"


@pytest.mark.parametrize("viewport", [v[0] for v in VIEWPORTS])
def test_no_page_scrolls_sideways(signed_in, viewport):
    """Horizontal overflow is the single most common responsive break, and
    the one a desktop-only check never sees. A wide table escaping its card
    pushes the whole page sideways; the fix is that the table scrolls inside
    its own container instead."""
    page = signed_in[viewport]
    offenders = []
    for path in PAGES:
        page.goto(f"{APP_URL}{path}", wait_until="networkidle")
        overflow = page.evaluate(
            "() => ({doc: document.documentElement.scrollWidth,"
            " win: window.innerWidth})")
        # A couple of pixels of slack for sub-pixel rounding and scrollbars.
        if overflow["doc"] > overflow["win"] + 3:
            offenders.append(f"{path}: content {overflow['doc']}px in a {overflow['win']}px window")
    assert not offenders, (
        f"page(s) scrolling sideways at {viewport} size:\n  " + "\n  ".join(offenders))


def test_no_page_raises_a_javascript_error_or_fails_an_asset(signed_in):
    """A broken script leaves buttons that look fine and do nothing. A failed
    asset leaves an unstyled page. Neither shows up in a server-side test."""
    page = signed_in["laptop"]
    problems = []
    for path in PAGES:
        errors, failed = _visit(page, path)
        for e in errors:
            problems.append(f"{path}: JS error: {e[:120]}")
        for f in failed:
            problems.append(f"{path}: failed request: {f[:120]}")
    assert not problems, "browser problem(s):\n  " + "\n  ".join(problems)


def test_no_interactive_element_is_rendered_invisible(signed_in):
    """Zero-width or zero-height controls are the shape of the modal bug: a
    flex child collapsing to nothing while the markup looks perfectly
    correct. Only the browser knows the difference."""
    page = signed_in["laptop"]
    offenders = []
    for path in PAGES:
        page.goto(f"{APP_URL}{path}", wait_until="networkidle")
        collapsed = page.evaluate("""() => {
            const out = [];
            for (const el of document.querySelectorAll('button, a.btn, .btn, input[type=submit]')) {
                // offsetParent is null for anything inside a hidden ancestor, which
                // is every control in a closed modal. Checking only the element's
                // OWN computed style misses those and reports the whole app as
                // broken -- the first version of this test did exactly that.
                if (el.offsetParent === null) continue;
                const cs = getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                const r = el.getBoundingClientRect();
                if (r.width < 1 || r.height < 1) {
                    out.push((el.textContent || el.value || el.className || el.tagName).trim().slice(0, 40) || '<unnamed>');
                }
            }
            return out;
        }""")
        for c in collapsed:
            offenders.append(f"{path}: {c!r} rendered at zero size")
    assert not offenders, "collapsed control(s):\n  " + "\n  ".join(offenders)


def test_touch_targets_are_big_enough_on_a_phone(signed_in):
    """44px is the accessibility floor. A control smaller than that is
    genuinely hard to hit with a thumb, and invisible as a problem on a
    laptop with a mouse."""
    page = signed_in["phone"]
    offenders = []
    for path in PAGES[:12]:
        page.goto(f"{APP_URL}{path}", wait_until="networkidle")
        small = page.evaluate("""() => {
            const out = [];
            for (const el of document.querySelectorAll('button, a.btn, .btn, input[type=submit], .theme-toggle-btn')) {
                const cs = getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                const r = el.getBoundingClientRect();
                if (r.width === 0 && r.height === 0) continue;
                if (r.height < 40 || r.width < 40) {
                    out.push(((el.textContent || el.value || el.className).trim().slice(0,28))
                             + ` ${Math.round(r.width)}x${Math.round(r.height)}`);
                }
            }
            return out;
        }""")
        for s in small:
            offenders.append(f"{path}: {s}")
    assert not offenders, (
        f"{len(offenders)} touch target(s) under 40x40 on a phone:\n  "
        + "\n  ".join(offenders[:20]))


def test_every_page_still_renders_in_dark_mode(signed_in, browser):
    """Dark mode doubles every colour decision in the app and is the theme
    nobody checks before shipping."""
    ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                              color_scheme="dark")
    page = ctx.new_page()
    _login(page)
    try:
        problems = []
        for path in PAGES:
            errors, failed = _visit(page, path)
            problems.extend(f"{path}: {e[:100]}" for e in errors)
            body_bg = page.evaluate("() => getComputedStyle(document.body).backgroundColor")
            if body_bg in ("rgba(0, 0, 0, 0)", "transparent"):
                problems.append(f"{path}: body has no background colour of its own in dark mode")
        assert not problems, "dark mode problem(s):\n  " + "\n  ".join(problems)
    finally:
        ctx.close()
