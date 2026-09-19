"""
Cross-cutting rules that must hold on EVERY surface that needs them, not just
the one where the rule was first written.

Why this file exists: five of the six findings in
SIMULATION_AUDIT_2026-09-11.md, and the bill-locking bug found in the seam
audit that followed, were all the same shape — a rule present in one code
path and missing from its sibling. Each surface was individually correct and
individually tested, so nothing failed. The rules below are the ones whose
absence has actually caused a bug here; each test names the incident it comes
from. See SEAM_RULES.md in the workspace for the full register.

These are deliberately PRECISE rather than broad. A fuzzy "does this function
mention a guard" scan produces both false alarms (a guard delegated to a
helper) and false silence (a helper that merely mentions the rule), and a
guard nobody trusts gets deleted. Each rule here is narrow enough to be
checked exactly.

Discovery is a live filesystem walk over `app.py` + every `routes/*.py`, not
a hardcoded list, and every test asserts a FLOOR on how much it inspected —
`CLAUDE.md` §7.3 and COMPARISON.md §51: a scanning guard whose subject moves
goes vacuous rather than red, which is how four guards in this codebase came
to pass while checking nothing.
"""
import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).parent.parent


def _route_modules():
    """app.py plus every blueprint — a live walk, never a fixed list.

    After the blueprint split (COMPARISON.md §49) a scan that reads only
    app.py inspects a fraction of the surface while still passing.
    """
    mods = [ROOT / "app.py"]
    mods += sorted((ROOT / "routes").glob("*.py"))
    return [p for p in mods if p.exists() and p.name != "__init__.py"]


def _functions():
    """[(module_name, func_name, source)] for every top-level function."""
    out = []
    for path in _route_modules():
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError:  # pragma: no cover
            pytest.fail(f"{path.name} does not parse")
        lines = src.splitlines(keepends=True)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                end = getattr(node, "end_lineno", None) or node.lineno
                out.append((path.name, node.name,
                            "".join(lines[node.lineno - 1:end])))
    return out


def test_the_scan_finds_something_to_scan():
    """The floor. Every rule below is a 'no matches' assertion, and those pass
    hardest when they are scanning nothing."""
    mods = _route_modules()
    fns = _functions()
    assert len(mods) >= 6, f"expected app.py + blueprints, found {[m.name for m in mods]}"
    assert len(fns) >= 100, f"only {len(fns)} functions found — the walk is not reaching the routes"


# ---------------------------------------------------------------------------
# Rule 1 — a bill mutation takes the parent row's lock.
#
# From the 2026-09-11 seam audit. visit_billing_save() had always taken
# `SELECT ... FOR UPDATE` on the visit, and its comment said the lock was
# there "so a concurrent discount save on the same visit serialises behind
# this one". A lock only serialises if BOTH sides take it, and
# visit_discount_save() never did — nor did inpatient_discount_save() or
# inpatient_billing_add(). The two guards that stop a discount landing on a
# non-discountable line were validating against snapshots the other had
# already invalidated, and because the paths took locks in different orders
# they deadlocked outright. Reproduced: 2/25 trials produced a discounted bill
# carrying a non-discountable line, plus 46 DeadlockDetected 500s.
# ---------------------------------------------------------------------------

BILL_WRITE = re.compile(
    r"(INSERT INTO|UPDATE)\s+(billing|visit_billing_lines|inpatient_billing)\b"
    r"|UPDATE\s+visits\s+SET[^\"']*discount_percent"
    r"|UPDATE\s+inpatient_cases\s+SET[^\"']*discount_percent",
    re.I)
PARENT_LOCK = re.compile(
    r"FROM\s+(visits|inpatient_cases)\s+WHERE\s+id=\?\s+FOR UPDATE", re.I)


def test_every_bill_mutation_takes_the_parent_row_lock():
    mutators, unlocked = [], []
    for mod, fn, src in _functions():
        if not BILL_WRITE.search(src):
            continue
        mutators.append(f"{mod}:{fn}")
        if not PARENT_LOCK.search(src):
            unlocked.append(f"{mod}:{fn}")
    assert mutators, "found no bill-mutating function at all — the pattern has drifted"
    assert not unlocked, (
        "these write to a bill without first locking the visit/case row, so a "
        "concurrent write to the same bill can interleave with them (and, taking "
        "locks in a different order from the routes that DO lock, deadlock):\n  "
        + "\n  ".join(unlocked))


# ---------------------------------------------------------------------------
# Rule 2 — a read-side date filter is validated before it reaches a query.
#
# From SIMULATION_AUDIT F4 and the seam audit's admin_logs finding. `?day=`
# (present but empty) reached Postgres as a date and 500'd the appointment
# book; `?date=` on /admin/logs reached a text-prefix comparison, matched
# nothing, and rendered an EMPTY audit log with no warning — indistinguishable
# from "nobody did anything that day", on the one page whose job is showing
# what happened.
# ---------------------------------------------------------------------------

READS_DATE_ARG = re.compile(r"request\.args\.get\(\s*[\"'](date|day|week|date_from|date_to)[\"']")
VALIDATES_DATE = re.compile(
    r"parse_date\s*\(|clean_date\s*\(|clean_date_filter\s*\(|date_filter_arg\s*\(")


def test_every_read_side_date_filter_is_validated():
    readers, unvalidated = [], []
    for mod, fn, src in _functions():
        if not READS_DATE_ARG.search(src):
            continue
        readers.append(f"{mod}:{fn}")
        if not VALIDATES_DATE.search(src):
            unvalidated.append(f"{mod}:{fn}")
    assert readers, "found no date-filtered route at all — the pattern has drifted"
    assert not unvalidated, (
        "these read a date from the query string and use it without validating "
        "it. An invalid value either reaches Postgres as a date (a 500) or "
        "silently matches nothing (an empty page the user reads as 'no data'):\n  "
        + "\n  ".join(unvalidated))


# ---------------------------------------------------------------------------
# Rule 3 — form input is never parsed with a bare float().
#
# From SIMULATION_AUDIT F2/F5. _save_audit_lines() used float(), which accepts
# "nan" — the one numeric entry point not using parse_money(), whose entire
# purpose is rejecting non-finite input. The NaN was confirmable and then
# disabled the guard reading it (`qty > nan` is False), letting POS sell 500
# units off an empty shelf in IQ and 500-ing the till in JO.
# ---------------------------------------------------------------------------

# Scoped to float() deliberately. int("nan") and int("inf") raise ValueError,
# so int() is not the NaN hazard float() is — the four int(f.get(...)) sites
# in this codebase are each wrapped in try/except ValueError with a friendly
# flash, which is correct. Widening this rule to int() would report all four
# as violations, and a guard that cries wolf gets deleted.
#
# This walks the AST rather than matching text, because the text version of
# this rule DID NOT CATCH ITS OWN BUG. F2 was written as
#
#     stock = request.form.get(f"stock_{iid}", "").strip()
#     ...
#     float(stock)
#
# and a regex looking for `float(f.get(` or `float(request.form` sees nothing
# there. The first draft of this test was proven blind by reintroducing F2 and
# watching it stay green — CLAUDE.md §7.3, caught on the guard's own first
# mutation run. So: track which local names were assigned from the request,
# then flag float() applied to any of them.


def _request_tainted_names(fn_node):
    """Locals assigned (even indirectly) from request.form/args in this
    function. One hop is enough for the shape this rule cares about."""
    tainted = set()
    for _ in range(3):          # settle: a = form.get(); b = a.strip(); c = b
        before = len(tainted)
        for node in ast.walk(fn_node):
            if not isinstance(node, ast.Assign):
                continue
            src = ast.dump(node.value)
            hits_request = ("request" in src and ("form" in src or "args" in src))
            hits_tainted = any(f"id='{n}'" in src for n in tainted)
            if hits_request or hits_tainted:
                for tgt in node.targets:
                    for sub in ast.walk(tgt):
                        if isinstance(sub, ast.Name):
                            tainted.add(sub.id)
        if len(tainted) == before:
            break
    return tainted


def test_form_input_is_never_parsed_with_a_bare_float():
    offenders = []
    for path in _route_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            tainted = _request_tainted_names(fn)
            if not tainted:
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "float" and node.args):
                    arg = node.args[0]
                    names = {s.id for s in ast.walk(arg) if isinstance(s, ast.Name)}
                    touched = names & tainted
                    if touched:
                        offenders.append(
                            f"{path.name}:{fn.name} -> float({', '.join(sorted(touched))}) "
                            f"on line {node.lineno}")
    assert not offenders, (
        "float() accepts 'nan' and 'inf' without raising, and every bound check "
        "downstream silently evaluates False against NaN — so an unchecked value "
        "does not merely store badly, it disables the guards that read it. Use "
        "parse_money()/parse_quantity():\n  " + "\n  ".join(offenders))


# ---------------------------------------------------------------------------
# Rule 4 — a request argument with a default is not read with .get(k, default)
# when an empty value would be wrong.
#
# From SIMULATION_AUDIT F4, which is worth stating as its own rule because it
# is invisible on inspection: `request.args.get("day", today)` applies the
# default only when the parameter is ABSENT. A present-but-empty "?day=" keeps
# the empty string, and parse_date("") RETURNS None rather than raising, so the
# route's own except-ValueError guard never fired either. Two independent
# near-misses in one line.
# ---------------------------------------------------------------------------

GET_WITH_DEFAULT = re.compile(
    # `,(?!\s*(?:""|\'\'))` and NOT `,\s*(?!""|\'\')`: with `\s*` outside the
    # lookahead the engine backtracks it to zero width, the lookahead then
    # inspects the space rather than the default, and every correct
    # `get("date", "")` call is reported as a violation. Found by this test
    # flagging three already-correct sites on its first run.
    r"request\.args\.get\(\s*[\"'](date|day|week|date_from|date_to)[\"'],(?!\s*(?:\"\"|''))")


def test_date_arguments_use_or_rather_than_a_get_default():
    offenders = []
    for mod, fn, src in _functions():
        for m in GET_WITH_DEFAULT.finditer(src):
            offenders.append(f"{mod}:{fn} -> {m.group(0).strip()}")
    assert not offenders, (
        "request.args.get(key, default) applies the default only when the key is "
        "ABSENT — a present-but-empty '?date=' keeps the empty string and reaches "
        "the query. Use `request.args.get(key) or default`:\n  "
        + "\n  ".join(offenders))


# ---------------------------------------------------------------------------
# Rules 5-8 — the rewards card (features/REWARDS_CARD_PLAN.md §11.1).
#
# This feature puts ONE new rule on four payment paths that were already
# shaped differently from each other, which is the exact situation every
# entry in SEAM_RULES.md §2 came out of. These four rules are the parts that
# must not drift apart.
#
# They scan logic.py and pdf_export.py as well as the route modules, because
# after the blueprint split most of the money code is not in a route at all.
# ---------------------------------------------------------------------------
def _money_modules():
    """Every module that can do bill arithmetic — a live walk, not a list."""
    mods = _route_modules()
    for extra in ("logic.py", "pdf_export.py", "core.py", "money.py"):
        p = ROOT / extra
        if p.exists():
            mods.append(p)
    return mods


def _all_functions():
    """[(module, func, source, node, module_src)] across _money_modules()."""
    out = []
    for path in _money_modules():
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        lines = src.splitlines(keepends=True)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                end = getattr(node, "end_lineno", None) or node.lineno
                out.append((path.name, node.name,
                            "".join(lines[node.lineno - 1:end]), node, src))
    return out


LINE_TABLES = ("visit_billing_lines", "inpatient_billing", "sale_items")


def test_rule5_every_billed_line_insert_snapshots_discountability():
    """A line inserted without `discountable` is a line whose eligibility was
    never recorded, so every later recomputation of that bill and every refund
    against it prices it wrong.

    The dropped NOT NULL default catches this at runtime too (setup.py drops
    it deliberately), but only once someone exercises that path. This catches
    it at the source.
    """
    inserts, offenders = 0, []
    for mod, fn, src, _node, _msrc in _all_functions():
        for m in re.finditer(r"INSERT INTO (%s)\s*\(([^)]*)\)" % "|".join(LINE_TABLES),
                             src, re.S):
            inserts += 1
            if "discountable" not in m.group(2):
                offenders.append(f"{mod}:{fn} -> INSERT INTO {m.group(1)}")
    assert inserts >= 3, (
        f"expected to find an INSERT for each of {LINE_TABLES}, found {inserts} — "
        "this scan has lost its subject and would pass against anything")
    assert not offenders, (
        "billed-line INSERT(s) that do not record `discountable`:\n  "
        + "\n  ".join(offenders))


def test_rule6_every_bill_total_call_passes_the_discountable_subtotal():
    """`compute_bill_totals(..., discountable_subtotal=...)` is keyword-only
    and has no default precisely so a caller cannot forget it — forgetting it
    would silently treat a non-discountable item as discountable and produce a
    wrong total with nothing to show for it.

    Walks the AST rather than the text: a call split across lines, or one
    whose keyword sits under a comment mentioning it, is not something a
    regex can judge. SEAM_RULES.md §3 records that rule 3's first
    text-matching draft was proven blind by its own mutation run.
    """
    calls, offenders = 0, []
    for path in _money_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fname = (node.func.attr if isinstance(node.func, ast.Attribute)
                     else getattr(node.func, "id", None))
            if fname != "compute_bill_totals":
                continue
            calls += 1
            if not any(kw.arg == "discountable_subtotal" for kw in node.keywords):
                offenders.append(f"{path.name}:{node.lineno}")
    assert calls >= 5, (
        f"expected several compute_bill_totals() calls, found {calls} — "
        "the scan has lost its subject")
    assert not offenders, (
        "compute_bill_totals() call(s) with no discountable_subtotal:\n  "
        + "\n  ".join(offenders))


def test_rule7_a_route_writing_a_discount_from_a_request_checks_its_source():
    """"Card only": a member's bill carries the card's discount and nothing
    else. Any route that takes a discount percentage from the REQUEST and
    writes it must first establish whether the bill is a member's — otherwise
    it is a path that can overwrite, raise or wipe a card discount.

    Tested as a GROUP rather than one route at a time, which is the whole
    point: three of these four were safe only because of a refusal added to a
    fourth.
    """
    checked, offenders = 0, []
    for mod, fn, src, _node, _msrc in _all_functions():
        writes_discount = re.search(
            r"(UPDATE\s+\w+\s+SET[^\"']*discount_percent\s*=|"
            r"INSERT INTO\s+\w+\s*\([^)]*discount_percent)", src, re.S)
        reads_request = "request.form" in src or re.search(r"\bf\.get\(", src)
        if not (writes_discount and reads_request):
            continue
        checked += 1
        if "discount_source" not in src:
            offenders.append(f"{mod}:{fn}")
    assert checked >= 3, (
        f"expected several request-driven discount writers, found {checked} — "
        "the scan has lost its subject")
    assert not offenders, (
        "route(s) writing a request-supplied discount without consulting "
        "discount_source:\n  " + "\n  ".join(offenders))


# The only places a discount percentage may be turned into money. Everything
# else must go through logic.discounted_raw_total() or read a STORED total.
# A new report that re-derives `lines x (1 - d)` is exactly how JO's P&L gap
# would come back (features/REWARDS_CARD_PLAN.md §2.1).
DISCOUNT_ARITHMETIC_ALLOWED = {
    "discounted_raw_total",       # the one shared formula
    "compute_bill_totals",        # calls it
    "refundable_sale_items",      # per line, against the line's own snapshot
    "_revenue_and_cogs_by_month",  # weights a stored total by discounted share
    "revenue_by_category",        # ditto, in SQL
    "payable_total",              # rounding, not discounting
    "member_discount_rate",       # reads the setting
}


def _without_comments(src):
    """Python `#` and SQL `--` comments removed.

    A comment that DESCRIBES the formula is not the formula. SEAM_RULES.md
    §7.3 records a mutation that was edited into a comment containing the
    words FOR UPDATE rather than into the SQL below it, and passed.
    """
    out = []
    for line in src.splitlines():
        line = re.sub(r"#.*$", "", line)
        line = re.sub(r"--.*$", "", line)
        out.append(line)
    return "\n".join(out)


DIVIDES_BY_100 = re.compile(r"/\s*100(?:\.0)?\b|Decimal\(100\)")


def test_rule8_discount_arithmetic_only_happens_where_it_is_allowed():
    """A percentage turned into money outside the allow-list is a second
    implementation of the bill, and the two will disagree the first time a
    member's bill carries a non-discountable line.

    Scanned per FUNCTION, not per line, deliberately: the P&L weighting reads
    the rate on one line and divides on another, and a line-scoped version of
    this rule did not see it. Its own floor is what reported that — the first
    draft found 2 of the 4 real sites and said so rather than passing.
    """
    seen, offenders = 0, []
    for mod, fn, src, _node, _msrc in _all_functions():
        body = _without_comments(src)
        if "discount" not in body.lower() or not DIVIDES_BY_100.search(body):
            continue
        seen += 1
        if fn not in DISCOUNT_ARITHMETIC_ALLOWED:
            offenders.append(f"{mod}:{fn}")
    assert seen >= 3, (
        f"expected to find discount arithmetic at the allow-listed sites, found "
        f"{seen} — the scan has lost its subject and would pass against anything")
    assert not offenders, (
        "discount arithmetic outside the allow-list — use "
        "logic.discounted_raw_total() or read the stored total:\n  "
        + "\n  ".join(offenders))
