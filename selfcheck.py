"""
Layer 1 of operational monitoring: the local self-check.

Answers one question — "is this install actually healthy right now?" — with
no internet and no external service. A clinic that never gets online still
needs it, so this module must never depend on Layer 2 (heartbeat) being
configured, or on the versioned-release layout existing.

Design rules, from features/MONITORING_FEATURE_PLAN.md §1:

* run_self_check() NEVER raises. It is called from a scheduler job and from
  the Dashboard; an exception in either place would be worse than the
  problem it is reporting.
* A check that cannot run records a finding rather than passing quietly.
  Silence is the exact failure this whole feature exists to prevent, so a
  check that skips itself is treated as a problem — the same rule
  scripts/restore_drill.sh follows.
* But "not applicable to this install" is not the same as "could not run".
  An install that does not use the in-app updater has no update log to
  read, permanently and by design. Reporting that as a warning every single
  day would be exactly the cry-wolf noise §6.0 warns about, so those checks
  return no finding. See _check_update_rolled_back().
* No "row counts look wrong" check. There is no baseline to compare against
  and it would fire on a quiet clinic.

This file is ALMOST identical to IQ's, and that is a verified result rather
than a copy-paste (CLAUDE.md §1, §2). **The exception is
consecutive_fail_days()**, which diverged in the original feature commit and
was only noticed on 2026-08-31: IQ picks each day's verdict by ran_at
timestamp, this version by insert order (the rows arrive id DESC). The two
agree whenever id order and ran_at order agree, which is always in normal
operation — so this is a robustness gap, not a live bug. IQ's version is the
better one and should be ported here; see COMPARISON.md §40.5. Every API it touches was
checked against JO's own code on 2026-08-26: logic.get_setting/int_setting,
backup.last_backup/recent_backups, the backup_log columns, and
updater.DATA_DIR all match IQ's exactly — DATA_DIR reads
VETCLINICSYSTEMJO_DATA_DIR rather than the IQ variable, but the attribute
this module reads has the same name, so no branch is needed. Nothing here
touches money, so the float/Decimal divergence (COMPARISON.md §1.1) does not
apply. **If either app's backup or settings API changes, re-derive rather
than re-copying.**
"""
import json
import os
import shutil
from datetime import datetime, timedelta

import logic

# Severity ordering, worst last — used to compute the overall status.
_RANK = {"ok": 0, "warn": 1, "fail": 2}

BACKUP_MAX_AGE_DEFAULT = 2      # days; overridable via selfcheck_backup_max_age_days
STRANDED_RUNNING_HOURS = 6      # matches logic.backup_alert_message()'s own threshold
RESTORE_VERIFY_MAX_AGE_DAYS = 45
DISK_WARN_BYTES = 2 * 1024 * 1024 * 1024   # 2 GB
DISK_FAIL_BYTES = 500 * 1024 * 1024        # 500 MB
LOG_RETENTION_ROWS = 180


def _finding(code, severity, message):
    return {"code": code, "severity": severity, "message": message}


def _parse_ts(value):
    """backup_log timestamps are TEXT. A malformed one must not take down
    the check that reads it."""
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Individual checks. Each returns a finding dict, or None when healthy.
# Each is passed the already-fetched context it needs so that one database
# round trip serves all of them.
# ---------------------------------------------------------------------------

def _check_backup_never(ctx):
    if ctx["last_backup"] is None:
        return _finding(
            "backup_never", "fail",
            "No database backup has ever run on this install.",
        )
    return None


def _check_backup_stale(ctx):
    # Skipped when nothing has ever run: backup_never already says it, and
    # two findings for one condition reads as two problems.
    if ctx["last_backup"] is None:
        return None
    row = ctx["last_success"]
    max_age = ctx["backup_max_age_days"]
    if row is None:
        return _finding(
            "backup_stale", "fail",
            "No backup has ever completed successfully.",
        )
    started = _parse_ts(row["started_at"])
    if started is None:
        return _finding(
            "backup_stale", "warn",
            "The last successful backup has an unreadable timestamp, so its "
            "age cannot be judged.",
        )
    age_days = (datetime.now() - started).days
    if age_days >= max_age:
        return _finding(
            "backup_stale", "fail",
            f"No successful backup for {age_days} day{'s' if age_days != 1 else ''}.",
        )
    return None


def _check_backup_failing(ctx):
    recent = ctx["recent_backups"][:3]
    if len(recent) >= 3 and all(r["status"] == "failed" for r in recent):
        return _finding(
            "backup_failing", "fail",
            "The last 3 backup attempts all failed: "
            f"{recent[0]['error'] or 'unknown error'}",
        )
    return None


def _check_backup_stranded(ctx):
    for row in ctx["recent_backups"]:
        if row["status"] != "running":
            continue
        started = _parse_ts(row["started_at"])
        if started is None:
            continue
        if (datetime.now() - started).total_seconds() > STRANDED_RUNNING_HOURS * 3600:
            return _finding(
                "backup_stranded", "warn",
                "A backup started but never finished — it has been running for "
                f"over {STRANDED_RUNNING_HOURS} hours.",
            )
    return None


def _check_backup_dir(ctx):
    """Covers both backup_dir_missing and backup_dir_unwritable — they are
    one question ("can we actually write a backup?") asked in two stages,
    and reporting both at once would double-count a single fault."""
    backup_dir = ctx["backup_dir"]
    if not backup_dir:
        return _finding(
            "backup_dir_missing", "fail",
            "No backup folder is configured — set one on the Settings page.",
        )
    if not os.path.isdir(backup_dir):
        # Creating it is right on a FIRST run -- the admin set a path and no
        # backup has been written there yet. It is wrong once backups have
        # been written there, because then the folder going missing means the
        # destination went away, and silently recreating it papers over
        # exactly the fault worth reporting: a synced folder (Drive/OneDrive)
        # that unlinked, or a folder someone moved. Backups would keep
        # "succeeding" into a fabricated local directory while the off-site
        # copy the clinic believes in quietly stopped.
        if ctx.get("backups_written_here"):
            return _finding(
                "backup_dir_missing", "fail",
                "The backup folder is gone. Backups were being written there, "
                "so this is a folder that disappeared rather than one not set "
                "up yet — check whether the drive or synced folder is still "
                "connected before anything writes a new one.",
            )
        try:
            os.makedirs(backup_dir, exist_ok=True)
        except OSError as e:
            return _finding(
                "backup_dir_missing", "fail",
                f"The backup folder does not exist and could not be created: {e.strerror}",
            )
    probe = os.path.join(backup_dir, ".selfcheck_write_probe")
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("probe")
        os.remove(probe)
    except OSError as e:
        return _finding(
            "backup_dir_unwritable", "fail",
            f"The backup folder exists but cannot be written to: {e.strerror}",
        )
    return None


def _check_disk_low(ctx):
    target = ctx["backup_dir"] if ctx["backup_dir"] and os.path.isdir(ctx["backup_dir"]) else os.getcwd()
    try:
        free = shutil.disk_usage(target).free
    except OSError as e:
        # This one genuinely could not run, as opposed to not applying.
        return _finding(
            "disk_low", "warn",
            f"Free disk space could not be read: {e.strerror}",
        )
    ctx["disk_free_bytes"] = free
    gb = free / (1024 ** 3)
    if free < DISK_FAIL_BYTES:
        return _finding("disk_low", "fail", f"Only {gb:.2f} GB free on the backup volume.")
    if free < DISK_WARN_BYTES:
        return _finding("disk_low", "warn", f"{gb:.1f} GB free on the backup volume.")
    return None


def _check_migration_failed(ctx):
    failures = ctx["migration_failures"]
    if failures and str(failures).strip():
        return _finding(
            "migration_failed", "fail",
            f"Some schema updates could not be applied on the last launch: {failures}",
        )
    return None


def _check_update_rolled_back(ctx):
    """Reads the updater's own log. NOT APPLICABLE — not a warning — on an
    install that does not use the versioned-release layout: updater.DATA_DIR
    is unset there, permanently and by design, so warning about it daily
    would be noise rather than signal (§6.0)."""
    try:
        import updater
    except Exception:
        return None
    if not getattr(updater, "DATA_DIR", None):
        return None
    log_path = os.path.join(updater.DATA_DIR, "logs", "updates.log")
    if not os.path.isfile(log_path):
        return None
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            lines = [ln.strip() for ln in fh.readlines() if ln.strip()]
    except OSError as e:
        return _finding(
            "update_rolled_back", "warn",
            f"The update log exists but could not be read: {e.strerror}",
        )
    if not lines:
        return None
    if "rollback" in lines[-1].lower() or "rolled back" in lines[-1].lower():
        return _finding(
            "update_rolled_back", "warn",
            "The most recent update was rolled back — this install is not "
            "running the version it tried to install.",
        )
    return None


def _check_restore_unverified(ctx):
    """Reads what Layer 4 (the self-verifying backup) records. Until a
    verification has ever run, this correctly reports that no backup on this
    install has been proven to restore — which is the true state, not a
    missing-feature artefact."""
    raw = ctx["last_verified_restore"]
    if not raw:
        return _finding(
            "restore_unverified", "warn",
            "No backup has ever been verified as restorable on this install.",
        )
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return _finding(
            "restore_unverified", "warn",
            "The last restore-verification result could not be read.",
        )
    when = _parse_ts(data.get("at"))
    if when is None:
        return _finding(
            "restore_unverified", "warn",
            "The last restore-verification result has no readable date.",
        )
    if data.get("result") != "pass":
        return _finding(
            "restore_unverified", "warn",
            "The most recent restore verification did not pass: "
            f"{data.get('detail') or 'no detail recorded'}",
        )
    if datetime.now() - when > timedelta(days=RESTORE_VERIFY_MAX_AGE_DAYS):
        days = (datetime.now() - when).days
        return _finding(
            "restore_unverified", "warn",
            f"No backup has been verified as restorable for {days} days.",
        )
    return None


def _check_backup_file_missing(ctx):
    """The newest successful backup must still exist on disk.

    Everything else here trusts backup_log, which lives in the database -- so
    every .dump file could be deleted and this feature would report `ok`.
    Layer 4 would catch it eventually, but its cadence is monthly, which is a
    long time to believe in backups that are not there. This is one
    os.path.isfile.
    """
    row = ctx["last_success"]
    if row is None:
        return None
    path = row["filepath"]
    if not path:
        return None
    if os.path.isfile(path):
        return None
    return _finding(
        "backup_file_missing", "fail",
        "The most recent backup is recorded as successful but its file is no "
        "longer on disk. Something removed it, or the folder it was written "
        "to is no longer the same folder.",
    )


_CHECKS = (
    _check_backup_never,
    _check_backup_stale,
    _check_backup_file_missing,
    _check_backup_failing,
    _check_backup_stranded,
    _check_backup_dir,
    _check_disk_low,
    _check_migration_failed,
    _check_update_rolled_back,
    _check_restore_unverified,
)


def _backups_written_here(recent, backup_dir):
    """True when a successful backup has been recorded inside backup_dir --
    i.e. the folder is an established destination, not one just configured."""
    if not backup_dir:
        return False
    try:
        target = os.path.abspath(backup_dir)
        for r in recent:
            if r["status"] != "success" or not r["filepath"]:
                continue
            if os.path.abspath(os.path.dirname(r["filepath"])) == target:
                return True
    except Exception:
        return False
    return False


def _gather(db):
    """One pass over everything the checks read."""
    import backup as backup_mod

    recent = list(backup_mod.recent_backups(db, limit=20))
    last_success = db.execute(
        "SELECT * FROM backup_log WHERE status='success' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return {
        "last_backup": backup_mod.last_backup(db),
        "last_success": last_success,
        "recent_backups": recent,
        "backup_dir": logic.get_setting(db, "backup_dir"),
        "backups_written_here": _backups_written_here(
            recent, logic.get_setting(db, "backup_dir")),
        "backup_max_age_days": logic.int_setting(
            db, "selfcheck_backup_max_age_days", BACKUP_MAX_AGE_DEFAULT
        ),
        "migration_failures": logic.get_setting(db, "migration_failures"),
        "last_verified_restore": logic.get_setting(db, "last_verified_restore"),
        "disk_free_bytes": None,
    }


def _drop_duplicated_cause(findings):
    """backup_failing quotes the error from the most recent attempt, so that
    on its own it says WHY backups are failing. When the cause is the backup
    folder itself, that quoted text and _check_backup_dir's own finding are
    the same paragraph in two wordings, and the banner printed both -- one
    fault, read twice. Reported 2026-08-31 off a real failing install.

    Only the quote is dropped, never the finding. "All three of the last
    attempts failed" is information the folder check does not carry: it is
    what says this is ongoing rather than a one-off, and it is what the
    3-day modal escalates on.

    Left untouched when backup_failing stands alone -- then the quoted error
    is the only explanation the admin gets, and dropping it would trade a
    repetition for a mystery. Same reasoning as _check_backup_dir's, which
    reports backup_dir_missing or backup_dir_unwritable but never both.
    """
    codes = {f["code"] for f in findings}
    if "backup_failing" not in codes:
        return findings
    if not codes & {"backup_dir_missing", "backup_dir_unwritable"}:
        return findings
    return [
        dict(f, message="The last 3 backup attempts all failed.")
        if f["code"] == "backup_failing" else f
        for f in findings
    ]


def run_self_check(db):
    """Returns {"status", "ran_at", "findings"[, "disk_free_bytes"]}.

    Never raises — see the module docstring.
    """
    ran_at = datetime.now().isoformat(timespec="seconds")

    # db_unreachable is checked first and on its own: if the database cannot
    # be read there is no point running checks that all read it, and their
    # individual failures would bury the one finding that explains them.
    try:
        db.execute("SELECT 1").fetchone()
    except Exception as e:
        return {
            "status": "fail",
            "ran_at": ran_at,
            "findings": [_finding("db_unreachable", "fail",
                                  f"The database could not be queried: {e}")],
        }

    try:
        ctx = _gather(db)
    except Exception as e:
        return {
            "status": "fail",
            "ran_at": ran_at,
            "findings": [_finding("db_unreachable", "fail",
                                  f"The database could not be read: {e}")],
        }

    findings = []
    for check in _CHECKS:
        try:
            result = check(ctx)
        except Exception as e:
            # A check that blew up is a check that did not run. Reporting it
            # is the whole point — see the module docstring.
            result = _finding(
                check.__name__.replace("_check_", ""), "warn",
                f"This check could not complete: {e}",
            )
        if result:
            findings.append(result)

    findings = _drop_duplicated_cause(findings)

    status = "ok"
    for f in findings:
        if _RANK[f["severity"]] > _RANK[status]:
            status = f["severity"]

    return {
        "status": status,
        "ran_at": ran_at,
        "findings": findings,
        "disk_free_bytes": ctx.get("disk_free_bytes"),
    }


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def record(db, result):
    """Writes one result to self_check_log and prunes to the retention limit.
    Never raises — a monitoring feature must not be able to break the app."""
    try:
        db.execute(
            "INSERT INTO self_check_log (ran_at, status, findings) VALUES (?,?,?)",
            (result["ran_at"], result["status"], json.dumps(result["findings"])),
        )
        db.execute(
            "DELETE FROM self_check_log WHERE id NOT IN ("
            "  SELECT id FROM self_check_log ORDER BY id DESC LIMIT ?"
            ")",
            (LOG_RETENTION_ROWS,),
        )
        db.commit()
        return True
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return False


def latest(db):
    try:
        return db.execute(
            "SELECT * FROM self_check_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except Exception:
        return None


def consecutive_fail_days(db):
    """How many distinct calendar days, ending today, the most recent check
    of each day reported 'fail'. Drives the Dashboard modal at 3 (§1.5).

    Counts days rather than rows so that a machine restarted six times in
    one morning does not escalate to a modal by lunchtime.
    """
    try:
        rows = db.execute(
            "SELECT ran_at, status FROM self_check_log ORDER BY id DESC LIMIT 400"
        ).fetchall()
    except Exception:
        return 0

    by_day = {}
    for row in rows:
        ts = _parse_ts(row["ran_at"])
        if ts is None:
            continue
        day = ts.date()
        # rows arrive newest-first, so the first seen per day is that day's latest
        by_day.setdefault(day, row["status"])

    if not by_day:
        return 0

    streak = 0
    day = max(by_day)
    while by_day.get(day) == "fail":
        streak += 1
        day = day - timedelta(days=1)
    return streak
