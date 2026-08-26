"""
Layers 2 and 3 of operational monitoring: the heartbeat, and its payload.

WHY THIS LAYER EXISTS, stated precisely, because it is easy to build the
wrong thing here:

Everything else in this feature requires the app to be RUNNING in order to
report anything. The failures that actually cost a clinic its data are the
ones where that does not hold — the machine never came back after a power
cut, Docker did not start, someone closed the terminal window, the disk
filled. Those produce no error at all. They produce silence.

So **the signal is the ABSENCE of a ping**, not the content of one. The
receiver's job is to alert when a ping does not arrive. Implementers
sometimes invert this and alert only on `status == "fail"` payloads; that is
the wrong design and defeats the entire point of the layer, because a machine
that is off cannot send a "fail".

Consequences of that, all deliberate:

* A failed heartbeat is NOT escalated to the clinic. They cannot act on it,
  and a red banner about monitoring would train them to ignore the banners
  that matter.
* The heartbeat is OFF unless `heartbeat_url` is set. An app that phones home
  by default is not acceptable for clinic software. Off is not a finding.
* `heartbeat_url` is a CREDENTIAL — for healthchecks.io, anyone holding it
  can send a fake ping and thereby suppress a real alert. It is never written
  to a log, never included in an error message, and never returned to a
  caller. tests/test_heartbeat.py asserts this.
* The receiver is not assumed to be healthchecks.io. It is whatever URL the
  admin pastes in, one per install, so a clinic can be moved to a self-hosted
  receiver without a code change.

PRIVACY — the payload rules are absolute (§3.2 of the plan):
no owner/patient/staff names, no phone numbers, addresses or notes, no money
figures of any kind, no free text from any user-entered field, and no full
file paths (a path can carry a person's name; a basename is fine). Counts and
statuses only. `row_counts` exists to show a database is not empty, not to
describe a clinic's business.
"""
import json
import os
import time
from datetime import datetime

import requests

import logic

APP = "jo"

TIMEOUT_SECONDS = 10
RETRY_DELAY_SECONDS = 5
MAX_FINDINGS = 10
MAX_PAYLOAD_BYTES = 4096

URL_SETTING = "heartbeat_url"
INSTALL_ID_SETTING = "heartbeat_install_id"

_SEVERITY_RANK = {"fail": 2, "warn": 1, "ok": 0}

# Only these, and only as counts. Adding a table here is a privacy decision,
# not a convenience one.
ROW_COUNT_TABLES = ("owners", "patients", "visits", "sales")


def install_id(db):
    """A short random id so one receiver can host several clinics and the
    alert can say WHICH one went quiet. Generated once, then stable."""
    existing = logic.get_setting(db, INSTALL_ID_SETTING)
    if existing:
        return existing
    import secrets
    new = secrets.token_hex(4).upper()
    try:
        db.execute(
            "INSERT INTO settings (key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (INSTALL_ID_SETTING, new),
        )
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    return new


def _uptime_hours():
    try:
        import app as app_module
        started = getattr(app_module, "APP_STARTED_AT", None)
        if started is None:
            return None
        return round((datetime.now() - started).total_seconds() / 3600.0, 1)
    except Exception:
        return None


def _backup_section(db):
    """Counts, sizes and timestamps only — never a filepath. A backup path
    routinely contains a person's account name."""
    out = {"last_success_at": None, "age_hours": None, "last_size_bytes": None,
           "consecutive_failures": 0, "verified_at": None, "verified_result": None}
    try:
        row = db.execute(
            "SELECT * FROM backup_log WHERE status='success' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            out["last_success_at"] = row["started_at"]
            out["last_size_bytes"] = row["filesize_bytes"]
            try:
                delta = datetime.now() - datetime.fromisoformat(str(row["started_at"]))
                out["age_hours"] = round(delta.total_seconds() / 3600.0, 1)
            except (TypeError, ValueError):
                pass

        recent = db.execute(
            "SELECT status FROM backup_log ORDER BY id DESC LIMIT 20").fetchall()
        streak = 0
        for r in recent:
            if r["status"] == "failed":
                streak += 1
            else:
                break
        out["consecutive_failures"] = streak
    except Exception:
        pass

    try:
        raw = logic.get_setting(db, "last_verified_restore")
        if raw:
            data = json.loads(raw)
            out["verified_at"] = data.get("at")
            out["verified_result"] = data.get("result")
    except (TypeError, ValueError):
        pass
    return out


def _db_section(db):
    out = {"reachable": False, "table_count": None, "row_counts": {}}
    try:
        out["table_count"] = db.execute(
            "SELECT COUNT(*) c FROM information_schema.tables "
            "WHERE table_schema='public'").fetchone()["c"]
        out["reachable"] = True
    except Exception:
        return out
    for table in ROW_COUNT_TABLES:
        try:
            exists = db.execute(
                "SELECT to_regclass(?) IS NOT NULL AS e", (f"public.{table}",)
            ).fetchone()["e"]
            if exists:
                out["row_counts"][table] = db.execute(
                    f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
        except Exception:
            continue
    return out


def build_payload(db, self_check_result):
    """Layer 3. Counts and statuses only — see the module docstring."""
    findings = list(self_check_result.get("findings") or [])
    findings.sort(key=lambda f: _SEVERITY_RANK.get(f.get("severity"), 0), reverse=True)
    findings = findings[:MAX_FINDINGS]

    try:
        version = __import__("app").VERSION
    except Exception:
        version = "unknown"

    payload = {
        "install_id": install_id(db),
        "app": APP,
        "version": version,
        "sent_at": datetime.now().isoformat(timespec="seconds"),
        "status": self_check_result.get("status", "ok"),
        "findings": findings,
        "backup": _backup_section(db),
        "db": _db_section(db),
        "disk_free_bytes": self_check_result.get("disk_free_bytes"),
        "uptime_hours": _uptime_hours(),
    }

    # Size cap. Findings are the only unbounded part, so they are what gets
    # trimmed — never the backup/db sections, which are the reason to look.
    while len(json.dumps(payload).encode("utf-8")) > MAX_PAYLOAD_BYTES and payload["findings"]:
        payload["findings"] = payload["findings"][:-1]
    return payload


def send(db, payload):
    """POSTs the payload to settings.heartbeat_url. Returns (ok, message).

    NEVER raises, and never blocks longer than roughly
    2*TIMEOUT + RETRY_DELAY. A failed heartbeat is recorded and explicitly NOT
    escalated to the user.

    The URL never appears in the returned message — it is a credential.
    """
    try:
        url = (logic.get_setting(db, URL_SETTING) or "").strip()
    except Exception:
        return False, "could not read the heartbeat setting"

    if not url:
        # Disabled is the default, and is not a finding.
        return True, "disabled"

    if not url.lower().startswith("https://"):
        return False, "the configured heartbeat URL is not https"

    body = json.dumps(payload)
    last = "no attempt was made"
    for attempt in (1, 2):
        try:
            resp = requests.post(
                url, data=body, timeout=TIMEOUT_SECONDS,
                headers={"Content-Type": "application/json"},
            )
            if 200 <= resp.status_code < 300:
                _mark_reported(db, payload.get("sent_at"))
                return True, f"sent ({resp.status_code})"
            last = f"receiver returned {resp.status_code}"
        except requests.RequestException as e:
            # str(e) on a requests exception embeds the full URL, which is the
            # credential. Only the exception TYPE is reported.
            last = f"could not reach the receiver ({type(e).__name__})"
        except Exception as e:
            last = f"unexpected error sending the heartbeat ({type(e).__name__})"

        if attempt == 1:
            time.sleep(RETRY_DELAY_SECONDS)
    return False, last


def _mark_reported(db, sent_at):
    """Records that the most recent self-check result has been reported."""
    try:
        db.execute(
            "UPDATE self_check_log SET reported_at=? WHERE id = "
            "(SELECT id FROM self_check_log ORDER BY id DESC LIMIT 1)",
            (sent_at or datetime.now().isoformat(timespec="seconds"),),
        )
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def send_for(db, self_check_result):
    """Build and send in one call — what the scheduler uses."""
    return send(db, build_payload(db, self_check_result))
