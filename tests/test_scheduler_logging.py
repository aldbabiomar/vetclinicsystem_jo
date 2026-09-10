"""
Scheduled jobs must say why they failed.

Every handler in scheduler.py swallows deliberately — an exception escaping a
scheduler thread kills that job and nothing else notices. Until now they also
swallowed *silently*, in the one component whose whole recorded bug history is
work that did not happen and said nothing about it: missed jobs skipped
(COMPARISON.md §32), a frozen monotonic clock (§33), the tick and the cron
racing (§37), a vanished backup destination reported healthy (§39), and a
failing backup erasing the evidence its folder was ever real (§41).

The behaviour under test is deliberately narrow, and it has two halves that
must both hold:

  GUARD    — a failure inside a scheduled job reaches the error log.
  CONTROL  — the job still swallows. Turning these into raised exceptions
             would satisfy the guard and break the thing the swallow is for.

Pure tier: no database, no scheduler, no network. The jobs take get_db and
close_db as arguments, so a callable that raises is enough to drive the
failure path.
"""
import logging

import pytest

import scheduler


@pytest.fixture
def captured(monkeypatch):
    """Capture what _log_failure would write, without touching errors.log.

    _log_failure imports `app` lazily and calls app.error_logger, so a stub
    module object placed in sys.modules is what the code under test picks up.
    """
    import sys
    import types

    records = []

    class _Recorder:
        def error(self, msg):
            records.append(("error", msg))

        def warning(self, msg):
            records.append(("warning", msg))

    stub = types.ModuleType("app")
    stub.error_logger = _Recorder()
    monkeypatch.setitem(sys.modules, "app", stub)
    return records


def _boom(*args, **kwargs):
    raise RuntimeError("scheduled job blew up")


# ---------------------------------------------------------------------------
# GUARD — the failure is recorded
# ---------------------------------------------------------------------------

def test_a_failing_backup_catchup_is_logged(captured):
    """Reverting _log_failure() to a bare `pass` fails this."""
    result = scheduler._run_backup_if_due(_boom, lambda c: None)
    assert result is False
    assert captured, "the backup catch-up failed and recorded nothing at all"
    level, msg = captured[0]
    assert level == "error"
    assert "nightly backup catch-up" in msg, msg
    assert "RuntimeError" in msg and "scheduled job blew up" in msg, (
        "the log line must carry the traceback, not just a label")


def test_a_failing_self_check_due_check_is_logged(captured):
    scheduler._run_self_check_if_due(_boom, lambda c: None)
    assert any("self-check is due" in m for _, m in captured), captured


def test_a_failing_self_check_is_logged(captured):
    scheduler._do_self_check(_boom, lambda c: None)
    assert any("the daily self-check" in m for _, m in captured), captured


def test_a_failing_restore_verification_is_logged(captured):
    scheduler._do_verify_restore(_boom, lambda c: None)
    assert any("restore verification" in m for _, m in captured), captured


def test_a_failing_close_is_logged_as_a_warning_not_an_error(captured):
    """Failing to hand a connection back is real but not the job failing —
    the two are recorded at different levels so one cannot drown the other."""
    scheduler._do_self_check(lambda: object(), _boom)
    levels = {lvl for lvl, _ in captured}
    assert "warning" in levels, captured
    assert any(lvl == "warning" and "closing the connection" in m for lvl, m in captured), captured


# ---------------------------------------------------------------------------
# CONTROL — the job still swallows, and still returns what it used to
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("job", [
    "_run_backup_if_due",
    "_run_self_check_if_due",
    "_do_self_check",
    "_do_verify_restore",
])
def test_a_failing_job_never_raises(captured, job):
    """CONTROL. Without this, 'log it' could quietly become 'raise it', which
    is the behaviour every one of these handlers exists to prevent."""
    getattr(scheduler, job)(_boom, lambda c: None)


def test_backup_catchup_still_reports_false_on_failure(captured):
    """CONTROL. The return value is what the tick uses to decide whether the
    backup ran; logging must not change it."""
    assert scheduler._run_backup_if_due(_boom, lambda c: None) is False


def test_logging_failure_cannot_break_the_job(monkeypatch):
    """CONTROL. Telemetry must never be the thing that breaks the job it is
    describing — so _log_failure swallows its own failures too."""
    import sys
    import types

    class _Exploding:
        def error(self, msg):
            raise OSError("errors.log is unwritable")

        def warning(self, msg):
            raise OSError("errors.log is unwritable")

    stub = types.ModuleType("app")
    stub.error_logger = _Exploding()
    monkeypatch.setitem(sys.modules, "app", stub)

    assert scheduler._run_backup_if_due(_boom, lambda c: None) is False


def test_no_scheduled_job_swallows_silently_any_more():
    """GUARD on the pattern itself, so a handler added later cannot go back to
    a bare `pass`. The single permitted one is inside _log_failure, which must
    swallow its own failure — see the control above."""
    import inspect
    import re

    src = inspect.getsource(scheduler)
    silent = list(re.finditer(r"except Exception:\n\s+pass", src))
    owners = []
    for m in silent:
        start = src.rfind("\ndef ", 0, m.start())
        owners.append(re.match(r"\ndef (\w+)", src[start:]).group(1))
    assert owners == ["_log_failure"], (
        f"scheduled jobs swallowing without a trace: {owners} — every handler "
        f"in this module should call _log_failure() before it swallows")
