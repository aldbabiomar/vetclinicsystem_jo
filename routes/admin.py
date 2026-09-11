"""
Users, roles and the change/login audit log.

Split out of app.py. Shared request-layer pieces come from core.py rather than
app.py -- app.py registers this blueprint, so importing from it here would be
circular.

Endpoint names carry the `admin.` prefix Flask gives every blueprint route:
`url_for("admin.admin_users")`, not `url_for("admin.admin_users")`.
"""

from datetime import date
from datetime import datetime
import auth
import logic

from flask_babel import gettext as _
from flask import (
    Blueprint, abort, flash, redirect, render_template, request, session, url_for
)

from core import date_filter_arg, get_db

bp = Blueprint("admin", __name__)


# ---------------------------------------------------------------------------
# Admin — user & role management
# ---------------------------------------------------------------------------
def _active_admin_count(db):
    return db.execute(
        "SELECT COUNT(*) c FROM users u JOIN roles r ON r.id = u.role_id "
        "WHERE u.active=true AND r.is_system=true"
    ).fetchone()["c"]


def _role_or_404(db, role_id):
    row = db.execute("SELECT * FROM roles WHERE id=?", (role_id,)).fetchone()
    if not row:
        abort(404)
    return row


def _future_appt_count(db, user_id):
    """How many upcoming appointments are booked against this user as the
    vet resource — day_grid() only builds columns for users whose role has
    is_vet_role=true, so any of these becomes unreachable from the grid
    the moment that stops being true for this user (a role change) or for
    their role itself (F-25). Shared by admin_user_toggle(),
    admin_user_role() (F-17), and admin_role_edit()/admin_role_delete()
    (F-25) so all four share one implementation."""
    return db.execute(
        "SELECT COUNT(*) c FROM appointments WHERE resource_type='vet' AND resource_id=? AND appt_date >= ?",
        (user_id, date.today().isoformat()),
    ).fetchone()["c"]


def _warn_orphaned_appointments(db, user_id):
    """Flashes a heads-up if moving this user off vet-eligibility just
    stranded upcoming appointments. See ORPHANED_RECORDS_AUDIT.md F-17."""
    n = _future_appt_count(db, user_id)
    if n:
        flash(f"Heads up: {n} upcoming appointment(s) were booked against this person — "
              f"they won't show on the Appointments grid anymore. Check Appointments for "
              f'the "need attention" list to reschedule them.', "error")


@bp.route("/admin/users")
@auth.permission_required("manage_users_roles")
def admin_users():
    db = get_db()
    users = db.execute(
        "SELECT u.*, r.name AS role_name, r.discount_cap AS role_discount_cap "
        "FROM users u JOIN roles r ON r.id = u.role_id ORDER BY u.full_name"
    ).fetchall()
    roles = db.execute("SELECT * FROM roles ORDER BY is_system DESC, created_at").fetchall()
    staff_counts = {
        r["role_id"]: r["c"] for r in
        db.execute("SELECT role_id, COUNT(*) c FROM users WHERE active=true GROUP BY role_id").fetchall()
    }
    role_perms = {}
    for rp in db.execute("SELECT role_id, permission_id FROM role_permissions").fetchall():
        role_perms.setdefault(rp["role_id"], set()).add(rp["permission_id"])

    perm_categories = []
    for cat in auth.PERMISSION_CATEGORIES:
        perm_categories.append((cat, [(k, label) for k, label, c in auth.PERMISSIONS if c == cat]))

    return render_template(
        "admin_users.html", users=users, roles=roles,
        staff_counts=staff_counts, role_perms=role_perms,
        perm_categories=perm_categories,
    )


@bp.route("/admin/users/new", methods=["POST"])
@auth.permission_required("manage_users_roles")
def admin_user_new():
    db = get_db()
    f = request.form
    username = f.get("username", "").strip()
    password = f.get("password", "")
    full_name = f.get("full_name", "").strip()
    role_id = f.get("role_id", "")
    role = db.execute("SELECT id FROM roles WHERE id=?", (role_id,)).fetchone()
    if not username or not full_name or not role:
        flash(_("Fill in a username, full name, and role."), "error")
        return redirect(url_for("admin.admin_users"))
    pw_error = auth.password_error(password, username)
    if pw_error:
        flash(pw_error, "error")
        return redirect(url_for("admin.admin_users"))
    if db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        flash(_("That username is already taken."), "error")
        return redirect(url_for("admin.admin_users"))

    custom_cap = None
    if f.get("capmode") == "custom":
        try:
            custom_cap = int(f.get("custom_discount_cap", ""))
        except ValueError:
            flash(_("Custom discount override must be a whole number."), "error")
            return redirect(url_for("admin.admin_users"))
        if custom_cap < 0 or custom_cap > 100:
            flash(_("Custom discount override must be between 0 and 100."), "error")
            return redirect(url_for("admin.admin_users"))

    uid = auth.new_user_id()
    db.execute(
        "INSERT INTO users (id,username,password_hash,full_name,role_id,custom_discount_cap,active,must_change_password,created_at) "
        "VALUES (?,?,?,?,?,?,true,true,?)",
        (uid, username, auth.hash_password(password), full_name, role_id, custom_cap,
         datetime.now().isoformat(timespec="seconds")),
    )
    auth.log_change(db, "users", uid, "create")
    db.commit()
    flash(_("User %(username)s created. They'll be asked to set a new password on first login.", username=username), "success")
    return redirect(url_for("admin.admin_users"))


@bp.route("/admin/users/<user_id>/toggle-active", methods=["POST"])
@auth.permission_required("manage_users_roles")
def admin_user_toggle(user_id):
    db = get_db()
    if user_id == session["user_id"]:
        flash(_("You can't disable your own account."), "error")
        return redirect(url_for("admin.admin_users"))
    row = db.execute(
        "SELECT u.active, r.is_system FROM users u JOIN roles r ON r.id = u.role_id WHERE u.id=?",
        (user_id,),
    ).fetchone()
    if row is None:
        flash(_("User not found."), "error")
        return redirect(url_for("admin.admin_users"))
    new_val = not row["active"]
    if new_val is False and row["is_system"] and _active_admin_count(db) <= 1:
        flash(_("Can't disable the last active Admin."), "error")
        return redirect(url_for("admin.admin_users"))
    db.execute("UPDATE users SET active=? WHERE id=?", (new_val, user_id))
    auth.bump_permissions_version(db)
    auth.log_change(db, "users", user_id, "update", {"active": (row["active"], new_val)})
    db.commit()
    flash(_("User updated."), "success")
    if new_val is False:
        _warn_orphaned_appointments(db, user_id)
    return redirect(url_for("admin.admin_users"))


@bp.route("/admin/users/<user_id>/role", methods=["POST"])
@auth.permission_required("manage_users_roles")
def admin_user_role(user_id):
    db = get_db()
    new_role_id = request.form.get("role_id", "")
    new_role = db.execute("SELECT id, name, is_system, is_vet_role FROM roles WHERE id=?", (new_role_id,)).fetchone()
    if not new_role:
        flash(_("Not a valid role."), "error")
        return redirect(url_for("admin.admin_users"))
    row = db.execute(
        "SELECT u.role_id, r.name AS role_name, r.is_system, r.is_vet_role FROM users u "
        "JOIN roles r ON r.id = u.role_id WHERE u.id=?",
        (user_id,),
    ).fetchone()
    if row is None:
        flash(_("User not found."), "error")
        return redirect(url_for("admin.admin_users"))
    if row["is_system"] and not new_role["is_system"] and _active_admin_count(db) <= 1:
        flash(_("Can't move the last active Admin out of the Admin role."), "error")
        return redirect(url_for("admin.admin_users"))
    db.execute("UPDATE users SET role_id=? WHERE id=?", (new_role_id, user_id))
    auth.bump_permissions_version(db)
    auth.log_change(db, "users", user_id, "update", {"role": (row["role_name"], new_role["name"])})
    db.commit()
    flash(_("Role updated."), "success")
    # admin_user_toggle() gets this right (moving a vet to a non-vet role
    # produces the identical day_grid() outcome — is_vet_role=true is what
    # decides whether a column is built for them) — this route used to say
    # nothing. Only warn when the *new* role isn't vet-eligible; moving
    # between two vet-eligible roles doesn't orphan anything. See
    # ORPHANED_RECORDS_AUDIT.md F-17.
    if row["is_vet_role"] and not new_role["is_vet_role"]:
        _warn_orphaned_appointments(db, user_id)
    return redirect(url_for("admin.admin_users"))


@bp.route("/admin/roles/new", methods=["POST"])
@auth.permission_required("manage_users_roles")
def admin_role_new():
    db = get_db()
    f = request.form
    name = f.get("name", "").strip()
    description = f.get("description", "").strip() or None
    if not name:
        flash(_("Give the new role a name."), "error")
        return redirect(url_for("admin.admin_users"))
    if db.execute("SELECT 1 FROM roles WHERE lower(name)=lower(?)", (name,)).fetchone():
        flash(f'A role named "{name}" already exists.', "error")
        return redirect(url_for("admin.admin_users"))
    try:
        cap = int(f.get("discount_cap", "0") or "0")
    except ValueError:
        flash(_("Max Discount must be a whole number."), "error")
        return redirect(url_for("admin.admin_users"))
    if cap < 0 or cap > 100:
        flash(_("Max Discount must be between 0 and 100."), "error")
        return redirect(url_for("admin.admin_users"))

    perms = [p for p in f.getlist("permissions") if p in auth.PERMISSION_KEY_SET]
    is_vet_role = bool(f.get("is_vet_role"))
    role_id = auth.new_role_id()
    db.execute(
        "INSERT INTO roles (id,name,description,is_system,discount_cap,is_vet_role,created_at) "
        "VALUES (?,?,?,false,?,?,?)",
        (role_id, name, description, cap, is_vet_role, datetime.now().isoformat(timespec="seconds")),
    )
    for p in perms:
        db.execute("INSERT INTO role_permissions (role_id, permission_id) VALUES (?,?)", (role_id, p))
    auth.bump_permissions_version(db)
    auth.log_change(db, "roles", role_id, "create", {"name": (None, name)})
    db.commit()
    flash(f'"{name}" role added.', "success")
    if auth.no_vet_role_configured(db):
        flash(_("No role is currently marked \"Can be assigned as a vet\" — Appointments, "
              "New Visit, Grooming, and Inpatient vet pickers will show no options until "
              "at least one role has this turned on."), "error")
    return redirect(url_for("admin.admin_users"))


@bp.route("/admin/roles/<role_id>/edit", methods=["POST"])
@auth.permission_required("manage_users_roles")
def admin_role_edit(role_id):
    db = get_db()
    role = _role_or_404(db, role_id)
    if role["is_system"]:
        flash(_("The Admin role can't be edited."), "error")
        return redirect(url_for("admin.admin_users"))
    f = request.form
    name = f.get("name", "").strip()
    description = f.get("description", "").strip() or None
    if not name:
        flash(_("A role needs a name."), "error")
        return redirect(url_for("admin.admin_users"))
    if db.execute("SELECT 1 FROM roles WHERE lower(name)=lower(?) AND id<>?", (name, role_id)).fetchone():
        flash(f'A role named "{name}" already exists.', "error")
        return redirect(url_for("admin.admin_users"))
    try:
        cap = int(f.get("discount_cap", "0") or "0")
    except ValueError:
        flash(_("Max Discount must be a whole number."), "error")
        return redirect(url_for("admin.admin_users"))
    if cap < 0 or cap > 100:
        flash(_("Max Discount must be between 0 and 100."), "error")
        return redirect(url_for("admin.admin_users"))

    perms = set(p for p in f.getlist("permissions") if p in auth.PERMISSION_KEY_SET)
    is_vet_role = bool(f.get("is_vet_role"))
    before = {
        "name": role["name"], "description": role["description"], "discount_cap": role["discount_cap"],
        "is_vet_role": role["is_vet_role"],
    }
    db.execute(
        "UPDATE roles SET name=?, description=?, discount_cap=?, is_vet_role=? WHERE id=?",
        (name, description, cap, is_vet_role, role_id),
    )
    db.execute("DELETE FROM role_permissions WHERE role_id=?", (role_id,))
    for p in perms:
        db.execute("INSERT INTO role_permissions (role_id, permission_id) VALUES (?,?)", (role_id, p))
    auth.bump_permissions_version(db)
    after = {"name": name, "description": description, "discount_cap": cap, "is_vet_role": is_vet_role}
    changes = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
    auth.log_change(db, "roles", role_id, "update", changes or None)
    db.commit()
    flash(f'"{name}" role saved.', "success")
    if auth.no_vet_role_configured(db):
        flash(_("No role is currently marked \"Can be assigned as a vet\" — Appointments, "
              "New Visit, Grooming, and Inpatient vet pickers will show no options until "
              "at least one role has this turned on."), "error")
    # Flipping a role's own is_vet_role flag affects every active user on
    # it at once, not just one person — admin_user_role()'s per-user
    # warning (F-17) doesn't fire here, since no single user's role_id
    # actually changed. Distinct finding: F-25 — the audit's original
    # framing called this IQ-only (JO had no custom-role feature at the
    # time it was written), but JO gained custom role creation in the
    # same-day parity pass, so this now applies here too. See
    # ORPHANED_RECORDS_AUDIT.md.
    if before["is_vet_role"] and not is_vet_role:
        affected = db.execute("SELECT id FROM users WHERE role_id=? AND active=true", (role_id,)).fetchall()
        total = sum(_future_appt_count(db, u["id"]) for u in affected)
        if total:
            flash(f"Heads up: {total} upcoming appointment(s) across {len(affected)} staff member(s) "
                  f"on this role won't show on the Appointments grid anymore. Check Appointments for "
                  f'the "need attention" list to reschedule them.', "error")
    return redirect(url_for("admin.admin_users"))


@bp.route("/admin/roles/<role_id>/delete", methods=["POST"])
@auth.permission_required("manage_users_roles")
def admin_role_delete(role_id):
    db = get_db()
    role = _role_or_404(db, role_id)
    if role["is_system"]:
        flash(_("The Admin role can't be deleted."), "error")
        return redirect(url_for("admin.admin_users"))
    assigned = db.execute("SELECT id FROM users WHERE role_id=?", (role_id,)).fetchall()
    reassign_to = request.form.get("reassign_to") or None
    if assigned:
        target = db.execute("SELECT id, name, is_system, is_vet_role FROM roles WHERE id=?", (reassign_to,)).fetchone()
        if not target or target["id"] == role_id:
            flash(_("Pick a role to move the affected staff to before deleting this one."), "error")
            return redirect(url_for("admin.admin_users"))
        for u in assigned:
            db.execute("UPDATE users SET role_id=? WHERE id=?", (target["id"], u["id"]))
        db.execute("DELETE FROM roles WHERE id=?", (role_id,))
        auth.bump_permissions_version(db)
        auth.log_change(db, "roles", role_id, "delete",
                         {"reassigned_to": (None, target["name"]), "staff_moved": (None, len(assigned))})
        db.commit()
        flash(f'{len(assigned)} staff member(s) moved to {target["name"]} · "{role["name"]}" deleted.', "success")
        # Same reasoning as admin_role_edit() above — reassigning every
        # user on a deleted vet-eligible role to a non-vet-eligible target
        # role affects them all at once. See ORPHANED_RECORDS_AUDIT.md F-25.
        if role["is_vet_role"] and not target["is_vet_role"]:
            total = sum(_future_appt_count(db, u["id"]) for u in assigned)
            if total:
                flash(f"Heads up: {total} upcoming appointment(s) across {len(assigned)} staff member(s) "
                      f"just moved off a vet-eligible role won't show on the Appointments grid anymore. "
                      f'Check Appointments for the "need attention" list to reschedule them.', "error")
    else:
        db.execute("DELETE FROM roles WHERE id=?", (role_id,))
        auth.bump_permissions_version(db)
        auth.log_change(db, "roles", role_id, "delete", {"name": (role["name"], None)})
        db.commit()
        flash(f'"{role["name"]}" deleted.', "success")
    if auth.no_vet_role_configured(db):
        flash(_("No role is currently marked \"Can be assigned as a vet\" — Appointments, "
              "New Visit, Grooming, and Inpatient vet pickers will show no options until "
              "at least one role has this turned on."), "error")
    return redirect(url_for("admin.admin_users"))


@bp.route("/admin/users/<user_id>/reset-password", methods=["POST"])
@auth.permission_required("manage_users_roles")
def admin_user_reset_password(user_id):
    db = get_db()
    new_pw = request.form.get("new_password", "")
    target = db.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
    pw_error = auth.password_error(new_pw, target["username"] if target else None)
    if pw_error:
        flash(pw_error, "error")
        return redirect(url_for("admin.admin_users"))
    # Also stamps password_changed_at so this reset immediately invalidates
    # any of this user's existing sessions elsewhere (see require_login())
    # — the whole point of an admin resetting a password (e.g. a suspected
    # compromised account) is that it takes effect now, not up to 12 hours
    # from now once that session's cookie happens to expire on its own.
    db.execute("UPDATE users SET password_hash=?, must_change_password=true, password_changed_at=? WHERE id=?",
               (auth.hash_password(new_pw), datetime.now().isoformat(timespec="seconds"), user_id))
    auth.log_change(db, "users", user_id, "update", {"password": ("(hidden)", "(reset by admin)")})
    db.commit()
    flash(_("Password reset. The user will be asked to set a new one on next login."), "success")
    return redirect(url_for("admin.admin_users"))


# ---------------------------------------------------------------------------
# Logins and Changes (admin-only audit page)
# ---------------------------------------------------------------------------
@bp.route("/admin/logs")
@auth.permission_required("view_logins_changes")
def admin_logs():
    db = get_db()
    # Validated, like every other date-filtered list page in this app --
    # through date_filter_arg(), the helper the others already use. This one
    # took the raw value: `?date=` (present but empty) or `?date=garbage`
    # reached changes_on_date()/logins_on_date(), which compare it as a text
    # timestamp prefix, so nothing matched and the page rendered an EMPTY log
    # with no explanation -- indistinguishable from "nobody did anything that
    # day", on the one screen whose whole job is showing what happened. See
    # SEAM_RULES.md.
    day = date_filter_arg("date", "That date wasn't valid — showing today instead.") \
        or date.today().isoformat()
    changes = logic.changes_on_date(db, day)
    logins = logic.logins_on_date(db, day)
    return render_template("admin_logs.html", day=day, today=date.today().isoformat(), changes=changes, logins=logins)
