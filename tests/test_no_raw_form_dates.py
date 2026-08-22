"""
Regression guard for the data-integrity bug described in
data_integrity_framework.md: a form field for a date column collapsing
blank-vs-missing incorrectly (f.get("x_date", default) or f.get("x_date")
if <flag> else None, instead of going through clean_date()) is exactly the
bug shape that broke /insights (inpatient_boarding_occupancy) and was
waiting to do the same to the Retention tab (cohort_retention_grid).

This test does not try to prove the app is correct - it just catches the
specific anti-pattern from regressing: a bare f.get("...date...") (or
f["...date..."]) that is not immediately wrapped in clean_date(...).
"""
import re
import pathlib

APP_PY = pathlib.Path(__file__).parent.parent / "app.py"


def test_no_unvalidated_write_side_date_fields():
    src = APP_PY.read_text()
    offenders = []
    for lineno, line in enumerate(src.splitlines(), start=1):
        for m in re.finditer(r'f\.get\("(\w+)"[^)]*\)|f\["(\w+)"\]', line):
            field = m.group(1) or m.group(2)
            # Only care about fields whose name is actually "date" as a
            # whole token (e.g. "date", "dismissal_date", "appt_date"),
            # not merely containing the substring (e.g. "updates_log").
            if "date" not in field.split("_"):
                continue
            preceding = line[: m.start()]
            if preceding.rstrip().endswith("clean_date("):
                continue  # properly wrapped
            offenders.append("line %d: unvalidated write-side date field %r" % (lineno, field))
    assert not offenders, (
        "Found form date field(s) not passed through clean_date() before "
        "reaching a DB write - see Layer 2 in data_integrity_framework.md:\n"
        + "\n".join(offenders)
    )
