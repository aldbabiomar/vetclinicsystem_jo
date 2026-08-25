"""
Route smoke tests — every page renders.

Seed money is Decimal, matching JO's model — seeding floats here would
quietly exercise a type the app never stores.

The cheapest test in the suite and, per line covered, the most valuable.
It walks the app's own URL map and requests every GET route with a logged-in
session, asserting none of them returns a server error.

What that actually catches: a template referencing a variable the route
stopped passing, a context builder crashing on an empty table, a query
broken by a schema change, a helper renamed in one place and not the other.
None of those are visible until someone opens the page — which, for a page
like Yearly Reports or Consignment Shrinkage, might be months.

What it does NOT catch: whether the page shows the *right* numbers. It is a
crash test, not a correctness test. The money tests are where correctness
lives.

Routes are discovered, never listed by hand — a new page is covered the
moment it is registered, with no test to remember to write.
"""
import uuid
from datetime import datetime, date
from decimal import Decimal

import pytest

from conftest import needs_db


pytestmark = needs_db


# Endpoints deliberately left out, each for a concrete reason.
SKIP_ENDPOINTS = {
    # Ends the session the rest of the suite is sharing.
    "logout",
    # Makes a real network call to the GitHub releases API: slow, flaky
    # offline, and it would hit an external service on every test run.
    "settings_updates_check",
    # Serves a file from disk by name; there is nothing meaningful to
    # request without a real generated file, and the download path is
    # covered by its own tests.
    "static",
}


def _all_get_routes(flask_app):
    return sorted(
        (r for r in flask_app.url_map.iter_rules()
         if "GET" in r.methods and r.endpoint not in SKIP_ENDPOINTS),
        key=lambda r: str(r),
    )


def _no_arg_routes(flask_app):
    return [r for r in _all_get_routes(flask_app) if not r.arguments]


def _arg_routes(flask_app):
    return [r for r in _all_get_routes(flask_app) if r.arguments]


# ---------------------------------------------------------------------------
# Every page that takes no parameters
# ---------------------------------------------------------------------------

def test_route_discovery_found_a_realistic_number_of_pages(flask_app):
    """A guard on the guard: if route discovery silently returned nothing,
    every test below would pass while testing absolutely nothing."""
    assert len(_no_arg_routes(flask_app)) > 30, "route discovery is not finding the app's pages"


def test_every_page_renders_without_a_server_error(client, flask_app):
    failures = []
    for rule in _no_arg_routes(flask_app):
        url = str(rule)
        try:
            resp = client.get(url)
        except Exception as exc:                     # noqa: BLE001 - reporting, not handling
            failures.append(f"{url} raised {type(exc).__name__}: {exc}")
            continue
        if resp.status_code >= 500:
            failures.append(f"{url} -> HTTP {resp.status_code}")
    assert not failures, "page(s) returning a server error:\n  " + "\n  ".join(failures)


def test_pages_render_on_a_database_with_no_clinic_data(client, flask_app, db):
    """Empty-state rendering. A report page that assumes at least one row —
    a max() over nothing, a division by a zero count — works fine all through
    development and breaks on the very first day at a new clinic, which is
    the worst possible moment to find it."""
    failures = []
    for rule in _no_arg_routes(flask_app):
        url = str(rule)
        resp = client.get(url)
        if resp.status_code >= 500:
            failures.append(f"{url} -> HTTP {resp.status_code}")
    assert not failures, (
        "page(s) failing against a near-empty database:\n  " + "\n  ".join(failures))


# ---------------------------------------------------------------------------
# Pages that take an id — exercised against real rows
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def seeded_ids(flask_app):
    """One real row of each kind the id-taking routes need, so those pages
    render against actual data rather than a 404 path."""
    import db as dbmod
    con = dbmod.connect()
    tag = uuid.uuid4().hex[:8].upper()
    o_id, p_id, v_id = f"O{tag}", f"P{tag}", f"V{tag}"
    inv_id, pl_id = f"INV{tag}", f"PL{tag}"
    con.execute("INSERT INTO owners (id, name) VALUES (?,?)", (o_id, f"Smoke Owner {tag}"))
    con.execute("INSERT INTO patients (id, owner_id, animal_name) VALUES (?,?,?)",
                (p_id, o_id, f"Smoke Pet {tag}"))
    con.execute("INSERT INTO visits (id, patient_id, date, case_status) VALUES (?,?,?,?)",
                (v_id, p_id, date.today().isoformat(), "Ongoing"))
    con.execute("INSERT INTO inventory_list (id, name, category, unit, track_expiry, cost_price, "
                "ownership_type, active) VALUES (?,?,?,?,?,?,?,?)",
                (inv_id, f"Smoke Item {tag}", "Retail", "unit", False, Decimal("2.000"), "Owned", True))
    con.execute("INSERT INTO price_list (id, name, category, cost_price, sale_price, active, "
                "linked_item_id, can_discount) VALUES (?,?,?,?,?,?,?,?)",
                (pl_id, f"Smoke Item {tag}", "Retail", Decimal("2.000"), Decimal("10.000"), True, inv_id, True))
    cur = con.execute("INSERT INTO boarding_sessions (patient_id, entry_date, special_needs, "
                      "total_is_auto, cleanup_amount, discount_percent, dismissed, total) "
                      "VALUES (?,?,?,?,?,?,?,?) RETURNING id",
                      (p_id, date.today().isoformat(), False, False, Decimal(0), Decimal(0), False, Decimal("10.000")))
    boarding_id = cur.fetchone()["id"]
    con.commit()

    ids = {
        "owner_id": o_id, "patient_id": p_id, "visit_id": v_id,
        "item_id": inv_id, "boarding_id": boarding_id,
    }
    # Anything already in the database is fine for the rest — these routes
    # only need *an* id that resolves, not one this test created.
    for key, sql in (
        ("case_id", "SELECT id FROM inpatient_cases ORDER BY id LIMIT 1"),
        ("sale_id", "SELECT id FROM sales ORDER BY id LIMIT 1"),
        ("dist_id", "SELECT id FROM distributors ORDER BY id LIMIT 1"),
        ("distributor_id", "SELECT id FROM distributors ORDER BY id LIMIT 1"),
        ("settlement_id", "SELECT id FROM consignment_settlements ORDER BY id LIMIT 1"),
        ("session_id", "SELECT id FROM audit_sessions ORDER BY id LIMIT 1"),
    ):
        row = con.execute(sql).fetchone()
        if row:
            ids[key] = row["id"]
    yield ids
    for sql, args in (
        ("DELETE FROM boarding_sessions WHERE id=?", (boarding_id,)),
        ("DELETE FROM price_list WHERE id=?", (pl_id,)),
        ("DELETE FROM inventory_list WHERE id=?", (inv_id,)),
        ("DELETE FROM visits WHERE id=?", (v_id,)),
        ("DELETE FROM patients WHERE id=?", (p_id,)),
        ("DELETE FROM owners WHERE id=?", (o_id,)),
    ):
        con.execute(sql, args)
    con.commit()
    con.close()


def test_every_detail_page_renders_for_a_real_record(client, flask_app, seeded_ids):
    """Detail pages are where the join-heavy queries live, and where a
    schema change is most likely to have been missed."""
    failures, exercised = [], 0
    for rule in _arg_routes(flask_app):
        # Routes taking a filename or a path serve a file from disk; there is
        # no id to substitute and nothing useful to assert here.
        if {"filename", "relpath"} & set(rule.arguments):
            continue
        if not set(rule.arguments) <= set(seeded_ids):
            continue                       # no seeded row of this kind
        url = str(rule)
        for name in rule.arguments:
            url = url.replace(f"<{name}>", str(seeded_ids[name]))
            url = url.replace(f"<int:{name}>", str(seeded_ids[name]))
        if "<" in url:
            continue                       # an unrecognised converter; skip rather than guess
        exercised += 1
        resp = client.get(url)
        if resp.status_code >= 500:
            failures.append(f"{url} -> HTTP {resp.status_code}")
    assert exercised > 0, "no id-taking page was actually exercised"
    assert not failures, "detail page(s) returning a server error:\n  " + "\n  ".join(failures)


def test_detail_pages_handle_an_id_that_does_not_exist(client, flask_app):
    """A made-up id must produce a "not found" message or a 404 — never a
    server error. This is the shape of bug that was found in
    visit_billing_save: the not-found check existed but sat on a branch that
    only ran when something else had already gone wrong."""
    failures, exercised = [], 0
    for rule in _arg_routes(flask_app):
        if {"filename", "relpath"} & set(rule.arguments):
            continue
        url = str(rule)
        for name in rule.arguments:
            # An id that is syntactically valid for both text and int columns.
            url = url.replace(f"<{name}>", "999999")
            url = url.replace(f"<int:{name}>", "999999")
        if "<" in url:
            continue
        exercised += 1
        resp = client.get(url)
        if resp.status_code >= 500:
            failures.append(f"{url} -> HTTP {resp.status_code}")
    assert exercised > 0
    assert not failures, (
        "page(s) erroring on a non-existent id instead of degrading:\n  " + "\n  ".join(failures))


# ---------------------------------------------------------------------------
# Access control — the smoke suite is logged in, so check the opposite too
# ---------------------------------------------------------------------------

def test_every_page_requires_a_login(flask_app):
    """A page reachable without a session leaks clinic data to anyone on the
    network. The app gates this globally with an allowlist, so the risk is a
    new page accidentally being added to that allowlist rather than each
    page forgetting its own guard."""
    anon = flask_app.test_client()
    public = {"login", "health", "favicon_ico", "static"}
    leaked = []
    for rule in _no_arg_routes(flask_app):
        if rule.endpoint in public:
            continue
        resp = anon.get(str(rule))
        # Anything that is not a redirect to login is suspicious.
        if resp.status_code == 200:
            leaked.append(f"{rule} -> HTTP 200 without a session")
    assert not leaked, "page(s) reachable while logged out:\n  " + "\n  ".join(leaked)
