"""
Authentication, roles, audit trail, and discount-cap logic for Jordan Referral Center.
"""
import uuid
from datetime import datetime, timedelta
from functools import wraps

from flask import session, redirect, url_for, request, g, abort
from werkzeug.security import generate_password_hash, check_password_hash

ROLES = ["Admin", "Vet", "Reception"]

# Max discount percent each role is allowed to apply (server-enforced).
DISCOUNT_CAPS = {"Admin": 25, "Vet": 15, "Reception": 10}


def hash_password(raw):
    return generate_password_hash(raw)


def verify_password(hash_, raw):
    return check_password_hash(hash_, raw)


def new_user_id():
    return "U" + uuid.uuid4().hex[:8].upper()


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------
def current_user(db):
    uid = session.get("user_id")
    if not uid:
        return None
    return db.execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def roles_required(*allowed_roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("user_id"):
                return redirect(url_for("login", next=request.path))
            if session.get("role") not in allowed_roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


_DISCOUNT_CAP_SETTING_KEYS = {
    "discount_cap_admin": "Admin",
    "discount_cap_vet": "Vet",
    "discount_cap_reception": "Reception",
}


def get_discount_caps(db):
    """Discount caps per role, editable by Admin from Settings > Discount
    Limits. Any role without a value saved in the settings table falls back
    to the built-in default in DISCOUNT_CAPS, so this always returns all
    three roles even before Settings has ever been saved."""
    caps = dict(DISCOUNT_CAPS)
    rows = db.execute(
        "SELECT key, value FROM settings WHERE key IN (?,?,?)",
        tuple(_DISCOUNT_CAP_SETTING_KEYS.keys()),
    ).fetchall()
    for r in rows:
        role = _DISCOUNT_CAP_SETTING_KEYS.get(r["key"])
        if not role:
            continue
        try:
            caps[role] = int(r["value"])
        except (TypeError, ValueError):
            pass  # malformed/blank stored value — keep the default for this role
    return caps


def discount_cap_for(role, db):
    return get_discount_caps(db).get(role, 0)


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
    return f"{os_name} \u00b7 {browser}"


# ---------------------------------------------------------------------------
# Change / audit logging
# ---------------------------------------------------------------------------
def log_change(db, table_name, record_id, action, changes=None):
    """
    action: 'create' / 'update' / 'delete'
    changes: dict of {field: (old_value, new_value)} — only used for 'update'.
    For 'create'/'delete' pass changes=None; one row is written for the whole record.
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
    db.commit()


def diff_dict(old_row, new_values):
    """Build a {field: (old, new)} dict from a database row and a plain dict of new values."""
    changes = {}
    for k, new_v in new_values.items():
        old_v = old_row[k] if old_row and k in old_row.keys() else None
        if str(old_v) != str(new_v):
            changes[k] = (old_v, new_v)
    return changes
