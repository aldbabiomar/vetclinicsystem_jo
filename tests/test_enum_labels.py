# -*- coding: utf-8 -*-
"""`enum_labels.py` duplicates constants on purpose — these tests keep the copies honest.

pybabel extracts by reading source text, so `_(CASE_STATUSES)` on a variable
yields nothing and the stored values have to be spelled out a second time.
Duplication nothing checks is how the first version of that file came to
declare a grooming status of "In Progress" that no code has ever stored,
while the real "Waiting" went untranslated and nothing failed.

Two sources of truth are checked here: the Python constants, and the literal
lists that exist only inside a template's `{% for x in [...] %}`.
"""
import ast
import re
from pathlib import Path

import pytest

import enum_labels
import core
import logic
from routes import clinical, inventory

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"


def labels(name):
    """enum_labels list as plain strings (they are lazy proxies)."""
    return [str(s) for s in getattr(enum_labels, name)]


@pytest.mark.parametrize("label_name,source", [
    ("CASE_STATUSES", clinical.CASE_STATUSES),
    ("FOLLOWUP_REASONS", clinical.FOLLOWUP_REASONS),
    ("WELLNESS_TYPES", clinical.WELLNESS_TYPES),
    ("GROOMING_SERVICES", logic.GROOMING_SERVICES),
    ("PAYMENT_METHODS", core.PAYMENT_METHODS),
    ("PRICE_CATEGORIES", inventory.PRICE_CATEGORIES),
    ("INVENTORY_CATEGORIES", inventory.INVENTORY_CATEGORIES),
    ("REVENUE_CATEGORIES", logic.REVENUE_CATEGORIES),
])
def test_mirrors_the_python_constant(label_name, source):
    assert labels(label_name) == list(source), (
        f"enum_labels.{label_name} has drifted from the constant it mirrors. "
        f"The stored values are what routes validate against — fix the copy in "
        f"enum_labels.py, never the constant."
    )


def template_literal_lists():
    """{loop var: [literals]} for every `{% for x in ['A','B'] %}` in templates."""
    found = {}
    for f in sorted(TEMPLATES.rglob("*.html")):
        src = f.read_text(encoding="utf-8")
        for m in re.finditer(r"\{%\s*for\s+(\w+)\s+in\s+(\[[^\]]*\])\s*%\}", src):
            try:
                values = ast.literal_eval(m.group(2))
            except (ValueError, SyntaxError):
                continue
            if all(isinstance(v, str) for v in values):
                found.setdefault((f.name, m.group(1)), values)
    return found


@pytest.mark.parametrize("label_name,template,var", [
    ("VISIT_TYPES", "visit_form_edit.html", "t"),
    ("FOLLOWUP_STATUSES", "followups_list.html", "s"),
    ("GROOMING_STATUSES", "grooming_list.html", "s"),
    ("SPECIES", "patient_form_edit.html", "sp"),
    ("REPRO_STATUSES", "patient_form_edit.html", "rs"),
    ("HOUSING", "patient_form_edit.html", "h"),
    ("SEXES", "patient_form_edit.html", "sx"),
])
def test_mirrors_the_template_literal(label_name, template, var):
    lists = template_literal_lists()
    assert (template, var) in lists, (
        f"{template} no longer has a `{{% for {var} in [...] %}}` literal list — "
        f"if the vocabulary moved to Python, point this test at the new constant."
    )
    assert labels(label_name) == lists[(template, var)]


def test_every_translatable_option_has_an_explicit_value():
    """An <option> with no value= submits its TEXT. Translate the text and the
    form posts Arabic into a column the app compares against English.

    Proven end to end once: a visit payment submitted in Arabic stored
    method='نقدًا', which the cash register buckets as "other" rather than
    Cash, so the drawer count reported a surplus that was not real.
    scripts/simulation/repro_option_value.py.
    """
    offenders = []
    for f in sorted(TEMPLATES.rglob("*.html")):
        src = f.read_text(encoding="utf-8")
        for m in re.finditer(r"<option\b([^>]*)>(.*?)</option>", src, re.S):
            attrs, body = m.group(1), m.group(2)
            if re.search(r"\bvalue\s*=", attrs):
                continue
            if "_(" in body or "|tr" in body:
                line = src[:m.start()].count("\n") + 1
                offenders.append(f"{f.name}:{line} {body.strip()[:50]}")
    assert not offenders, (
        "These <option> elements submit their translated text as the form value:\n  "
        + "\n  ".join(offenders)
        + "\nGive each one value=\"<the English constant>\"."
    )


def test_control_an_option_with_an_explicit_value_is_accepted():
    """Pairs with the guard above: it must pass the shape we actually ship,
    not refuse every <option> it sees."""
    sample = '<option value="Cash" selected>{{ _(\'Cash\') }}</option>'
    m = re.fullmatch(r"<option\b([^>]*)>(.*?)</option>", sample, re.S)
    assert re.search(r"\bvalue\s*=", m.group(1)), "the control itself is malformed"


def test_permission_labels_mirror_auth():
    """The roles matrix renders auth.PERMISSIONS through |tr, so every label
    has to be declared — a new permission added to auth.py without a line here
    shows as English on an otherwise Arabic page."""
    import auth
    assert labels("PERMISSION_LABELS") == [lab for _, lab, _ in auth.PERMISSIONS]
    assert labels("PERMISSION_CATEGORIES") == list(auth.PERMISSION_CATEGORIES)


def test_cash_ledger_events_mirror_the_query():
    """These are built inside the SQL of logic.cash_register_ledger(), so the
    source of truth is the query text itself."""
    import re
    src = (ROOT / "logic.py").read_text(encoding="utf-8")
    start = src.index("def cash_register_ledger")
    segment = src[start:start + 8000]
    found = set(re.findall(r"'([A-Z][A-Za-z ]+)' AS event_type", segment))
    found |= set(re.findall(r"THEN '([A-Z][A-Za-z ]+)'", segment))
    found |= set(re.findall(r"ELSE '([A-Z][A-Za-z ]+)' END", segment))
    declared = set(labels("CASH_LEDGER_EVENTS"))
    missing = found - declared
    assert not missing, (
        f"cash_register_ledger() produces event types that enum_labels does not "
        f"declare, so they render in English: {sorted(missing)}")
    assert found, "no event types found in the query — this test reads nothing"


def test_weekday_labels_mirror_logic():
    import logic
    assert labels("WEEKDAY_LABELS") == list(logic.WEEKDAY_LABELS)


def test_seeded_roles_mirror_auth():
    """auth.py seeds three roles with a name and a description; both render
    through |tr, so both have to be declared or they stay English."""
    import re
    src = (ROOT / "auth.py").read_text(encoding="utf-8")
    block = src[src.index("    defaults = ["):]
    block = block[:block.index("\n    ]")]
    pairs = re.findall(r'\(\s*"([^"]+)",\s*"((?:[^"\\]|\\.)*)"', block, re.S)
    assert len(pairs) == 3, f"expected 3 seeded roles, found {len(pairs)}"
    names = [n for n, _ in pairs]
    descs = [re.sub(r"\s+", " ", d) for _, d in pairs]
    assert labels("SEEDED_ROLE_NAMES") == names
    assert [re.sub(r"\s+", " ", d) for d in labels("SEEDED_ROLE_DESCRIPTIONS")] == descs
