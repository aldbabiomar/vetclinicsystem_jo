"""
Settings, and the installation-maintenance actions behind it.

The ordinary clinic settings (clinic name, appointment hours, alert windows)
sit behind `manage_settings`. Everything that administers the INSTALL rather
than the clinic -- backups, restore, updates, autostart, and the folder picker
they share -- sits behind `manage_maintenance`, so that the Settings page's own
gate and the server's gate are the same condition. They were not, until
2026-09-10: the page hid these controls while every route behind them accepted
a `manage_settings` holder's request.

Split out of app.py, which was ~7,200 lines. Shared request-layer pieces come
from core.py rather than app.py -- app.py registers this blueprint, so
importing from it here would be circular.

Endpoint names carry the `settings.` prefix Flask gives every blueprint route:
`url_for("settings.settings_page")`, not `url_for("settings.settings_page")`.
"""
import os
from datetime import datetime

from flask_babel import gettext as _
from flask import (
    Blueprint, flash, g, jsonify, redirect, render_template, request, session, url_for
)

import auth
import db as dbmod
import jobs
import logic
from core import DATA_DIR as _data_dir, VERSION, get_db, lan_address

bp = Blueprint("settings", __name__)


def _browse_roots(db):
    """The only directories the Settings folder/file pickers may browse.

    This endpoint used to resolve whatever ?path= it was given with
    os.path.abspath() and list it, with no confinement at all -- so any
    logged-in user who could reach it could enumerate /, /etc, /Users and
    /var/log on the clinic machine. It still has to browse the *server's*
    disk (pg_dump/pg_restore run there, see the docstring on
    api_browse_folder), so the fix is a root, not removal.
    """
    roots = []
    for candidate in (os.path.expanduser("~"),
                      logic.get_setting(db, "backup_dir"),
                      _data_dir):
        if candidate and os.path.isdir(candidate):
            real = os.path.realpath(candidate)
            if real not in roots:
                roots.append(real)
    return roots


def _within_roots(path, roots):
    """True if `path` is one of `roots` or lives beneath one.

    commonpath(), never startswith(): '/Users/omar-evil' must not pass a
    '/Users/omar' check. realpath() first so a symlink cannot step outside
    a root either.
    """
    real = os.path.realpath(path)
    for root in roots:
        try:
            if os.path.commonpath([real, root]) == root:
                return True
        except ValueError:
            # Different drives on Windows -- not comparable, so not inside.
            continue
    return False


def _outside_roots_error(roots):
    where = ", ".join(roots) if roots else "the backup folder"
    return jsonify({"error": f"That folder is outside the areas this app can browse ({where})."}), 400


@bp.route("/api/browse-folder")
@auth.permission_required("manage_maintenance")
def api_browse_folder():
    """
    Lists subfolders (and, when ?ext= is given, matching files too) of a
    path on THIS SERVER's filesystem — used by both the Backup Folder
    picker and the Restore File picker on Settings. This has to browse the
    server's disk, not the browser's — pg_dump/pg_restore (see backup.py)
    run on the server, so a client-side file picker (which can only see
    the browser's own machine) would pick the wrong computer's files
    entirely whenever Settings is opened from a different machine than
    the one running the app.
    """
    db = get_db()
    requested = request.args.get("path", "").strip()
    ext = (request.args.get("ext") or "").strip().lower()
    if requested:
        path = os.path.abspath(requested)
    else:
        configured = logic.get_setting(db, "backup_dir")
        path = os.path.abspath(configured) if configured and os.path.isdir(configured) else os.path.expanduser("~")

    if not os.path.isdir(path):
        return jsonify({"error": f"“{path}” isn’t a folder VetClinicSystem JO can see on this computer."}), 400

    roots = _browse_roots(db)
    if not _within_roots(path, roots):
        return _outside_roots_error(roots)

    try:
        entries = os.listdir(path)
    except OSError as e:
        return jsonify({"error": f"Can’t open that folder: {e.strerror or e}"}), 400

    folders, files = [], []
    for name in entries:
        if name.startswith("."):
            continue
        full = os.path.join(path, name)
        if os.path.isdir(full) and not os.path.islink(full):
            folders.append(name)
        elif ext and os.path.isfile(full) and name.lower().endswith(ext):
            files.append(name)
    folders.sort(key=str.lower)
    files.sort(key=str.lower)

    parent = os.path.dirname(path)
    if parent == path or not _within_roots(parent, roots):
        parent = None
    return jsonify({
        "current": path,
        "parent": parent,
        "folders": folders,
        "files": files,
    })


@bp.route("/api/browse-folder/new-folder", methods=["POST"])
@auth.permission_required("manage_maintenance")
def api_browse_folder_new():
    data = request.get_json(silent=True) or {}
    parent = os.path.abspath((data.get("path") or "").strip())
    name = (data.get("name") or "").strip()
    if not name or "/" in name or "\\" in name:
        return jsonify({"error": "Enter a plain folder name (no slashes)."}), 400
    if not os.path.isdir(parent):
        return jsonify({"error": "That parent folder no longer exists."}), 400
    roots = _browse_roots(get_db())
    if not _within_roots(parent, roots):
        return _outside_roots_error(roots)
    new_path = os.path.join(parent, name)
    try:
        os.makedirs(new_path, exist_ok=True)
    except OSError as e:
        return jsonify({"error": f"Couldn’t create that folder: {e.strerror or e}"}), 400
    return jsonify({"ok": True, "path": new_path})


# ---------------------------------------------------------------------------
# Settings (Admin only)
# ---------------------------------------------------------------------------
@bp.route("/settings", methods=["GET", "POST"])
@auth.permission_required("manage_settings")
def settings_page():
    db = get_db()
    if request.method == "POST":
        # (field, min, max) — keeps schedule generation and alert windows sane.
        # Settings whose VALUE must never be written to the audit log. The
        # fact that they changed is the auditable part.
        SECRET_SETTING_KEYS = {"heartbeat_url"}

        def _secret_state(v):
            return "set" if (v or "").strip() else "not set"

        NUMERIC_RANGES = {
            "audit_overdue_days": (1, 3650),
            "expiry_soon_days": (1, 3650),
            "appt_slot_minutes": (5, 240),
            "backup_retention": (1, 3650),
            # Backups are nightly, so one missed night is noise and two is a
            # pattern. Capped at 30: a threshold beyond that is indistinguishable
            # from switching the check off, which selfcheck_enabled already does
            # honestly.
            "selfcheck_backup_max_age_days": (1, 30),
            # Floor of 90 days is deliberate and load-bearing: auth
            # .login_lock_status() reads login_log to decide whether an
            # account is locked out, so pruning inside that window would
            # silently disarm the lockout.
            "log_retention_days": (logic.LOG_RETENTION_MIN_DAYS, logic.LOG_RETENTION_MAX_DAYS),
        }
        for key, (lo, hi) in NUMERIC_RANGES.items():
            val = request.form.get(key)
            if val is None or val.strip() == "":
                continue
            try:
                n = int(val)
            except ValueError:
                flash(_("%(title)s must be a whole number.", title=key.replace('_', ' ').title()), "error")
                return redirect(url_for("settings.settings_page"))
            if n < lo or n > hi:
                flash(_("%(title)s must be between %(lo)s and %(hi)s.", title=key.replace('_', ' ').title(), lo=lo, hi=hi), "error")
                return redirect(url_for("settings.settings_page"))

        # Time-of-day fields — validated as real HH:MM before anything else
        # touches them. appt_start_time/appt_end_time feed straight into
        # logic.generate_slots()'s datetime.strptime(..., "%H:%M") (used by
        # Appointments, New Visit, Grooming, and Inpatient's vet pickers),
        # and backup_time feeds scheduler.reschedule()'s CronTrigger — an
        # unvalidated value there doesn't just break one page, it can raise
        # at the next app *startup* (scheduler.start() runs unguarded before
        # the server starts serving), making the whole app fail to launch
        # until someone fixes the row directly in the database. The <input
        # type="time"> in the template stops this in the normal UI, but
        # that's client-side only, so it's validated here too. See
        # ERROR_500_AUDIT.md E-01/E-02.
        # `language` drives which catalogue every page renders from, and its
        # value reaches Flask-Babel directly. A whitelist rather than trusting
        # the form, same reasoning as every other settings field validated
        # here — and an unknown locale would otherwise fall back silently,
        # which reads as "the setting did not save".
        SUPPORTED_LANGUAGES = ("en", "ar")
        lang_val = request.form.get("language")
        if lang_val is not None and lang_val not in SUPPORTED_LANGUAGES:
            flash(_("Not a valid language."), "error")
            return redirect(url_for("settings.settings_page"))

        TIME_FIELDS = ["appt_start_time", "appt_end_time", "backup_time"]
        for key in TIME_FIELDS:
            val = request.form.get(key)
            if val is None or val.strip() == "":
                continue
            try:
                datetime.strptime(val.strip(), "%H:%M")
            except ValueError:
                flash(_("%(title)s must be a valid time (HH:MM).", title=key.replace('_', ' ').title()), "error")
                return redirect(url_for("settings.settings_page"))

        start = request.form.get("appt_start_time")
        end = request.form.get("appt_end_time")
        if start and end and start >= end:
            flash(_("Day Ends At must be after Day Starts At."), "error")
            return redirect(url_for("settings.settings_page"))

        # The heartbeat URL is a credential — for a healthchecks.io-style
        # receiver, anyone holding it can send a fake ping and so SUPPRESS a
        # real alert. Plain http would put it in the clear on the clinic LAN,
        # so it is https or nothing. Empty is valid and means "disabled",
        # which is the default.
        hb_url = request.form.get("heartbeat_url")
        if hb_url is not None and hb_url.strip() and not hb_url.strip().lower().startswith("https://"):
            flash(_("The monitoring ping URL must start with https://"), "error")
            return redirect(url_for("settings.settings_page"))
        # Snapshot before the change — appt_start_time/appt_end_time/
        # appt_slot_minutes feed generate_slots(), which day_grid() (and
        # logic.orphaned_appointments()) key every appointment's slot_label
        # against. Comparing the orphaned count before/after this save is
        # how we know whether *this specific change* just stranded any
        # existing bookings, without hand-duplicating the slot-generation
        # logic here to simulate it separately.
        orphaned_before = len(logic.orphaned_appointments(db))
        for key in ["clinic_name", "clinic_location", "audit_overdue_days", "expiry_soon_days", "opening_date",
                    "appt_start_time", "appt_end_time", "appt_slot_minutes",
                    "backup_dir", "backup_time", "backup_retention", "language",
                    "selfcheck_backup_max_age_days", "heartbeat_url", "log_retention_days"]:
            val = request.form.get(key)
            if val is not None:
                old = logic.get_setting(db, key)
                db.execute(
                    "INSERT INTO settings (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, val),
                )
                if old != val:
                    # heartbeat_url is a credential: anyone holding it can send
                    # a fake ping and suppress the alert that fires when this
                    # machine goes dark. log_change writes values into
                    # audit_log, which is readable by view_logins_changes — a
                    # broader permission than manage_settings — and appears in
                    # audit exports. Record THAT it changed, never the value.
                    if key in SECRET_SETTING_KEYS:
                        auth.log_change(db, "settings", key, "update",
                                        {key: (_secret_state(old), _secret_state(val))})
                    else:
                        auth.log_change(db, "settings", key, "update", {key: (old, val)})
        # selfcheck_enabled is a checkbox, and an unchecked box submits
        # nothing at all — so it cannot go through the loop above, where a
        # missing key means "left alone". It would switch on and never off.
        # The hidden companion field is what distinguishes "this form was
        # submitted and the box was clear" from "this form doesn't have the
        # field", e.g. a POST from an older cached page.
        if request.form.get("selfcheck_present"):
            val = "1" if request.form.get("selfcheck_enabled") else "0"
            old = logic.get_setting(db, "selfcheck_enabled")
            db.execute(
                "INSERT INTO settings (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("selfcheck_enabled", val),
            )
            if old != val:
                auth.log_change(db, "settings", "selfcheck_enabled", "update",
                                {"selfcheck_enabled": (old, val)})
        db.commit()
        if request.form.get("backup_time"):
            import scheduler
            scheduler.reschedule(request.form.get("backup_time"))
        flash(_("Settings saved."), "success")
        newly_orphaned = len(logic.orphaned_appointments(db)) - orphaned_before
        if newly_orphaned > 0:
            flash(f"Heads up: changing the scheduling hours/slot length just made {newly_orphaned} upcoming "
                  f"appointment(s) stop matching a slot on the grid. They're still booked — check "
                  f"Appointments for the \"need attention\" list to reschedule them.", "error")
        return redirect(url_for("settings.settings_page"))
    rows = db.execute("SELECT * FROM settings").fetchall()
    settings = {r["key"]: r["value"] for r in rows}
    import backup as backup_mod
    import autostart
    # An 'in_progress' marker that was never updated to 'success'/'failed'
    # means the process died mid-restore — the database may be in a
    # partially restored state. See ORPHANED_RECORDS_AUDIT.md F-20.
    restore_marker = backup_mod.read_restore_marker()
    incomplete_restore = bool(restore_marker and restore_marker.get("status") == "in_progress")
    return render_template(
        "settings.html", settings=settings, lan_address=lan_address(),
        recent_backups=backup_mod.recent_backups(db),
        recent_restores=backup_mod.recent_restores(db),
        autostart_supported=autostart.is_supported(),
        autostart_enabled=autostart.is_enabled(),
        incomplete_restore=incomplete_restore,
        app_version=VERSION,
    )


@bp.route("/settings/backup-now", methods=["POST"])
@auth.permission_required("manage_maintenance")
def settings_backup_now():
    import backup as backup_mod

    # Checked here, before a job is started, rather than only inside
    # run_backup(): otherwise clicking Back Up Now with no folder set spins up
    # a progress panel that runs through its steps and then reports failure,
    # which reads as "the backup broke" rather than "you haven't set this up
    # yet". Nothing to do here is not an error worth a job.
    if not logic.get_setting(get_db(), "backup_dir"):
        return jsonify({"error": "No backup folder configured yet — set one above, "
                                 "then Save Settings, before backing up."}), 400

    def task(update):
        # Runs in a background thread — needs its own DB connection,
        # since g.db belongs to this request and gets closed at request
        # teardown long before a background thread finishes. Also keeps
        # run_backup() from committing on the request's own connection,
        # which would otherwise commit any other pending write this
        # request happened to have made as a side effect of taking a
        # backup. See ORPHANED_RECORDS_AUDIT.md F-24.
        conn = dbmod.connect()
        try:
            ok, message = backup_mod.run_backup(conn, triggered_by="manual", on_progress=update)
            return {"ok": ok, "message": message}
        finally:
            conn.close()

    job_id = jobs.start(
        ["Checking backup folder", "Dumping database", "Applying retention policy", "Done"],
        task,
    )
    return jsonify({"job_id": job_id})


@bp.route("/settings/restore-now", methods=["POST"])
@auth.permission_required("manage_maintenance")
def settings_restore_now():
    source_file = (request.form.get("source_file") or "").strip()
    import backup as backup_mod

    # Path confinement + provenance check — only a .dump file inside the
    # configured backup folder AND recorded in this app's own backup_log
    # as a successful backup can be restored. Runs on the request's own
    # (still-open) connection, before that connection is released and
    # before any restore work starts, so an invalid/unauthorized path
    # never gets anywhere near pg_restore.
    ok, resolved_source, message = backup_mod.resolve_restorable_backup(get_db(), source_file)
    if not ok:
        flash(message, "error")
        return redirect(url_for("settings.settings_page"))

    # pg_restore --clean issues DROP TABLE (and similar) against every
    # table in the database — including ones this very request already
    # touched, like `users` via require_login()'s lookup a moment ago.
    # Release this request's own connection back to the pool first so it
    # isn't still holding a read lock on those tables when pg_restore
    # tries to drop them.
    conn = g.pop("db", None)
    if conn is not None:
        conn.commit()
        dbmod.putconn(conn)

    # The restore is about to DROP and recreate every table — close the
    # whole pool so no other pooled-but-idle connection (e.g. a second
    # admin's open tab) is left holding cached plans/catalog snapshots
    # across it. getconn() transparently reopens a fresh pool on the next
    # request.
    dbmod.close_pool()

    def task(update):
        ok, message = backup_mod.run_restore(dbmod.connect, resolved_source,
                                              triggered_by="manual", on_progress=update)
        return {"ok": ok, "message": message}

    job_id = jobs.start(
        ["Checking backup file", "Restoring database", "Reconciling schema", "Recording result", "Done"],
        task,
    )
    return jsonify({"job_id": job_id})


@bp.route("/settings/job-status")
@auth.permission_required("manage_maintenance")
def settings_job_status():
    """Polled by the progress panel on the Updates section, and by Backup
    Now / Restore Now."""
    job_id = request.args.get("job_id", "")
    kind = request.args.get("kind", "")
    state = jobs.status(job_id)
    if state is None:
        return jsonify({"status": "not_found"}), 404
    payload = {
        "status": state["status"],
        "steps": state["steps"],
        "current": state["current"],
        "fraction": state.get("fraction"),
        "started_at": state["started_at"],
    }
    if state["status"] == "done":
        result = state.get("result") or {}
        payload["ok"] = result.get("ok")
        payload["message"] = result.get("message")
        if kind == "restore" and result.get("ok"):
            # The restore just replaced every row in the database,
            # including `users` — force a fresh login on this browser
            # rather than leaving a session tied to data that may no
            # longer match what's actually there now.
            session.clear()
    elif state["status"] == "error":
        payload["message"] = state.get("error")
    return jsonify(payload)


@bp.route("/settings/autostart", methods=["POST"])
@auth.permission_required("manage_maintenance")
def settings_autostart():
    import autostart
    enable = request.form.get("autostart_enabled") == "on"
    ok, message = autostart.enable() if enable else autostart.disable()
    flash(message, "success" if ok else "error")
    return redirect(url_for("settings.settings_page"))


@bp.route("/settings/updates/status")
@auth.permission_required("manage_maintenance")
def settings_updates_status():
    """Everything the Settings page needs to DRAW the updates card, and
    nothing that needs the network.

    This exists because the page used to call /settings/updates/check on
    load, which asks GitHub for the latest release — to render two facts
    that are both local: whether updates are set up, and which version is
    running. GitHub allows 60 unauthenticated API calls per hour per IP
    address, shared by every install behind it, so opening Settings often
    enough silently spent the clinic's quota. The cost landed on the "Check
    for Updates" button, the one place the call is actually wanted, which
    then reported the clinic as offline. COMPARISON.md §46.

    Keep this route free of network calls. If it ever needs to know
    something only GitHub can answer, that is a sign the answer belongs
    behind the button instead.
    """
    import updater
    configured = updater.is_configured()
    return jsonify({
        "configured": configured,
        "current_version": updater.current_version() if configured else VERSION,
    })


@bp.route("/settings/updates/check")
@auth.permission_required("manage_maintenance")
def settings_updates_check():
    import updater
    if not updater.is_configured():
        return jsonify({"configured": False, "current_version": VERSION})
    try:
        available, latest = updater.is_update_available()
    except Exception as exc:
        return jsonify({"configured": True, "current_version": updater.current_version(),
                         "error": updater.describe_check_failure(exc)}), 502
    return jsonify({
        "configured": True,
        "current_version": updater.current_version(),
        "available": available,
        "latest_tag": latest.get("tag_name"),
        "latest_body": latest.get("body"),
    })


@bp.route("/settings/updates/apply", methods=["POST"])
@auth.permission_required("manage_maintenance")
def settings_updates_apply():
    import updater
    if not updater.is_configured():
        return jsonify({"error": "Updates aren't set up on this install yet."}), 400
    try:
        available, latest = updater.is_update_available()
    except Exception as exc:
        return jsonify({"error": updater.describe_check_failure(exc)}), 502
    if not available:
        return jsonify({"error": "Already on the latest version."}), 400
    tag_name, tarball_url = latest.get("tag_name"), latest.get("tarball_url")

    def task(update):
        ok, message = updater.apply_update(tag_name, tarball_url, on_progress=update)
        return {"ok": ok, "message": message}

    job_id = jobs.start(
        ["Backing up database", "Downloading release", "Validating release",
         "Applying database changes", "Verifying the new version", "Switching to the new version"],
        task,
    )
    return jsonify({"job_id": job_id})


@bp.route("/settings/updates/rollback", methods=["POST"])
@auth.permission_required("manage_maintenance")
def settings_updates_rollback():
    import updater
    if not updater.is_configured():
        return jsonify({"error": "Updates aren't set up on this install yet."}), 400
    candidates = [n for n in updater.list_releases() if n != updater.active_release_name()]
    if not candidates:
        return jsonify({"error": "No previous release available to roll back to."}), 400

    def task(update):
        update(0)
        ok, message = updater.rollback_to_previous()
        return {"ok": ok, "message": message}

    job_id = jobs.start(["Rolling back"], task)
    return jsonify({"job_id": job_id})
