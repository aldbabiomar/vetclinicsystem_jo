"""
Layers 2 and 3 — the heartbeat and its payload.

Three properties are load-bearing here, and each has a control so that a
passing assertion means something:

1. DISABLED IS THE DEFAULT. No URL set means nothing is sent, and that is not
   an error and not a finding.
2. THE PAYLOAD CARRIES NO PERSONAL DATA. Asserted by seeding a real owner
   name and phone number and requiring neither string to appear anywhere in
   the serialised payload — with a control proving those strings were
   actually in the database, so "absent from the payload" cannot silently
   mean "absent from the clinic".
3. THE URL IS A CREDENTIAL. It must never appear in a return value, an
   exception message, or a log line — someone holding it can suppress a real
   alert by faking a ping.

Nothing here talks to a real receiver. The live ping against a real
healthchecks.io check is a separate, manual verification step (plan §6.1).
"""
import json
import logging
from datetime import datetime, timedelta

import pytest
import requests

from conftest import needs_db

pytestmark = needs_db

SECRET_URL = "https://hc-ping.example/00000000-1111-2222-3333-444444444444"


def _set(db, key, value):
    if value is None:
        db.execute("DELETE FROM settings WHERE key=?", (key,))
    else:
        db.execute(
            "INSERT INTO settings (key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
    db.commit()


@pytest.fixture
def hb(db):
    saved = {
        k: (db.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone() or {}).get("value")
        for k in ("heartbeat_url", "heartbeat_install_id")
    }
    _set(db, "heartbeat_url", None)
    yield db
    for k, v in saved.items():
        _set(db, k, v)


OK_RESULT = {"status": "ok", "ran_at": datetime.now().isoformat(timespec="seconds"),
             "findings": [], "disk_free_bytes": 5 * 1024 ** 3}


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


# --- 1. disabled is the default ------------------------------------------

def test_no_url_sends_nothing_and_is_not_an_error(hb, monkeypatch):
    import heartbeat
    calls = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: calls.append(a) or FakeResponse(200))

    ok, msg = heartbeat.send(hb, {"sent_at": "x"})
    assert ok is True, "a disabled heartbeat is not a failure"
    assert msg == "disabled"
    assert calls == [], "nothing may be sent when no URL is configured"


def test_a_configured_url_actually_sends(hb, monkeypatch):
    """The control for the test above — without it, 'sends nothing when
    disabled' and 'never sends anything at all' are the same result."""
    import heartbeat
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse(200)

    monkeypatch.setattr(requests, "post", fake_post)
    _set(hb, "heartbeat_url", SECRET_URL)

    ok, msg = heartbeat.send(hb, {"sent_at": datetime.now().isoformat(timespec="seconds")})
    assert ok is True
    assert len(calls) == 1
    assert calls[0][0] == SECRET_URL


def test_a_non_https_url_is_refused(hb, monkeypatch):
    import heartbeat
    calls = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: calls.append(a) or FakeResponse(200))
    _set(hb, "heartbeat_url", "http://hc-ping.example/abc")

    ok, msg = heartbeat.send(hb, {"sent_at": "x"})
    assert ok is False
    assert calls == [], "an http URL must not be contacted at all"


# --- 2. the payload carries no personal data -----------------------------

def test_payload_contains_no_names_phones_or_money(hb, db):
    import heartbeat
    marker_name = "ZZTESTOWNERNAME"
    marker_phone = "0791234567"
    owner_id = "ZZTEST_HB_OWNER"

    db.execute("DELETE FROM owners WHERE id=?", (owner_id,))
    db.execute("INSERT INTO owners (id, name, phone) VALUES (?,?,?)",
               (owner_id, marker_name, marker_phone))
    db.commit()
    try:
        # The control: prove the markers really are in the database, so that
        # "not in the payload" cannot quietly mean "not in the clinic either".
        row = db.execute("SELECT name, phone FROM owners WHERE id=?", (owner_id,)).fetchone()
        assert row["name"] == marker_name and row["phone"] == marker_phone

        payload = heartbeat.build_payload(db, OK_RESULT)
        blob = json.dumps(payload)
        assert marker_name not in blob, "an owner name reached the payload"
        assert marker_phone not in blob, "a phone number reached the payload"

        # And the owner was actually counted, so the payload really did look
        # at this table rather than skipping it.
        assert payload["db"]["row_counts"].get("owners", 0) >= 1
    finally:
        db.execute("DELETE FROM owners WHERE id=?", (owner_id,))
        db.commit()


def test_payload_carries_no_money_figures_and_no_file_paths(hb, db):
    import heartbeat
    payload = heartbeat.build_payload(db, OK_RESULT)
    blob = json.dumps(payload)

    for banned in ("total", "balance", "revenue", "price", "amount"):
        assert banned not in blob.lower(), f"a money-ish key {banned!r} reached the payload"
    assert "filepath" not in blob
    assert "/" not in json.dumps(payload["backup"]), (
        "a backup file path reached the payload — a path can carry a person's name"
    )


def test_payload_has_the_documented_shape(hb, db):
    import heartbeat
    payload = heartbeat.build_payload(db, OK_RESULT)
    for key in ("install_id", "app", "version", "sent_at", "status", "findings",
                "backup", "db", "disk_free_bytes", "uptime_hours"):
        assert key in payload, f"payload is missing {key}"
    assert payload["app"] == "jo"
    assert set(payload["backup"]) == {
        "last_success_at", "age_hours", "last_size_bytes",
        "consecutive_failures", "verified_at", "verified_result"}


def test_payload_stays_under_4kb_with_200_findings(hb, db):
    import heartbeat
    noisy = {
        "status": "fail",
        "ran_at": datetime.now().isoformat(timespec="seconds"),
        "findings": [{"code": f"c{i}", "severity": "warn",
                      "message": "x" * 200} for i in range(200)],
        "disk_free_bytes": 1,
    }
    payload = heartbeat.build_payload(db, noisy)
    size = len(json.dumps(payload).encode("utf-8"))
    assert size <= heartbeat.MAX_PAYLOAD_BYTES, f"payload was {size} bytes"
    assert len(payload["findings"]) <= heartbeat.MAX_FINDINGS
    # The parts worth looking at must survive the trimming.
    assert payload["backup"] and payload["db"]


def test_the_worst_findings_are_the_ones_kept(hb, db):
    import heartbeat
    mixed = {
        "status": "fail",
        "ran_at": datetime.now().isoformat(timespec="seconds"),
        "findings": ([{"code": f"w{i}", "severity": "warn", "message": "w"} for i in range(15)]
                     + [{"code": "the_fail", "severity": "fail", "message": "f"}]),
        "disk_free_bytes": 1,
    }
    payload = heartbeat.build_payload(db, mixed)
    codes = [f["code"] for f in payload["findings"]]
    assert "the_fail" in codes, "the only failure was dropped in favour of warnings"


def test_install_id_is_generated_once_and_then_stable(hb, db):
    import heartbeat
    _set(db, "heartbeat_install_id", None)
    first = heartbeat.install_id(db)
    assert first
    assert heartbeat.install_id(db) == first
    stored = db.execute(
        "SELECT value FROM settings WHERE key='heartbeat_install_id'").fetchone()
    assert stored["value"] == first


# --- 3. the URL is a credential ------------------------------------------

def test_a_failing_receiver_does_not_raise_and_never_leaks_the_url(hb, monkeypatch):
    import heartbeat
    monkeypatch.setattr(heartbeat, "RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResponse(500))
    _set(hb, "heartbeat_url", SECRET_URL)

    ok, msg = heartbeat.send(hb, {"sent_at": "x"})
    assert ok is False
    assert "500" in msg
    assert SECRET_URL not in msg
    assert "hc-ping.example" not in msg


def test_a_connection_error_does_not_raise_and_never_leaks_the_url(hb, monkeypatch):
    import heartbeat
    monkeypatch.setattr(heartbeat, "RETRY_DELAY_SECONDS", 0)

    def boom(*a, **k):
        # requests embeds the full URL in its exception text — which is
        # exactly how a credential ends up in a log file.
        raise requests.ConnectionError(f"failed to reach {SECRET_URL}")

    monkeypatch.setattr(requests, "post", boom)
    _set(hb, "heartbeat_url", SECRET_URL)

    ok, msg = heartbeat.send(hb, {"sent_at": "x"})
    assert ok is False
    assert SECRET_URL not in msg, "the URL leaked through the exception message"
    assert "hc-ping.example" not in msg


def test_saving_the_url_does_not_write_it_to_the_audit_log(hb, db, client):
    """The credential must not escape through Settings either.

    The other tests here guard heartbeat.send()'s return value. Saving the
    setting is a SECOND boundary, and it was leaking: the generic settings
    loop routes every changed key through auth.log_change(), which writes the
    value into audit_log — rendered on the Logins & Changes page to anyone
    holding view_logins_changes, a broader permission than manage_settings,
    and included in audit exports. A user who cannot open Settings could read
    the ping URL and use it to fake pings, suppressing the alert that fires
    when the clinic machine goes dark.
    """
    marker = "https://hc-ping.example/SECRET-AUDIT-MARKER-0001"
    db.execute("DELETE FROM audit_log WHERE field='heartbeat_url'")
    db.commit()

    resp = client.post("/settings", data={"heartbeat_url": marker},
                       follow_redirects=True)
    assert resp.status_code == 200

    stored = db.execute(
        "SELECT value FROM settings WHERE key='heartbeat_url'").fetchone()
    assert stored and stored["value"] == marker, (
        "the setting must still save — this is about what gets LOGGED"
    )

    rows = db.execute(
        "SELECT old_value, new_value FROM audit_log WHERE field='heartbeat_url'"
    ).fetchall()
    assert rows, "the change should still be audited, just without the value"
    blob = " ".join(f"{r['old_value']} {r['new_value']}" for r in rows)
    assert marker not in blob, "the ping URL was written to the audit log"
    assert "hc-ping.example" not in blob
    assert "set" in blob, "the audit entry should record THAT it changed"


def test_the_url_is_never_written_to_a_log(hb, monkeypatch, caplog):
    import heartbeat
    monkeypatch.setattr(heartbeat, "RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResponse(500))
    _set(hb, "heartbeat_url", SECRET_URL)

    with caplog.at_level(logging.DEBUG):
        heartbeat.send(hb, {"sent_at": "x"})
    assert SECRET_URL not in caplog.text
    assert "hc-ping.example" not in caplog.text


def test_it_retries_once_and_only_once(hb, monkeypatch):
    import heartbeat
    monkeypatch.setattr(heartbeat, "RETRY_DELAY_SECONDS", 0)
    attempts = []
    monkeypatch.setattr(requests, "post",
                        lambda *a, **k: attempts.append(1) or FakeResponse(500))
    _set(hb, "heartbeat_url", SECRET_URL)

    heartbeat.send(hb, {"sent_at": "x"})
    assert len(attempts) == 2, f"expected exactly one retry, saw {len(attempts)} attempts"


# --- reporting back ------------------------------------------------------

def test_a_successful_send_marks_the_self_check_reported(hb, db, monkeypatch):
    import heartbeat
    import selfcheck
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResponse(200))
    _set(hb, "heartbeat_url", SECRET_URL)

    result = selfcheck.run_self_check(db)
    selfcheck.record(db, result)
    assert selfcheck.latest(db)["reported_at"] is None

    ok, _ = heartbeat.send_for(db, result)
    assert ok is True
    assert selfcheck.latest(db)["reported_at"] is not None


def test_a_failed_send_leaves_it_unreported(hb, db, monkeypatch):
    import heartbeat
    import selfcheck
    monkeypatch.setattr(heartbeat, "RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResponse(503))
    _set(hb, "heartbeat_url", SECRET_URL)

    result = selfcheck.run_self_check(db)
    selfcheck.record(db, result)
    heartbeat.send_for(db, result)
    assert selfcheck.latest(db)["reported_at"] is None, (
        "an unsent result must not be recorded as reported"
    )
