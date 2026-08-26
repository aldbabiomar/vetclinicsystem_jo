"""
Regression guard: a form field naming a date column must go through
clean_date() -- or, on a read-side filter, clean_date_filter() -- before it
can reach a query.

The bug shape this catches is a bare read of a date form field --
f.get("visit_date"), f.get("visit_date", ""), f["visit_date"], or the
blank-vs-missing collapse f.get("x_date") if <flag> else None -- handed
onward without validation. It is the shape that broke /insights
(inpatient_boarding_occupancy) and was waiting to do the same to the
Retention tab (cohort_retention_grid).

Why it matters beyond "the value looks wrong": logic.parse_date() used to
validate a 10-character *prefix* rather than the value, so
"2026-08-25garbage" returned a real date and raised nothing, and the
unvalidated string reached the query anyway (COMPARISON.md section 21).
That specific hole is fixed; this test guards the call sites, which is the
other half of the same problem.

Strengthened 2026-08-26 (divergence audit finding 7.6), at the same time it
was ported to IQ. The original only saw a form variable literally named
`f`, only scanned app.py, and rejected a legitimate multi-line clean_date()
call. Its docstring also cited data_integrity_framework.md, which exists in
neither repo. See the two control tests below: this file's own detector is
tested, because a guard that silently stops matching anything passes
forever without checking a thing (CLAUDE.md section 7.3).
"""
import re
import pathlib

APP_ROOT = pathlib.Path(__file__).parent.parent

# JO validates write-side dates through clean_date() and read-side ?date=
# filters through clean_date_filter() (app.py). IQ has no clean_date_filter,
# so its copy of this test lists only clean_date -- keep each app's list
# matching that app's actual code rather than sharing one.
WRAPPERS = ("clean_date(", "clean_date_filter(")

# The object is captured rather than assumed, so any alias for request.form
# is covered, not just the conventional `f`.
READ = re.compile(
    r'(request\.form|[a-zA-Z_][a-zA-Z0-9_]*)'
    r'(?:\.get\(\s*"(\w+)"|\[\s*"(\w+)"\s*\])'
)

ALIAS = re.compile(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*request\.form\b')


def _form_aliases(src):
    """Every local name bound to request.form in this module, plus the
    direct request.form.get(...) form."""
    return set(ALIAS.findall(src)) | {"request.form"}


def _is_date_field(field):
    # "date" as a whole token: "date", "visit_date", "dismissal_date" --
    # but not merely containing it, e.g. "updates_log".
    return "date" in field.split("_")


def _scan(src, wrappers=WRAPPERS):
    """Return [(lineno, field, obj)] for unvalidated date-field form reads.

    Scans the whole source rather than line by line, so a clean_date(...)
    call split across lines is correctly recognised as wrapped.
    """
    aliases = _form_aliases(src)
    offenders = []
    for m in READ.finditer(src):
        obj = m.group(1)
        field = m.group(2) or m.group(3)
        if obj not in aliases or not _is_date_field(field):
            continue
        preceding = src[: m.start()].rstrip()
        if any(preceding.endswith(w) for w in wrappers):
            continue
        offenders.append((src.count("\n", 0, m.start()) + 1, field, obj))
    return offenders


def _modules():
    return sorted(p for p in APP_ROOT.glob("*.py"))


def test_no_unvalidated_write_side_date_fields():
    offenders = []
    seen = 0
    for path in _modules():
        src = path.read_text()
        aliases = _form_aliases(src)
        seen += sum(
            1
            for m in READ.finditer(src)
            if m.group(1) in aliases and _is_date_field(m.group(2) or m.group(3))
        )
        for lineno, field, obj in _scan(src):
            offenders.append(
                "%s:%d: date field %r read from %s without clean_date()"
                % (path.name, lineno, field, obj)
            )

    assert not offenders, (
        "Form date field(s) reaching a query without clean_date():\n"
        + "\n".join(offenders)
    )

    # Anti-vacuity: if the app stops using this idiom entirely the test above
    # would pass while checking nothing. It reads 21 such fields today.
    assert seen >= 10, (
        "The scanner found only %d date form reads across %d modules. Either "
        "the app changed how it reads form data, or this detector stopped "
        "matching -- fix the detector rather than lowering this floor."
        % (seen, len(_modules()))
    )


# --- controls: prove the detector actually detects -------------------------
# Without these, "no offenders" and "the regex is broken" are the same result.
# Both shapes below were confirmed to behave as asserted when this was
# written; the first four cases are ones the original rule missed entirely.

BAD = [
    ('f = request.form\nd = f.get("visit_date")\n', "bare .get"),
    ('f = request.form\nd = f.get("visit_date", "")\n', "bare .get with default"),
    ('f = request.form\nd = f["visit_date"]\n', "bare subscript"),
    ('d = request.form.get("visit_date")\n', "direct request.form.get"),
    ('g = request.form\nd = g.get("visit_date")\n', "alias other than f"),
    (
        'f = request.form\nd = f.get("dismissal_date") if flag else None\n',
        "blank-vs-missing ternary",
    ),
]

GOOD = [
    ('f = request.form\nd = clean_date(f.get("visit_date"))\n', "wrapped"),
    (
        'f = request.form\nd = clean_date(\n    f.get("visit_date"), "Visit date")\n',
        "wrapped across lines",
    ),
    (
        'f = request.form\nd = clean_date_filter(f.get("start_date"))\n',
        "wrapped in the read-side filter helper",
    ),
    ('f = request.form\nd = f.get("updates_log")\n', "not a date field"),
    ('other = {}\nd = other.get("visit_date")\n', "not a form object"),
]


def test_detector_flags_every_known_bad_shape():
    for src, label in BAD:
        assert _scan(src), "detector missed a real offender: %s" % label


def test_detector_accepts_every_known_good_shape():
    for src, label in GOOD:
        assert not _scan(src), "detector false-positived on: %s" % label
