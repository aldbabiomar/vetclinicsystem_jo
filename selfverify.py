"""
Layer 4 of operational monitoring: the self-verifying backup.

Without this, the rest of the feature reports on a backup file's *existence*,
which is worth very little. A truncated dump, a correctly-sized file of random
bytes, and a structurally perfect archive containing zero rows all look
identical on disk — and the third restores cleanly, keeps every foreign key,
and lets the app boot. All three are useless. This was demonstrated against
scripts/restore_drill.sh; see COMPARISON.md §23.3.

So once a month this restores the newest backup into a throwaway database and
asks whether what came back is actually a clinic's records.

A Python port of the essential checks in scripts/restore_drill.sh, with one
deliberate difference: the drill restores into a throwaway *container*, which
needs Docker. This restores into a throwaway *database on the same Postgres
server the app is already talking to*, so it works on a clinic machine where
nobody has Docker permissions.

GUARD RAILS — these are the whole reason this is safe to run unattended:

* It NEVER touches the live database. The only statements issued against the
  live server are CREATE DATABASE and DROP DATABASE for the throwaway, and
  every check query runs on a separate connection to the throwaway itself.
* It NEVER deletes or modifies a backup file. It only reads one.
* The throwaway database is dropped in a `finally`, always — including on
  timeout, on a failed restore, and on an unexpected exception.
* A hard timeout (10 minutes). On timeout: record a warning, drop the
  database, move on.
* If pg_restore is unavailable it records a WARNING, not a silent pass. A
  check that skips quietly reports as success, which is exactly the failure
  this whole feature exists to prevent.

MONEY — the one place the IQ/JO divergence shows up in this feature
(CLAUDE.md §1, COMPARISON.md §1.1). This is JO: billing.total must come back
as `numeric`, and no bill may carry more than 3 decimal places, because the
JOD's fils subunit is in everyday real use and NUMERIC(12,3) is what stores
it. The column TYPE is the thing that silently destroys money here — a JO
backup restoring numeric as double precision would keep every value looking
right today and lose fils on the next write.

IQ's copy of this file asserts deliberately OPPOSITE things (`double
precision`, and every non-zero bill a whole multiple of 250 IQD) and the two
must never be reconciled — the same rule test_money.py already follows.
"""
import json
import os
import secrets
import shutil
import subprocess
from datetime import datetime
from urllib.parse import quote

import psycopg

import backup as backup_mod
import logic

RESTORE_TIMEOUT_SECONDS = 600
SETTING_KEY = "last_verified_restore"

# How often the verification should actually run. Deliberately shorter than
# selfcheck.RESTORE_VERIFY_MAX_AGE_DAYS (45), so a couple of missed runs — a
# machine switched off, a clinic closed for a week — do not trip the
# restore_unverified warning. The gap between the two IS the tolerance.
VERIFY_INTERVAL_DAYS = 30

# --- JO's money model. See the module docstring. ---
MONEY_EXPECTED_TYPE = "numeric"
MAX_DECIMAL_PLACES = 3

CORE_TABLES = ("users", "owners", "patients", "visits", "billing",
               "sales", "price_list", "inventory_list")


def _check(name, ok, detail=""):
    return {"name": name, "ok": bool(ok), "detail": detail}


def _dsn_for(dbname):
    user, password, _appdb, host, port = backup_mod._pg_conn_parts()
    auth = quote(user, safe="")
    if password:
        auth += ":" + quote(password, safe="")
    return f"postgresql://{auth}@{host}:{port}/{dbname}"


def _newest_successful_backup(db):
    row = db.execute(
        "SELECT * FROM backup_log WHERE status='success' AND filepath IS NOT NULL "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row


def _run_pg_restore_into(dbname, dump_path):
    """Restore dump_path into an EXISTING empty database. Raises on failure.

    Deliberately no --clean/--if-exists: the target is a database this module
    just created, so there is nothing to drop, and passing --clean would be
    one typo away from being pointed at something real.
    """
    user, password, _appdb, host, port = backup_mod._pg_conn_parts()
    env = backup_mod._pg_env(password)

    if shutil.which("pg_restore"):
        cmd = ["pg_restore", "-w", "-h", host, "-p", port, "-U", user,
               "-d", dbname, "--no-owner", "--no-privileges", dump_path]
        subprocess.run(cmd, check=True, env=env, capture_output=True, text=True,
                       timeout=RESTORE_TIMEOUT_SECONDS)
        return

    container = os.environ.get("VETCLINICSYSTEMJO_PG_CONTAINER", "vetclinicsystemjo_postgres")
    if shutil.which("docker"):
        container_path = "/tmp/selfverify_" + os.path.basename(dump_path)
        subprocess.run(["docker", "cp", dump_path, f"{container}:{container_path}"],
                       check=True, capture_output=True, text=True,
                       timeout=RESTORE_TIMEOUT_SECONDS)
        try:
            cmd = ["docker", "exec", "-e", "PGPASSWORD", container,
                   "pg_restore", "-w", "-U", user, "-d", dbname,
                   "--no-owner", "--no-privileges", container_path]
            subprocess.run(cmd, check=True, env=env, capture_output=True, text=True,
                           timeout=RESTORE_TIMEOUT_SECONDS)
        finally:
            subprocess.run(["docker", "exec", container, "rm", "-f", container_path],
                           capture_output=True, text=True)
        return

    raise RuntimeError("neither pg_restore nor docker is available")


def _run_checks(con):
    """Every check runs against the THROWAWAY database's connection."""
    checks = []

    def scalar(sql):
        with con.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
            return row[0] if row else None

    tables = scalar("SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='public'")
    checks.append(_check("schema restored", (tables or 0) > 0,
                         f"{tables} tables"))

    fks = scalar("SELECT count(*) FROM information_schema.table_constraints "
                 "WHERE constraint_type='FOREIGN KEY' AND table_schema='public'")
    checks.append(_check("foreign keys present", (fks or 0) > 0,
                         f"{fks} foreign keys"))

    total_rows = 0
    for t in CORE_TABLES:
        if scalar(f"SELECT to_regclass('public.{t}') IS NOT NULL"):
            total_rows += scalar(f"SELECT count(*) FROM {t}") or 0
    checks.append(_check("core tables populated", total_rows > 0,
                         f"{total_rows} rows across the core tables"))

    accounts = 0
    if scalar("SELECT to_regclass('public.users') IS NOT NULL"):
        accounts = scalar("SELECT count(*) FROM users WHERE role_id IS NOT NULL") or 0
    checks.append(_check("someone could log in", accounts > 0,
                         f"{accounts} user account(s)"))

    # The single most important integrity question: does every child row still
    # point at a parent that exists? A dump restored out of order, or with a
    # constraint dropped, shows up here and nowhere else.
    orphans = None
    if scalar("SELECT to_regclass('public.patients') IS NOT NULL"):
        orphans = scalar(
            "SELECT count(*) FROM patients p LEFT JOIN owners o ON o.id=p.owner_id "
            "WHERE p.owner_id IS NOT NULL AND o.id IS NULL")
    if orphans is None:
        # The table is absent, so the question could not be asked. Saying
        # "None orphaned" would render a FAILING check as a clean result.
        checks.append(_check("no orphaned patients", False,
                             "the patients table is missing from this backup"))
    else:
        checks.append(_check("no orphaned patients", orphans == 0,
                             "none" if orphans == 0 else f"{orphans} orphaned"))

    # --- money ---
    # A missing billing table is a FINDING, not a reason to skip quietly: a
    # check that silently skips reports as a pass.
    if not scalar("SELECT to_regclass('public.billing') IS NOT NULL"):
        checks.append(_check("money verified", False,
                             "no billing table — money could not be verified at all"))
        return checks

    money_type = scalar("SELECT data_type FROM information_schema.columns "
                        "WHERE table_name='billing' AND column_name='total'")
    if not money_type:
        checks.append(_check("money verified", False,
                             "billing.total is missing — this backup cannot "
                             "reproduce what anyone was charged"))
        return checks

    checks.append(_check(
        f"billing.total is {MONEY_EXPECTED_TYPE}",
        money_type == MONEY_EXPECTED_TYPE,
        f"restored as '{money_type}'",
    ))

    # JO only: nothing may carry more precision than NUMERIC(12,3) stores. A
    # value with more decimals than that came back from somewhere other than
    # this app's own arithmetic, and would be silently truncated on write.
    #
    # The ::numeric cast is load-bearing, not tidiness. scale() accepts only
    # numeric, so on the one input this check exists to catch — a backup whose
    # total came back as double precision — the bare call RAISES. That aborted
    # the whole verification and reported "warn" (could not run), throwing away
    # the type-check failure that had just been recorded and turning the single
    # most important negative result in this layer into a shrug. Caught by
    # test_a_backup_whose_money_column_is_the_wrong_type_fails.
    over = scalar(
        f"SELECT count(*) FROM billing WHERE scale(total::numeric) > {MAX_DECIMAL_PLACES}")
    checks.append(_check(
        f"no bill exceeds {MAX_DECIMAL_PLACES} decimal places (1 fils)",
        over == 0,
        "all within 3 decimals" if over == 0 else f"{over} bill(s) exceed it",
    ))
    return checks


def verify_latest_backup(db):
    """Restore the newest successful backup into a throwaway database and check
    what came back. Returns {"at", "result", "detail", "checks"}.

    result is "pass", "fail", or "warn" — "warn" meaning the verification could
    not be performed at all, which is never treated as a pass.

    Never raises.
    """
    at = datetime.now().isoformat(timespec="seconds")

    def out(result, detail, checks=None):
        return {"at": at, "result": result, "detail": detail, "checks": checks or []}

    try:
        row = _newest_successful_backup(db)
    except Exception as e:
        return out("warn", f"could not read the backup log: {e}")

    if row is None:
        return out("warn", "no successful backup to verify yet")

    dump_path = row["filepath"]
    if not dump_path or not os.path.isfile(dump_path):
        return out("warn", "the newest successful backup is no longer on disk")

    if not (shutil.which("pg_restore") or shutil.which("docker")):
        return out("warn", "pg_restore is not available on this machine, so the "
                           "backup could not be test-restored")

    # Hex only, so the identifier can never need quoting or carry an injection.
    temp_db = "selfverify_" + secrets.token_hex(4)
    created = False
    admin = None
    try:
        # Connected to the app's own database purely to issue CREATE/DROP
        # DATABASE for the throwaway — no application table is read or written
        # through this connection. CREATE DATABASE cannot run inside a
        # transaction, hence autocommit.
        admin = psycopg.connect(_dsn_for(backup_mod._pg_conn_parts()[2]), autocommit=True)
        admin.execute(f'CREATE DATABASE "{temp_db}"')
        created = True

        try:
            _run_pg_restore_into(temp_db, dump_path)
        except subprocess.TimeoutExpired:
            return out("warn", f"the test restore did not finish within "
                               f"{RESTORE_TIMEOUT_SECONDS // 60} minutes")
        except subprocess.CalledProcessError as e:
            detail = (e.stderr or "").strip().splitlines()
            return out("fail", "the backup could not be restored: "
                               + (detail[-1] if detail else "pg_restore failed"))

        with psycopg.connect(_dsn_for(temp_db)) as con:
            # A check that RAISES must not escape to the handler below, where
            # it would be reported as "warn — could not run". The inputs most
            # likely to make a check raise are exactly the malformed backups
            # this layer exists to reject, so an exception here is evidence
            # AGAINST the backup, not an inconclusive result. This is not
            # hypothetical: scale(double precision) raising is what made the
            # wrong-money-type case report "warn" instead of "fail".
            try:
                checks = _run_checks(con)
            except Exception as e:
                checks = [_check("all checks completed", False,
                                 f"a check could not run against the restored "
                                 f"data: {e}")]

        failed = [c for c in checks if not c["ok"]]
        if failed:
            return out("fail", "; ".join(f"{c['name']}: {c['detail']}" for c in failed),
                       checks)
        return out("pass", f"{len(checks)} checks passed", checks)

    except Exception as e:
        return out("warn", f"the verification could not run: {e}")

    finally:
        # Always, including on timeout and on an unexpected exception. FORCE
        # detaches any connection still attached to the throwaway; without it a
        # half-finished pg_restore can hold the database open and leak it.
        if created:
            try:
                if admin is None or admin.closed:
                    admin = psycopg.connect(
                        _dsn_for(backup_mod._pg_conn_parts()[2]), autocommit=True)
                try:
                    admin.execute(f'DROP DATABASE IF EXISTS "{temp_db}" WITH (FORCE)')
                except Exception:
                    # WITH (FORCE) needs PostgreSQL 13+.
                    admin.execute(f'DROP DATABASE IF EXISTS "{temp_db}"')
            except Exception:
                pass
        if admin is not None:
            try:
                admin.close()
            except Exception:
                pass


def record(db, result):
    """Stores the result where selfcheck's restore_unverified finding and the
    heartbeat payload both read it. Never raises."""
    payload = {"at": result["at"], "result": result["result"],
               "detail": result["detail"]}
    try:
        db.execute(
            "INSERT INTO settings (key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (SETTING_KEY, json.dumps(payload)),
        )
        db.commit()
        return True
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return False


def is_due(db, max_age_days=VERIFY_INTERVAL_DAYS):
    """True when a verification should run now: never run, unreadable, or
    older than max_age_days. Never raises — when in doubt, say yes.

    This exists because a monthly CRON is the wrong shape for this job. A
    trigger set to "the 1st at 02:45" simply does not happen on a machine that
    is switched off that night, and APScheduler will not run it late — so a
    clinic that closes on the 1st, or powers its machine down overnight at all,
    would never verify a backup and would then warn about it forever. Checking
    daily and doing the work only when due is robust to any single night being
    missed, and it also means a FRESH install verifies as soon as it has its
    first backup instead of warning every day until the 1st comes around.
    """
    try:
        raw = logic.get_setting(db, SETTING_KEY)
    except Exception:
        return True
    if not raw:
        return True
    try:
        data = json.loads(raw)
        when = datetime.fromisoformat(str(data.get("at")))
    except (TypeError, ValueError):
        return True
    if data.get("result") != "pass":
        # A previous failure retries DAILY rather than waiting for the full
        # interval: the likely fix is simply the next night's backup, and
        # waiting 30 days to re-test would blow past
        # selfcheck.RESTORE_VERIFY_MAX_AGE_DAYS (45) with the clinic warned
        # the whole time. Bounded to once a day so a persistently broken
        # backup is not re-restored every tick.
        return (datetime.now() - when).days >= 1
    return (datetime.now() - when).days >= max_age_days


def run_and_record(db):
    result = verify_latest_backup(db)
    record(db, result)
    return result


def run_if_due(db):
    """What the scheduler calls. Returns the result, or None if not due."""
    try:
        if not is_due(db):
            return None
    except Exception:
        pass
    return run_and_record(db)
