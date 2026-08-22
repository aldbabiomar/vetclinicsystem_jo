"""
Authentication, roles, permissions, audit trail, and discount-cap logic for
VetClinicSystem JO.
"""
import uuid
from datetime import datetime, timedelta
from functools import wraps

from flask import session, redirect, url_for, request, abort
from werkzeug.security import generate_password_hash, check_password_hash

# ---------------------------------------------------------------------------
# Permissions — the app's fixed vocabulary of what *can* be gated. This list
# itself is not admin-editable (it's re-synced into the `permissions` table
# on every launch); which roles have which of these is what's editable, via
# the `role_permissions` table.
#
# (key, label, category) — grouped the same way the sidebar groups pages, so
# a role's checklist reads like a shorter version of the nav itself.
#
# Includes a few keys (manage_cash_register, view_consignment,
# manage_consignment_items, manage_consignment_stock,
# manage_consignment_settlements) for features this app doesn't have yet —
# kept in the vocabulary now so adding those features later is a permission
# grant, not a second permissions-list migration.
# ---------------------------------------------------------------------------
PERMISSIONS = [
    ("manage_owners", "Manage Owners", "Patients & Visits"),
    ("manage_patients", "Manage Patients", "Patients & Visits"),
    ("manage_visits", "Manage Visits", "Patients & Visits"),
    ("manage_followups", "Manage Follow-Ups", "Patients & Visits"),
    ("manage_wellness", "Manage Wellness Plans", "Patients & Visits"),
    ("manage_grooming", "Manage Grooming", "Patients & Visits"),
    ("manage_boarding", "Manage Boarding", "Patients & Visits"),
    ("manage_appointments", "Manage Appointments", "Patients & Visits"),
    ("manage_inpatient", "Manage Inpatient Cases", "Inpatient"),
    ("view_inventory_status", "View Inventory Status", "Inventory"),
    ("manage_ordering_sheet", "Manage Ordering Sheet", "Inventory"),
    ("manage_audit_history", "Manage Audit History", "Inventory"),
    ("manage_inventory_catalog", "Manage Inventory Catalog", "Inventory"),
    ("manage_distributors", "Manage Distributors", "Inventory"),
    ("process_pos_sales", "Process POS Sales", "Sales & Billing"),
    ("view_sales_history", "View Sales History", "Sales & Billing"),
    ("manage_price_list", "Manage Price List", "Sales & Billing"),
    ("manage_refunds", "Manage Refunds", "Sales & Billing"),
    ("manage_cash_register", "Manage Cash Register", "Sales & Billing"),
    ("view_financial_reports", "View Financial Reports", "Sales & Billing"),
    ("view_insights_retention", "View Insights & Retention", "Sales & Billing"),
    ("manage_users_roles", "Manage Users & Roles", "Admin"),
    ("manage_settings", "Manage Settings", "Admin"),
    ("view_logins_changes", "View Logins & Change Log", "Admin"),
    ("view_consignment", "View Consignment", "Consignment"),
    ("manage_consignment_items", "Manage Consignment Items", "Consignment"),
    ("manage_consignment_stock", "Log Receiving, Returns & Shrinkage", "Consignment"),
    ("manage_consignment_settlements", "Manage Settlements", "Consignment"),
]
PERMISSION_KEYS = [k for k, _, _ in PERMISSIONS]
PERMISSION_KEY_SET = set(PERMISSION_KEYS)

# The permissions that are (and always have been) Admin-only in this app —
# everything else was open to any logged-in user. Vet and Reception seed
# with every permission EXCEPT these, i.e. exactly their current effective
# access, just now expressed as editable checkboxes.
ADMIN_ONLY_TODAY = {
    "manage_price_list", "manage_refunds", "manage_cash_register", "view_financial_reports",
    "view_insights_retention", "manage_users_roles", "manage_settings",
    "view_logins_changes",
    "manage_consignment_settlements",
}
VET_RECEPTION_DEFAULT_PERMISSIONS = PERMISSION_KEY_SET - ADMIN_ONLY_TODAY

# Discount caps a brand-new install seeds Admin/Vet/Reception with. After
# that, each role's actual cap lives in roles.discount_cap and is editable
# from Settings.
DISCOUNT_CAPS = {"Admin": 25, "Vet": 15, "Reception": 10}


def hash_password(raw):
    return generate_password_hash(raw)


def verify_password(hash_, raw):
    return check_password_hash(hash_, raw)


# A real hash of an unguessable value, computed once at import time —
# login() checks the submitted password against this whenever the
# username doesn't exist (or the account is disabled), instead of
# short-circuiting straight to "no match". check_password_hash() is
# deliberately slow (scrypt/pbkdf2); skipping it for a nonexistent
# username makes that request return measurably faster than one for a
# real, active username, which is a timing side-channel an attacker can
# use to enumerate valid usernames one request at a time. Comparing
# against this either way keeps the two cases' timing the same.
_DUMMY_PASSWORD_HASH = generate_password_hash(uuid.uuid4().hex)


def new_user_id():
    return "U" + uuid.uuid4().hex[:8].upper()


def new_role_id():
    return "ROLE" + uuid.uuid4().hex[:8].upper()


def no_vet_role_configured(db):
    """
    True if zero roles are marked "can be assigned as a vet" — meaning
    Appointments, New Visit, Grooming, and Inpatient's vet pickers would
    all render with no options. Callers use this to surface a loud warning
    right when an admin's role edit/delete would cause it.
    """
    row = db.execute("SELECT COUNT(*) AS n FROM roles WHERE is_vet_role=true").fetchone()
    return row["n"] == 0


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------
def current_user(db):
    uid = session.get("user_id")
    if not uid:
        return None
    return db.execute("SELECT * FROM users WHERE id=? AND active=true", (uid,)).fetchone()


def permission_required(*perm_keys):
    """Gate a route behind one or more permission keys (any one matching is
    enough — pass a single key for the normal case). Reads from the
    session's cached permission set, which require_login()'s
    refresh_session_permissions() keeps in sync every request."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("user_id"):
                return redirect(url_for("login", next=request.path))
            granted = session.get("permissions") or []
            if not any(p in granted for p in perm_keys):
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def has_permission(perm_key):
    """True if the currently logged-in user's role grants this permission.
    Registered as a Jinja global so templates can do
    {% if has_permission('manage_owners') %}."""
    return perm_key in (session.get("permissions") or [])


# ---------------------------------------------------------------------------
# Roles & permissions — seeding and session refresh
# ---------------------------------------------------------------------------
def bump_permissions_version(db):
    """Call this after any change to a role's name/description/permissions/
    discount cap, a role being created or deleted, or a user's role_id/
    custom_discount_cap — it's the signal every other logged-in session
    checks (once per request, cheaply) to know its cached permission set is
    stale and needs reloading."""
    db.execute(
        "INSERT INTO settings (key, value) VALUES ('permissions_version', '1') "
        "ON CONFLICT (key) DO UPDATE SET value = (COALESCE(settings.value, '0')::int + 1)::text"
    )


def seed_default_roles_and_permissions(db):
    """Idempotent — safe to call on every launch. Keeps the `permissions`
    table (the app's fixed vocabulary) in sync with PERMISSIONS above, and
    creates Admin/Vet/Reception the first time only — never overwrites an
    admin's later edits to Vet or Reception, since those are ordinary
    custom roles from that point on."""
    for i, (key, label, category) in enumerate(PERMISSIONS):
        db.execute(
            "INSERT INTO permissions (id, label, category, sort_order) VALUES (?,?,?,?) "
            "ON CONFLICT (id) DO UPDATE SET label=EXCLUDED.label, category=EXCLUDED.category, "
            "sort_order=EXCLUDED.sort_order",
            (key, label, category, i),
        )

    defaults = [
        ("Admin", "Full access to every area of the app, always. There must be at least one active Admin.",
         True, DISCOUNT_CAPS["Admin"], set(PERMISSION_KEYS), False),
        ("Vet", "Clinical staff — patient care, visits, and inpatient cases.",
         False, DISCOUNT_CAPS["Vet"], VET_RECEPTION_DEFAULT_PERMISSIONS, True),
        ("Reception", "Front desk — scheduling, checkout, and client-facing tasks.",
         False, DISCOUNT_CAPS["Reception"], VET_RECEPTION_DEFAULT_PERMISSIONS, False),
    ]
    changed = False
    for name, desc, is_system, cap, perms, is_vet_role in defaults:
        existing = db.execute("SELECT id FROM roles WHERE name=?", (name,)).fetchone()
        if existing:
            continue
        role_id = new_role_id()
        db.execute(
            "INSERT INTO roles (id,name,description,is_system,discount_cap,is_vet_role,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (role_id, name, desc, is_system, cap, is_vet_role, datetime.now().isoformat(timespec="seconds")),
        )
        for perm in perms:
            db.execute(
                "INSERT INTO role_permissions (role_id, permission_id) VALUES (?,?) ON CONFLICT DO NOTHING",
                (role_id, perm),
            )
        changed = True
    if changed:
        bump_permissions_version(db)
    db.commit()


def refresh_session_permissions(db, user_row):
    """Called once per request for a logged-in user (from require_login()).
    Keeps session['permissions'], session['discount_cap'], and
    session['role'] in sync with the database — so if an admin changes this
    user's role, edits that role's permission checklist or discount cap, or
    sets/clears this user's personal discount override, it takes effect on
    the user's very next request rather than requiring them to log out and
    back in. Skips the extra permission-set query on every request when
    nothing has actually changed since it was last cached."""
    role_row = db.execute(
        "SELECT id, name, discount_cap, is_system FROM roles WHERE id=?",
        (user_row["role_id"],),
    ).fetchone()
    if not role_row:
        session.clear()
        return

    version_row = db.execute("SELECT value FROM settings WHERE key='permissions_version'").fetchone()
    current_version = version_row["value"] if version_row else "0"

    if (
        session.get("role_id") == role_row["id"]
        and session.get("_perm_version") == current_version
        and session.get("_cached_custom_cap") == user_row["custom_discount_cap"]
    ):
        return  # nothing relevant has changed since this was cached

    perm_rows = db.execute(
        "SELECT permission_id FROM role_permissions WHERE role_id=?", (role_row["id"],)
    ).fetchall()
    session["role_id"] = role_row["id"]
    session["role"] = role_row["name"]
    session["is_system_role"] = bool(role_row["is_system"])
    session["permissions"] = [p["permission_id"] for p in perm_rows]
    session["_cached_custom_cap"] = user_row["custom_discount_cap"]
    session["discount_cap"] = (
        user_row["custom_discount_cap"] if user_row["custom_discount_cap"] is not None else role_row["discount_cap"]
    )
    session["_perm_version"] = current_version


def discount_cap_for():
    """The current logged-in user's effective discount cap: their personal
    override if one is set, else their role's cap. Cached in
    session['discount_cap'] by refresh_session_permissions() so this never
    needs its own query."""
    return session.get("discount_cap", 0)


# ---------------------------------------------------------------------------
# Login attempt logging
# ---------------------------------------------------------------------------
def log_login(db, user_id, username, success):
    ua = request.headers.get("User-Agent", "")
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    db.execute(
        "INSERT INTO login_log (user_id, username, success, timestamp, ip, user_agent) VALUES (?,?,?,?,?,?)",
        (user_id, username, 1 if success else 0, datetime.now().isoformat(timespec="seconds"), ip, ua),
    )
    db.commit()


# ---------------------------------------------------------------------------
# Login rate limiting — locks a username out temporarily after repeated
# failed attempts, so a brute-force password guesser can't hammer an
# account indefinitely.
# ---------------------------------------------------------------------------
LOCKOUT_THRESHOLD = 5
LOCKOUT_WINDOW_MINUTES = 15


def login_lock_status(db, username):
    """Returns (locked: bool, minutes_remaining: int|None, unlock_at: datetime|None)."""
    if not username:
        return False, None, None
    cutoff = (datetime.now() - timedelta(minutes=LOCKOUT_WINDOW_MINUTES)).isoformat(timespec="seconds")
    row = db.execute(
        "SELECT COUNT(*) AS n, MAX(timestamp) AS last_at FROM login_log "
        "WHERE username=? AND success=0 AND timestamp >= ?",
        (username, cutoff),
    ).fetchone()
    if not row or row["n"] < LOCKOUT_THRESHOLD:
        return False, None, None
    last_at = datetime.fromisoformat(row["last_at"])
    unlock_at = last_at + timedelta(minutes=LOCKOUT_WINDOW_MINUTES)
    remaining = unlock_at - datetime.now()
    if remaining.total_seconds() <= 0:
        return False, None, None
    return True, max(1, int(remaining.total_seconds() // 60) + 1), unlock_at


def describe_device(user_agent):
    """Very small, dependency-free user-agent summary: 'Windows · Chrome' etc."""
    ua = (user_agent or "").lower()
    if "windows" in ua:
        os_name = "Windows"
    elif "mac os" in ua or "macintosh" in ua:
        os_name = "Mac"
    elif "iphone" in ua:
        os_name = "iPhone"
    elif "ipad" in ua:
        os_name = "iPad"
    elif "android" in ua:
        os_name = "Android"
    elif "linux" in ua:
        os_name = "Linux"
    else:
        os_name = "Unknown OS"

    if "edg/" in ua:
        browser = "Edge"
    elif "chrome/" in ua and "chromium" not in ua:
        browser = "Chrome"
    elif "crios" in ua:
        browser = "Chrome (iOS)"
    elif "fxios" in ua:
        browser = "Firefox (iOS)"
    elif "firefox/" in ua:
        browser = "Firefox"
    elif "safari/" in ua and "chrome/" not in ua:
        browser = "Safari"
    else:
        browser = "Unknown browser"
    return f"{os_name} · {browser}"


# ---------------------------------------------------------------------------
# Change / audit logging
# ---------------------------------------------------------------------------
def log_change(db, table_name, record_id, action, changes=None):
    """
    action: 'create' / 'update' / 'delete'
    changes: dict of {field: (old_value, new_value)} — only used for 'update'.
    For 'create'/'delete' pass changes=None; one row is written for the whole record.

    Deliberately does NOT commit. This write must land in the same
    transaction as the mutation it's describing, so the caller commits
    both together (or, on an exception, the app-context teardown rolls
    both back together). A route that calls this must therefore always
    reach its own db.commit() afterward.
    """
    uid = session.get("user_id")
    uname = session.get("username", "system")
    ts = datetime.now().isoformat(timespec="seconds")

    if action == "update" and changes:
        for field, (old, new) in changes.items():
            if str(old) == str(new):
                continue
            db.execute(
                "INSERT INTO audit_log (user_id,username,timestamp,action,table_name,record_id,field,old_value,new_value) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (uid, uname, ts, "update", table_name, record_id, field, old, new),
            )
    else:
        db.execute(
            "INSERT INTO audit_log (user_id,username,timestamp,action,table_name,record_id,field,old_value,new_value) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (uid, uname, ts, action, table_name, record_id, None, None, None),
        )


def diff_dict(old_row, new_values):
    """Build a {field: (old, new)} dict from a database row and a plain dict of new values."""
    changes = {}
    for k, new_v in new_values.items():
        old_v = old_row[k] if old_row and k in old_row.keys() else None
        if str(old_v) != str(new_v):
            changes[k] = (old_v, new_v)
    return changes
