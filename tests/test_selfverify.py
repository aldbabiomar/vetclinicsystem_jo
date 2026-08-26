"""
Layer 4 of operational monitoring — the self-verifying backup.

The point of this layer is that a backup FILE existing proves nothing, so the
point of these tests is that verify_latest_backup() actually distinguishes a
good backup from a bad one. The three deliberately-broken backups below are
the ones scripts/restore_drill.sh was validated against (COMPARISON.md §23.3),
and the third is the important one:

  * a truncated file
  * a correctly-sized file of random bytes
  * a STRUCTURALLY PERFECT archive containing no rows — which restores
    cleanly, keeps every foreign key, and would let the app boot

All three must fail verification. A layer that passes the third one is worse
than no layer at all, because it reports a reassuring "verified" for a backup
that would lose the entire clinic.

Equally important, and easy to get wrong: a verification that could not RUN
(no backup yet, no pg_restore) must report "warn" and never "pass".
"""
import json
import os
import shutil
import subprocess
from datetime import datetime

import pytest

from conftest import needs_db

pytestmark = needs_db

pg_tools = pytest.mark.skipif(
    not (shutil.which("pg_dump") and shutil.which("pg_restore")),
    reason="pg_dump/pg_restore not on PATH — this layer cannot be exercised",
)


def _log_backup(db, path, status="success"):
    now = datetime.now().isoformat(timespec="seconds")
    db.execute(
        "INSERT INTO backup_log (started_at, finished_at, status, filepath) "
        "VALUES (?,?,?,?)",
        (now, now, status, path),
    )
    db.commit()


@pytest.fixture
def clean_backup_log(db):
    saved = db.execute("SELECT * FROM backup_log ORDER BY id").fetchall()
    saved_setting = db.execute(
        "SELECT value FROM settings WHERE key='last_verified_restore'").fetchone()
    db.execute("DELETE FROM backup_log")
    db.commit()
    yield db
    db.execute("DELETE FROM backup_log")
    for row in saved:
        db.execute(
            "INSERT INTO backup_log (id, started_at, finished_at, status, filepath, "
            "filesize_bytes, error, triggered_by) VALUES (?,?,?,?,?,?,?,?)",
            (row["id"], row["started_at"], row["finished_at"], row["status"],
             row["filepath"], row["filesize_bytes"], row["error"], row["triggered_by"]),
        )
    if saved_setting:
        db.execute(
            "INSERT INTO settings (key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("last_verified_restore", saved_setting["value"]),
        )
    else:
        db.execute("DELETE FROM settings WHERE key='last_verified_restore'")
    db.commit()


def _real_dump(tmp_path, name="good.dump"):
    """A real backup of the live test database, taken the way the app takes
    one — not a hand-built file."""
    import backup as backup_mod
    out = str(tmp_path / name)
    backup_mod._run_pg_dump(out)
    return out


def _throwaway_databases():
    """Every selfverify_* database currently on the server. Used to prove the
    throwaway is always dropped."""
    import psycopg
    import selfverify
    import backup as backup_mod
    dsn = selfverify._dsn_for(backup_mod._pg_conn_parts()[2])
    with psycopg.connect(dsn, autocommit=True) as con, con.cursor() as cur:
        cur.execute("SELECT datname FROM pg_database WHERE datname LIKE 'selfverify_%%'")
        return {r[0] for r in cur.fetchall()}


# --- cannot-run cases: must warn, must never pass ------------------------

def test_no_backup_yet_warns_and_does_not_pass(clean_backup_log):
    import selfverify
    result = selfverify.verify_latest_backup(clean_backup_log)
    assert result["result"] == "warn"
    assert result["result"] != "pass"


def test_backup_file_gone_from_disk_warns(clean_backup_log, tmp_path):
    import selfverify
    _log_backup(clean_backup_log, str(tmp_path / "vanished.dump"))
    result = selfverify.verify_latest_backup(clean_backup_log)
    assert result["result"] == "warn"
    assert "no longer on disk" in result["detail"]


def test_a_failed_backup_row_is_not_verified(clean_backup_log, tmp_path):
    """Only a SUCCESSFUL backup is a candidate. Verifying a failed one would
    report a real failure for a file that was never claimed to be good."""
    import selfverify
    _log_backup(clean_backup_log, str(tmp_path / "nope.dump"), status="failed")
    result = selfverify.verify_latest_backup(clean_backup_log)
    assert result["result"] == "warn"
    assert "no successful backup" in result["detail"]


# --- the real thing ------------------------------------------------------

@pg_tools
def test_a_real_backup_verifies_and_passes(clean_backup_log, tmp_path):
    import selfverify
    before = _throwaway_databases()
    _log_backup(clean_backup_log, _real_dump(tmp_path))

    result = selfverify.verify_latest_backup(clean_backup_log)
    assert result["result"] == "pass", result["detail"]
    assert result["checks"], "a pass with no checks recorded would be meaningless"
    assert all(c["ok"] for c in result["checks"])

    names = {c["name"] for c in result["checks"]}
    assert "no orphaned patients" in names
    assert any("billing.total is" in n for n in names), (
        "the money-type check must have actually run — it is the one place "
        "the IQ/JO divergence shows up in this feature"
    )
    assert _throwaway_databases() == before, "the throwaway database was not dropped"


@pg_tools
def test_the_money_check_asserts_this_app_s_own_model(clean_backup_log, tmp_path):
    """JO: numeric, 3 decimals. IQ's copy asserts double precision and the
    250-IQD note rule. If these two files are ever reconciled into one, this
    fails in whichever app is wrong — the same guard test_money.py already
    carries."""
    import selfverify
    assert selfverify.MONEY_EXPECTED_TYPE == "numeric"
    assert selfverify.MAX_DECIMAL_PLACES == 3
    assert not hasattr(selfverify, "DENOMINATION"), (
        "IQ's 250-IQD note rounding has no meaning in JOD and must never be "
        "ported into JO — see COMPARISON.md §1.1"
    )

    _log_backup(clean_backup_log, _real_dump(tmp_path))
    result = selfverify.verify_latest_backup(clean_backup_log)
    names = {c["name"] for c in result["checks"]}
    assert "billing.total is numeric" in names
    assert "no bill exceeds 3 decimal places (1 fils)" in names


def _dump_of_scratch_db(tmp_path, money_type, name="wrongmoney.dump"):
    """Build a minimal but STRUCTURALLY VALID database whose only fault is the
    money column's type, dump it, and drop it. Every other check this layer
    runs is arranged to pass, so a failure isolates the money assertion.
    """
    import psycopg
    import secrets
    import backup as backup_mod
    import selfverify

    user, password, appdb, host, port = backup_mod._pg_conn_parts()
    scratch = "wrongmoney_" + secrets.token_hex(4)
    admin = psycopg.connect(selfverify._dsn_for(appdb), autocommit=True)
    out = str(tmp_path / name)
    try:
        admin.execute(f'CREATE DATABASE "{scratch}"')
        with psycopg.connect(selfverify._dsn_for(scratch), autocommit=True) as con:
            con.execute("CREATE TABLE owners (id TEXT PRIMARY KEY)")
            con.execute("CREATE TABLE patients (id TEXT PRIMARY KEY, "
                        "owner_id TEXT REFERENCES owners(id))")
            con.execute("CREATE TABLE users (id TEXT PRIMARY KEY, role_id TEXT)")
            con.execute(f"CREATE TABLE billing (id TEXT PRIMARY KEY, total {money_type})")
            con.execute("INSERT INTO owners VALUES ('O1')")
            con.execute("INSERT INTO patients VALUES ('P1','O1')")
            con.execute("INSERT INTO users VALUES ('U1','admin')")
            con.execute("INSERT INTO billing VALUES ('B1', 500)")
        env = backup_mod._pg_env(password)
        subprocess.run(
            ["pg_dump", "-w", "-h", host, "-p", port, "-U", user,
             "-F", "c", "-f", out, scratch],
            check=True, env=env, capture_output=True, text=True,
        )
    finally:
        try:
            admin.execute(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')
        except Exception:
            admin.execute(f'DROP DATABASE IF EXISTS "{scratch}"')
        admin.close()
    return out


@pg_tools
def test_a_backup_whose_money_column_is_the_wrong_type_fails(clean_backup_log, tmp_path):
    """The money-type check must be able to FAIL, not merely to run.

    DOUBLE PRECISION is IQ's money model, and correct there — restored into JO
    it is wrong, and it is the kind of wrong that looks fine today and loses
    fils on the next write. Nothing else in this layer would notice.
    """
    import selfverify
    path = _dump_of_scratch_db(tmp_path, "DOUBLE PRECISION")
    _log_backup(clean_backup_log, path)

    before = _throwaway_databases()
    result = selfverify.verify_latest_backup(clean_backup_log)
    assert result["result"] == "fail", (
        "a backup whose billing.total is double precision must not verify in "
        "JO: %r" % result["detail"]
    )
    failed = {c["name"] for c in result["checks"] if not c["ok"]}
    assert "billing.total is numeric" in failed, (
        "the money-type check was not the thing that failed — %r" % failed
    )
    assert _throwaway_databases() == before


@pg_tools
def test_the_control_the_same_backup_with_the_right_money_type_passes(
        clean_backup_log, tmp_path):
    """Without this, 'failed for the wrong money type' and 'failed for any
    reason at all' are indistinguishable — the two are separated only by
    changing the one thing under test."""
    import selfverify
    path = _dump_of_scratch_db(tmp_path, "NUMERIC(12,3)", name="rightmoney.dump")
    _log_backup(clean_backup_log, path)

    result = selfverify.verify_latest_backup(clean_backup_log)
    assert result["result"] == "pass", (
        "the identical database with JO's own money type must verify: %r"
        % result["detail"]
    )


# --- the three deliberately-broken backups -------------------------------

@pg_tools
def test_a_truncated_backup_fails(clean_backup_log, tmp_path):
    import selfverify
    path = _real_dump(tmp_path, "truncated.dump")
    size = os.path.getsize(path)
    with open(path, "r+b") as fh:
        fh.truncate(size // 3)
    _log_backup(clean_backup_log, path)

    before = _throwaway_databases()
    result = selfverify.verify_latest_backup(clean_backup_log)
    assert result["result"] != "pass", "a truncated dump must never verify"
    assert _throwaway_databases() == before, (
        "the throwaway database must be dropped even when the restore fails"
    )


@pg_tools
def test_a_file_of_random_bytes_fails(clean_backup_log, tmp_path):
    import selfverify
    good = _real_dump(tmp_path, "sized.dump")
    size = os.path.getsize(good)
    path = str(tmp_path / "random.dump")
    with open(path, "wb") as fh:
        fh.write(os.urandom(size))
    _log_backup(clean_backup_log, path)

    before = _throwaway_databases()
    result = selfverify.verify_latest_backup(clean_backup_log)
    assert result["result"] != "pass"
    assert _throwaway_databases() == before


@pg_tools
def test_a_structurally_perfect_but_empty_backup_fails(clean_backup_log, tmp_path):
    """THE important one. This file restores cleanly, keeps every foreign key,
    and would let the app boot — it just contains no rows. On disk it is
    indistinguishable from a good backup: same name, plausible size, and it
    would be listed happily in Settings.
    """
    import backup as backup_mod
    import selfverify
    user, password, dbname, host, port = backup_mod._pg_conn_parts()
    env = backup_mod._pg_env(password)
    path = str(tmp_path / "schema_only.dump")
    subprocess.run(
        ["pg_dump", "-w", "-h", host, "-p", port, "-U", user,
         "--schema-only", "-F", "c", "-f", path, dbname],
        check=True, env=env, capture_output=True, text=True,
    )
    _log_backup(clean_backup_log, path)

    before = _throwaway_databases()
    result = selfverify.verify_latest_backup(clean_backup_log)
    assert result["result"] == "fail", (
        "a schema-only backup restores perfectly and contains nothing — it "
        "must not be reported as verified: %r" % result["detail"]
    )
    failed = {c["name"] for c in result["checks"] if not c["ok"]}
    assert "core tables populated" in failed
    assert _throwaway_databases() == before


# --- recording -----------------------------------------------------------

def test_record_writes_where_selfcheck_reads_it(clean_backup_log):
    import selfverify
    import selfcheck
    db = clean_backup_log
    result = {"at": datetime.now().isoformat(timespec="seconds"),
              "result": "pass", "detail": "test", "checks": []}
    assert selfverify.record(db, result) is True

    raw = db.execute(
        "SELECT value FROM settings WHERE key='last_verified_restore'").fetchone()
    assert json.loads(raw["value"])["result"] == "pass"

    # The contract between the two layers: a fresh pass clears the finding.
    sc = selfcheck.run_self_check(db)
    assert "restore_unverified" not in {f["code"] for f in sc["findings"]}


def test_a_failed_verification_is_recorded_and_reported_by_selfcheck(clean_backup_log):
    import selfverify
    import selfcheck
    db = clean_backup_log
    selfverify.record(db, {"at": datetime.now().isoformat(timespec="seconds"),
                           "result": "fail", "detail": "core tables populated: 0 rows",
                           "checks": []})
    sc = selfcheck.run_self_check(db)
    findings = {f["code"]: f for f in sc["findings"]}
    assert "restore_unverified" in findings
    assert "did not pass" in findings["restore_unverified"]["message"]
