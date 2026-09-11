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


def _settle_loading_shell(page, timeout=45000):
    """Several heavy reports (/insights, /retention) answer with a placeholder
    that polls a background job and then navigates ITSELF to the real page.

    Anything that reads the page straight after `goto` is therefore looking at
    the shell, not at the page it named. That blind spot hid a real
    ReferenceError on /insights from the JavaScript-error test below — the
    error happened on the second navigation, after the assertion had already
    run. Wait for the shell to resolve before reading anything."""
    try:
        page.wait_for_function(
            "() => !document.querySelector('.vz-progress-shell')", timeout=timeout)
        page.wait_for_load_state("networkidle")
    except Exception:
        pass          # not a shell, or it never resolved — the caller still asserts


def _visit(page, path):
    errors, failed_requests = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("requestfailed", lambda r: failed_requests.append(f"{r.method} {r.url}"))
    page.goto(f"{APP_URL}{path}", wait_until="networkidle")
    _settle_loading_shell(page)
    return errors, failed_requests


def _visit_watching_console(page, path):
    """Same as _visit, but also collects console messages.

    `pageerror` does NOT fire for a Content-Security-Policy violation — the
    browser refuses the script and writes a console error instead, and the
    page carries on looking completely normal. So a CSP that blocks every
    inline script on a page is invisible to every check above this line, and
    would ship as "all pages load fine".
    """
    messages = []
    page.on("console", lambda m: messages.append(f"{m.type}: {m.text}"))
    page.goto(f"{APP_URL}{path}", wait_until="networkidle")
    return messages


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


@pytest.mark.parametrize("viewport", ["phone", "tablet"])
def test_touch_targets_are_big_enough_on_touch_screens(signed_in, viewport):
    """44px is the accessibility floor. A control smaller than that is
    genuinely hard to hit with a thumb, and invisible as a problem on a
    laptop with a mouse.

    PARAMETRIZED OVER TABLET AS WELL, and that is the point of this edit.
    This test previously ran on the phone alone, so it stood on the one
    viewport where the CSS already worked. Every touch rule lived inside a
    `max-width: 760px` query, and a tablet is 768 -- 8px on the wrong side --
    so at tablet size buttons measured 34-37px, 36 nav links were under 44,
    and the appointment "+" button was 21px tall: the same bug IQ 1.10.8 /
    JO 1.8.9 had already shipped a fix for, still live because the fix was
    inside that same query. The test named the right property and simply
    never looked where it was violated."""
    page = signed_in[viewport]
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
        f"{len(offenders)} touch target(s) under 40x40 at {viewport} size:\n  "
        + "\n  ".join(offenders[:20]))

@pytest.mark.parametrize("viewport", ["phone", "tablet"])
def test_form_fields_are_16px_so_ios_does_not_zoom(signed_in, viewport):
    """iOS Safari zooms the page whenever a focused field is under 16px, and
    does not zoom back out -- so every form entry leaves the page magnified.
    style.css carries that exact comment; the rule that fixes it was inside
    the 760px query, so on an iPad every field was 14px and the fix the
    comment describes never applied to the device it describes."""
    page = signed_in[viewport]
    offenders = []
    for path in ("/settings", "/owners/new", "/patients"):
        page.goto(f"{APP_URL}{path}", wait_until="networkidle")
        small = page.evaluate("""() => {
            const out = [];
            const sel = 'input:not([type=hidden]):not([type=checkbox]):not([type=radio]), select, textarea';
            for (const el of document.querySelectorAll(sel)) {
                const cs = getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                if (parseFloat(cs.fontSize) < 16) {
                    out.push((el.name || el.id || el.className || 'field') + ' ' + cs.fontSize);
                }
            }
            return out;
        }""")
        for s in small:
            offenders.append(f"{path}: {s}")
    assert not offenders, (
        f"{len(offenders)} field(s) under 16px at {viewport} size -- iOS will "
        f"zoom on focus and stay zoomed:\n  " + "\n  ".join(offenders[:20]))


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

# ---------------------------------------------------------------------------
# Interaction, not just rendering
#
# Everything above loads pages. That is not enough: POS "Complete Sale" was
# silently broken from IQ v1.5.0 / JO v1.1.0 — the button disabled itself
# inside its own onclick, and a disabled submitter cannot submit its form, so
# the click produced no request, no navigation and no error. Every page-load
# test passed the whole time, and so did every server-side route test, because
# they POST to /pos/checkout directly and never touch the button.
# ---------------------------------------------------------------------------

def _add_first_search_result(page):
    """Click the first POS search result, the way a cashier does.

    The selector is the interesting part. It used to be
    `div[onclick*=addToCart]`, which stopped matching anything the moment the
    inline handlers were moved into static/behaviors.js (review finding S6) —
    and because a missing result is a `skip`, not a failure, the two most
    important tests in this file went dormant and reported green. That is the
    same shape as the dormant browser tier in COMPARISON.md §40.3: a guard
    that goes vacuous rather than red.

    So this asserts rather than returning False when the search itself works
    but nothing matches the selector. "The search returned rows and none of
    them was clickable" is a bug, not a reason to skip.
    """
    page.fill("#posSearch", "a")
    page.wait_for_timeout(1200)
    rows = page.locator("#posResults .list-line")
    if rows.count() == 0:
        return False
    hit = page.locator('#posResults [data-vz-act="pos-4"]').first
    assert hit.count() > 0, (
        "the POS search returned rows but none carried the add-to-cart hook — "
        "the markup and static/behaviors.js have drifted apart, and every test "
        "using this helper would otherwise have skipped silently")
    hit.click()
    page.wait_for_timeout(300)
    return page.evaluate("() => (typeof cart !== 'undefined') && cart.length > 0")


def test_completing_a_sale_actually_submits(signed_in):
    """The end-to-end journey a cashier performs dozens of times a day.

    Asserts the click produces a real POST and leaves the page. Anything less
    — checking the button exists, or that the route works when posted to
    directly — passes while the button does nothing at all.
    """
    page = signed_in["laptop"]
    page.goto(f"{APP_URL}/pos", wait_until="networkidle")
    if not _add_first_search_result(page):
        pytest.skip("no sellable item in this database to put in the cart")

    posts = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)
    before = page.url
    page.click("#completeSaleBtn")
    page.wait_for_timeout(2500)

    assert posts, (
        "clicking Complete Sale sent no request at all — the form did not submit. "
        "Check nothing disables the submit button inside its own click handler.")
    assert any("checkout" in u for u in posts), f"posted somewhere unexpected: {posts}"
    assert page.url != before, "the page never left /pos after completing a sale"


def test_a_double_click_still_only_makes_one_sale(signed_in):
    """The protection the broken line was trying to provide. Fixing the submit
    must not reintroduce the double-charge it was guarding against."""
    page = signed_in["laptop"]
    page.goto(f"{APP_URL}/pos", wait_until="networkidle")
    if not _add_first_search_result(page):
        pytest.skip("no sellable item in this database to put in the cart")

    posts = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)
    page.evaluate("""() => { const b = document.getElementById('completeSaleBtn');
                             b.click(); b.click(); b.click(); }""")
    page.wait_for_timeout(2500)
    checkouts = [u for u in posts if "checkout" in u]
    assert len(checkouts) == 1, (
        f"{len(checkouts)} checkout requests from one triple-click — a customer could be "
        "charged more than once")


# ---------------------------------------------------------------------------
# Content-Security-Policy (review finding S6)
#
# script-src carries a per-request nonce instead of 'unsafe-inline'. Two ways
# that goes wrong silently: the nonce in the header stops matching the one in
# the markup (every inline script on every page is refused), or a template
# regrows an on*= attribute (that one handler stops working, nothing else).
# Neither raises a JS error and neither changes what the page looks like.
# ---------------------------------------------------------------------------

def test_no_page_reports_a_csp_violation(signed_in):
    """GUARD. Reintroduce 'unsafe-inline' and this still passes; break the
    nonce and it fails on every page at once, which is the failure worth
    catching, because nothing else in this file would notice it."""
    page = signed_in["laptop"]
    problems = []
    for path in PAGES:
        for msg in _visit_watching_console(page, path):
            low = msg.lower()
            if "content security policy" in low or "refused to execute" in low:
                problems.append(f"{path}: {msg[:160]}")
    assert not problems, (
        "Content-Security-Policy blocked script(s) — the page still renders, so "
        "nothing else here would have caught this:\n  " + "\n  ".join(problems))


def test_the_csp_actually_forbids_inline_script(signed_in):
    """CONTROL for the test above.

    Without this, a policy that had quietly gone back to 'unsafe-inline' would
    pass the violation check perfectly — no violations is exactly what a
    permissive policy produces. This injects a script the policy must refuse
    and fails if it runs.
    """
    page = signed_in["laptop"]
    page.goto(f"{APP_URL}/", wait_until="networkidle")
    ran = page.evaluate("""() => {
        const s = document.createElement('script');
        s.textContent = 'window.__cspProbe = true;';
        document.head.appendChild(s);
        return window.__cspProbe === true;
    }""")
    assert not ran, (
        "an inline <script> with no nonce executed — script-src is still "
        "permissive, so test_no_page_reports_a_csp_violation is passing "
        "for the wrong reason")


def test_the_nonce_changes_between_requests(signed_in):
    """A nonce reused across responses is worth about as much as none at all:
    an attacker who can read one page can embed it in the injection."""
    page = signed_in["laptop"]
    seen = set()
    for _ in range(3):
        page.goto(f"{APP_URL}/", wait_until="domcontentloaded")
        seen.add(page.evaluate(
            "() => document.querySelector('script[nonce]')?.nonce || "
            "document.querySelector('script[nonce]')?.getAttribute('nonce')"))
    assert None not in seen, "no inline script carried a nonce attribute"
    assert len(seen) == 3, f"the nonce repeated across requests: {seen}"


# ---------------------------------------------------------------------------
# Four bugs a clinic found by using the app. Every one of them rendered a page
# that looked fine to a status-code sweep, and none was visible to any test
# that existed. COMPARISON.md §59.
# ---------------------------------------------------------------------------

def _barcode_label_url(page):
    """The label URL of an item that actually has a barcode, or None.

    There is no anchor to scrape: the catalog reaches the label through the
    /barcode/status JSON its barcode-manager modal calls, so the test asks the
    same endpoint the page does."""
    page.goto(f"{APP_URL}/inventory-catalog", wait_until="networkidle")
    ids = page.evaluate("""() => [...new Set(
        [...document.querySelectorAll('[data-item-id], tbody tr td:first-child')]
          .map(e => (e.getAttribute('data-item-id') || e.textContent).trim())
          .filter(v => /^[A-Z]{2,4}\\d+$/.test(v)))].slice(0, 25)""")
    for item_id in ids:
        r = page.evaluate("""async (id) => {
          const res = await fetch(`/inventory-catalog/${id}/barcode/status`,
                                  {headers: {'Accept': 'application/json'}});
          if (!res.ok) return null;
          const j = await res.json();
          return j.label_url || null;
        }""", item_id)
        if r:
            return r
    return None


def test_the_barcode_label_actually_draws_a_barcode(signed_in):
    """It never did. The page bound its render function to the JsBarcode
    <script>'s load event from an inline script placed AFTER it — and a
    classic <script src> has already loaded and fired by then, so the listener
    heard nothing and the <svg> stayed empty. No error was shown either,
    because the error path was bound the same way.

    Asserting on the drawn SVG rather than on the page rendering is the whole
    point: the page rendered perfectly for as long as this was broken."""
    page = signed_in["laptop"]
    href = _barcode_label_url(page)
    if not href:
        pytest.skip("no inventory item has a barcode in this database")
    page.goto(f"{APP_URL}{href}", wait_until="networkidle")
    drawn = page.evaluate("document.getElementById('barcodeSvg').children.length")
    assert drawn > 0, (
        "the barcode <svg> is empty — nothing called the render function. "
        "Do not bind it to the vendor script's load event; call it directly.")
    assert not page.evaluate("document.getElementById('printBtn').disabled"), (
        "Print is still disabled, which means the render never completed")


def test_a_modal_never_grows_taller_than_the_screen(signed_in):
    """The Add Role modal was 1374px tall on a 390x844 phone: its top was
    clipped off-screen and the submit button sat 281px below the fold with
    nothing to scroll, so a role could not be created on a phone at all. On a
    1440x900 laptop it was 894px — six pixels of headroom.

    Checked on the phone, which is where a height bug actually bites."""
    page = signed_in["phone"]
    page.goto(f"{APP_URL}/admin/users", wait_until="networkidle")
    result = page.evaluate("""() => {
      const m = document.getElementById('roleModal');
      if (!m) return null;
      m.style.display = 'flex';
      const box = m.querySelector('.modal-box');
      const r = box.getBoundingClientRect();
      box.scrollTop = box.scrollHeight;
      const actions = box.querySelector('.form-actions');
      const a = actions.getBoundingClientRect();
      return {height: Math.round(r.height), top: Math.round(r.top),
              bottom: Math.round(r.bottom), viewport: innerHeight,
              submitReachable: a.bottom <= innerHeight + 1};
    }""")
    if result is None:
        pytest.skip("no role modal on this page")
    assert result["top"] >= -1 and result["bottom"] <= result["viewport"] + 1, (
        f"the modal does not fit the screen: {result}")
    assert result["submitReachable"], (
        f"the submit button cannot be reached even after scrolling: {result}")


def _open_insights(page, timeout=45000):
    """Open /insights and wait for the REAL page.

    It answers with a loading shell that polls a background job and then
    navigates itself, so `networkidle` plus a fixed sleep is a race — it
    passed on one app and failed on the other purely on timing. Waiting for a
    chart card (or an empty note in its place) waits for the thing under
    test."""
    page.goto(f"{APP_URL}/insights", wait_until="networkidle")
    page.wait_for_function(
        "() => document.querySelectorAll('.card.chart-panel').length >= 2",
        timeout=timeout)
    page.wait_for_timeout(1200)      # let Chart.js finish its first paint


def test_a_chart_fills_its_card_rather_than_its_aspect_ratio(signed_in):
    """Both Insights charts were sized from an aspect ratio Chart.js derived
    from the canvas height attribute, so their WIDTH was a function of their
    height — a doughnut became a square as tall as the card was wide, and the
    bar chart could not fill a wide card.

    A chart with no data is replaced by an empty note, so an absent canvas is
    a pass, not a failure — that is the other half of the same fix."""
    page = signed_in["laptop"]
    _open_insights(page)
    measured = page.evaluate("""() => {
      const out = [];
      for (const id of ['revenueChart', 'paymentChart']) {
        const c = document.getElementById(id);
        if (!c) { out.push({id, state: 'empty'}); continue; }
        const card = c.closest('.card');
        const cs = getComputedStyle(card);
        const inner = card.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
        const r = c.getBoundingClientRect();
        out.push({id, state: 'drawn', unused: Math.round(inner - r.width),
                  height: Math.round(r.height)});
      }
      return out;
    }""")
    assert measured, "no charts found on Insights at all"
    for m in measured:
        if m["state"] == "empty":
            continue
        assert abs(m["unused"]) <= 2, (
            f"{m['id']} leaves {m['unused']}px of its card unused — it is being "
            f"sized from an aspect ratio instead of filling the width")
        assert 0 < m["height"] <= 420, (
            f"{m['id']} is {m['height']}px tall; a chart should have a controlled "
            f"height, not one derived from the card width")


def test_an_empty_chart_explains_itself(signed_in):
    """A chart with no rows used to draw a blank white card with a title and
    nothing else, which reads as a broken feature rather than an empty one.
    If a canvas is absent there must be a note in its place."""
    page = signed_in["laptop"]
    _open_insights(page)
    blank = page.evaluate("""() => {
      const bad = [];
      document.querySelectorAll('.card.chart-panel').forEach(card => {
        const hasChart = card.querySelector('canvas');
        const hasNote = card.querySelector('.empty-note');
        if (!hasChart && !hasNote) bad.push(card.querySelector('h2')?.textContent || '?');
      });
      return bad;
    }""")
    assert not blank, f"these chart cards render neither a chart nor an explanation: {blank}"

    found = page.evaluate("document.querySelectorAll('.card.chart-panel').length")
    assert found >= 2, (
        f"only {found} chart cards found — the selector has stopped matching the "
        f"page, so this guard would pass while checking nothing")
