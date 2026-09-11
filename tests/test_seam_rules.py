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
