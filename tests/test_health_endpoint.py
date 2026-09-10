"""
/health is unauthenticated, so whatever it says is public.

It is in OPEN_ENDPOINTS by design — updater.py probes it on a throwaway port
to prove a new release boots and can reach the database before that release is
ever promoted. What it must not do is describe the failure in the database
driver's own words: psycopg's connection errors carry the host, port and user
inline, e.g.

    connection to server at "127.0.0.1", port 5432 failed:
    FATAL:  password authentication failed for user "vetclinic"

which is reachable whenever the pool has no live connection — an app starting
before Postgres is the ordinary way to get there.
"""
import re

import pytest

from conftest import needs_db

pytestmark = needs_db

SECRETS = ["port ", "password", "user \"", "connection to server", "postgresql://"]


def test_a_healthy_response_says_nothing_beyond_the_version(client):
    """CONTROL. The endpoint still has to do its job — updater.py's
    _probe_health() reads exactly this."""
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert "version" in body
    assert set(body) == {"status", "version"}, f"unexpected fields: {set(body)}"


def test_a_failing_health_check_discloses_nothing_about_the_database(flask_app, monkeypatch):
    """GUARD. Reverting to `return {"status": "error", "detail": str(e)}` fails
    this: the exception raised here is the shape psycopg actually produces."""
    import app as app_module

    boom = RuntimeError(
        'connection to server at "127.0.0.1", port 5432 failed: '
        'FATAL:  password authentication failed for user "vetclinic"')

    def explode():
        raise boom

    monkeypatch.setattr(app_module, "get_db", explode)
    resp = flask_app.test_client().get("/health")
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["status"] == "error"
    detail = body.get("detail", "")
    for leak in SECRETS:
        assert leak not in detail.lower(), (
            f"/health leaked {leak!r} to an unauthenticated caller: {detail!r}")


def test_a_failing_health_check_still_gives_something_to_go_on(flask_app, monkeypatch):
    """CONTROL. Redacting everything would satisfy the guard and leave an
    operator with a 503 and no thread to pull. The reference id ties the
    response to the full traceback in the access-controlled log."""
    import app as app_module

    def explode():
        raise RuntimeError("nope")

    monkeypatch.setattr(app_module, "get_db", explode)
    resp = flask_app.test_client().get("/health")
    detail = resp.get_json()["detail"]
    assert "Reference" in detail
    assert re.search(r"Reference [0-9A-F]{8}", detail), detail
    assert "errors.log" in detail


def test_the_failure_is_written_to_the_error_log(flask_app, monkeypatch):
    """CONTROL. The reference id is worthless if nothing was recorded under
    it — that would be a pointer to an empty room."""
    import app as app_module

    written = []
    monkeypatch.setattr(app_module.error_logger, "error", lambda m: written.append(m))
    monkeypatch.setattr(app_module, "get_db", lambda: (_ for _ in ()).throw(RuntimeError("secret detail here")))
    resp = flask_app.test_client().get("/health")
    ref = re.search(r"Reference ([0-9A-F]{8})", resp.get_json()["detail"]).group(1)
    assert written, "nothing was logged"
    assert ref in written[0], "the log line does not carry the reference shown to the caller"
    assert "secret detail here" in written[0], "the real cause did not reach the log"


def test_health_is_still_reachable_without_logging_in(flask_app):
    """CONTROL. updater.py probes this before any session exists."""
    assert flask_app.test_client().get("/health").status_code in (200, 503)
