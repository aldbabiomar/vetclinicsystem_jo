# -*- coding: utf-8 -*-
"""Self-check findings must stay translatable.

These are the sentences on the "this install needs attention" banner — the
ones that say backups are failing or the backup folder has vanished. They are
the messages that matter most at the moment they appear, and they were the
last English left on an otherwise fully Arabic dashboard.

They are awkward because a finding is WRITTEN to `self_check_log` when the
scheduler runs and READ BACK later, possibly in another language. So
`selfcheck.py` stores English plus a msgid, and `app.py`'s `finding` filter
translates at render. Two things have to hold for that to keep working:

  1. every message is marked with `N_(...)` — a message built by an f-string
     produces a different string every time and can never match a catalogue
     entry, so it would silently stay English forever;
  2. `message` remains the RENDERED ENGLISH, because the heartbeat, the log
     and several existing tests read it — including one that asserts a
     particular error string is ABSENT, which would go vacuous if the value
     moved into `args` and out of `message`.
"""
import ast
from pathlib import Path

SELFCHECK = Path(__file__).resolve().parents[1] / "selfcheck.py"


def _finding_calls():
    tree = ast.parse(SELFCHECK.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "_finding"
                and len(node.args) >= 3):
            yield node


def _is_marked(node):
    """N_( ... ), or a conditional choosing between two N_( ... )."""
    if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "N_":
        return True
    if isinstance(node, ast.IfExp):
        return _is_marked(node.body) and _is_marked(node.orelse)
    return False


def test_every_finding_message_is_marked_for_extraction():
    offenders = []
    for call in _finding_calls():
        msg = call.args[2]
        if not _is_marked(msg):
            kind = "f-string" if isinstance(msg, ast.JoinedStr) else type(msg).__name__
            offenders.append(f"selfcheck.py:{call.lineno}: {kind}")
    assert not offenders, (
        "These findings are not marked with N_(), so pybabel cannot extract "
        "them and they will render in English forever. An f-string is the "
        "usual cause — use N_(\"... %(name)s ...\") with an args dict "
        "instead:\n  " + "\n  ".join(offenders))


def test_the_scanner_actually_reads_the_findings():
    """Floor. Without it this file passes hardest when `_finding` is renamed
    and the scanner matches nothing at all."""
    found = list(_finding_calls())
    assert len(found) >= 20, (
        f"only {len(found)} _finding calls found in selfcheck.py — the scanner "
        f"has stopped matching, so the guard above is checking nothing")


def test_message_is_still_the_rendered_english():
    """`message` must keep carrying the VALUES, not just the template.

    Several existing tests read it, and one asserts that a specific error
    string does NOT appear in it — that check only means something while the
    value is actually there to be absent."""
    import selfcheck
    f = selfcheck._finding("x", "warn", "No successful backup for %(days)s days.",
                           {"days": 9})
    assert f["message"] == "No successful backup for 9 days.", f
    assert f["msgid"] == "No successful backup for %(days)s days.", f
    assert f["args"] == {"days": 9}, f

    plain = selfcheck._finding("y", "warn", "No backup has ever completed successfully.")
    assert plain["message"] == plain["msgid"], plain


def test_a_bad_argument_set_cannot_break_the_banner():
    """A finding reports a problem. If its own formatting raises, the banner
    that was trying to tell someone about a failing backup would be the thing
    that disappears."""
    import selfcheck
    f = selfcheck._finding("z", "warn", "needs %(missing)s", {"other": 1})
    assert f["message"] == "needs %(missing)s", (
        "a mismatched args dict should fall back to the template, not raise")
