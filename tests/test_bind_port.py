# -*- coding: utf-8 -*-
"""The address shown to staff must be derived, never a literal.

Two pages tell staff where to reach this app — the dashboard and Settings.
Both hard-coded ":5050" in IQ while JO derived it from BIND_PORT, so IQ
printed the wrong address on any other port. That is not hypothetical: the
two installs on this machine collide on the default ports and JO had to be
moved to 5051 (COMPARISON.md §54). A wrong address is worse than no address,
because staff will try it and conclude the app is down.
"""
import re
from pathlib import Path

import app as app_module

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


def _port_env_var():
    """The env var this app actually reads, taken from app.py rather than
    guessed — the two apps use different prefixes and a guess would silently
    test the sibling's variable, which is never set here."""
    src = (Path(app_module.__file__)).read_text(encoding="utf-8")
    m = re.search(r'BIND_PORT = int\(os\.environ\.get\("([A-Z_]+)"', src)
    assert m, "app.py no longer derives BIND_PORT from the environment"
    return m.group(1)


def test_no_template_hard_codes_a_port():
    offenders = []
    for f in sorted(TEMPLATES.rglob("*.html")):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r":50[0-9][0-9]\b", line):
                offenders.append(f"{f.name}:{i}: {line.strip()[:90]}")
    assert not offenders, (
        "These templates hard-code a port instead of using {{ bind_port }}:\n  "
        + "\n  ".join(offenders)
    )


def test_bind_port_is_exposed_to_templates():
    assert app_module.app.jinja_env.globals.get("bind_port") == app_module.BIND_PORT


def test_bind_port_follows_the_environment(monkeypatch):
    """Control: the constant is read from the environment, not a literal —
    otherwise the guard above passes while every page still shows 5050."""
    import importlib

    var = _port_env_var()
    monkeypatch.setenv(var, "5999")
    reloaded = importlib.reload(app_module)
    try:
        assert reloaded.BIND_PORT == 5999, (
            f"BIND_PORT ignored {var}=5999 — it is a literal, not a derived value."
        )
    finally:
        monkeypatch.undo()
        importlib.reload(app_module)
