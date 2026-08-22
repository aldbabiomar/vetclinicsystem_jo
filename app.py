import ipaddress
import json
import os
import re
import signal
import socket
import sys
import time
import logging
import logging.handlers
import traceback
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
load_dotenv()

from flask import (
    Flask, render_template, request, redirect, url_for, flash, g, jsonify,
    session, send_from_directory, send_file, abort
)
from flask.json.provider import DefaultJSONProvider
from flask_wtf import CSRFProtect
from werkzeug.exceptions import HTTPException

import logic
import auth
import db as dbmod
import barcode as barcode_mod
import attachments as attach_mod
import pdf_export

BASE_DIR = os.path.dirname(__file__)

class _DecimalJSONProvider(DefaultJSONProvider):
    """Flask's default JSON provider has no idea what a Decimal is (it only
    special-cases datetime/UUID/dataclass/Markup) and raises TypeError the
    moment jsonify() sees one — every money value read back from the
    database is now a Decimal (see parse_money() for why). Converted to
    float here, once, at the JSON boundary only: JSON/JS have no exact
    decimal type anyway, and this is a one-way trip out to the browser for
    display, not a value that gets computed with server-side afterward."""
    @staticmethod
    def default(o):
        if isinstance(o, Decimal):
            return float(o)
        return DefaultJSONProvider.default(o)


app = Flask(__name__)
app.json = _DecimalJSONProvider(app)
app.secret_key = os.environ.get("SECRET_KEY")
if not app.secret_key:
    raise SystemExit(
        "SECRET_KEY is not set. Copy .env.example to .env (setup.py does this "
        "for you) before starting the app."
    )
csrf = CSRFProtect(app)

# ---------------------------------------------------------------------------
# Network/session hardening — this app binds to every interface on the LAN
# by default (see serve() at the bottom of this file), which is fine for a
# single-clinic deployment as long as it's paired with real compensating
# controls. None of this changes default behavior for an operator who
# doesn't configure anything: every knob below is opt-in via environment
# variable, same as .env.example already does for SECRET_KEY etc.
# ---------------------------------------------------------------------------

# If a reverse proxy (nginx/Caddy/etc) is terminating TLS in front of this
# app, set BEHIND_TLS_PROXY=1 so Flask (a) trusts the proxy's
# X-Forwarded-For/X-Forwarded-Proto/X-Forwarded-Host headers for the real
# client IP and scheme instead of the proxy's own, and (b) marks the
# session cookie Secure (browsers refuse to send Secure cookies over plain
# HTTP, so this must stay off for a plain-HTTP LAN deployment — Waitress
# itself doesn't terminate TLS, by its own design, so TLS here always means
# "there's a reverse proxy in front", never "pass Waitress a certificate").
BIND_PORT = int(os.environ.get("VETCLINICSYSTEMJO_PORT", "5050"))
BEHIND_TLS_PROXY = os.environ.get("BEHIND_TLS_PROXY") == "1"
if BEHIND_TLS_PROXY:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Explicit session cookie policy (previously unset, relying entirely on
# Flask's framework defaults with no visibility into what those were).
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = BEHIND_TLS_PROXY
# Previously unset entirely: a login session had no server-enforced
# expiry at all — only "until the browser drops the cookie", which
# doesn't happen on a front-desk machine where the browser is routinely
# left open for an entire shift or longer. session.permanent is set at
# successful login (see login() below) so this actually takes effect.
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(
    hours=float(os.environ.get("SESSION_LIFETIME_HOURS", "12"))
)

# Optional network allowlist: comma-separated CIDR blocks (e.g.
# "192.168.1.0/24,10.0.0.5/32"). Unset by default — no behavior change
# for a normal single-router clinic LAN. Lets an operator whose network
# is bigger/flatter than that (e.g. one shared VLAN with other, unrelated
# devices) restrict which source addresses can reach the app at all,
# independent of and in addition to login/permissions.
_ALLOWED_NETWORKS = []
for _cidr in os.environ.get("VETCLINICSYSTEMJO_ALLOWED_NETWORKS", "").split(","):
    _cidr = _cidr.strip()
    if _cidr:
        _ALLOWED_NETWORKS.append(ipaddress.ip_network(_cidr, strict=False))


@app.before_request
def _enforce_network_allowlist():
    if not _ALLOWED_NETWORKS:
        return None
    try:
        client_ip = ipaddress.ip_address(request.remote_addr)
    except (ValueError, TypeError):
        return ("Forbidden", 403)
    if not any(client_ip in net for net in _ALLOWED_NETWORKS):
        return ("Forbidden", 403)
    return None


# Simple in-memory per-IP rate limit on login attempts — independent of
# (and in addition to) auth.py's existing per-USERNAME lockout, which
# doesn't slow down someone trying many different usernames from one
# source. No new dependency: a small sliding window keyed by client IP,
# reset lazily. This is intentionally generous (20 requests / 5 minutes)
# since a busy front desk can generate real login traffic from behind a
# single router's IP; it's meant to blunt automated spraying, not to
# police normal multi-person use of one shared network address.
_LOGIN_ATTEMPTS_BY_IP = {}
_LOGIN_RATE_LIMIT_WINDOW_SECONDS = 300
_LOGIN_RATE_LIMIT_MAX = 20


def _login_rate_limit_check(ip):
    now = time.monotonic()
    window_start = now - _LOGIN_RATE_LIMIT_WINDOW_SECONDS
    attempts = [t for t in _LOGIN_ATTEMPTS_BY_IP.get(ip, []) if t > window_start]
    attempts.append(now)
    _LOGIN_ATTEMPTS_BY_IP[ip] = attempts
    # Opportunistic cleanup so this dict doesn't grow unbounded over a
    # long-running process — cheap, and only runs on the (low-traffic)
    # login route.
    if len(_LOGIN_ATTEMPTS_BY_IP) > 1000:
        for k in list(_LOGIN_ATTEMPTS_BY_IP.keys()):
            if not [t for t in _LOGIN_ATTEMPTS_BY_IP[k] if t > window_start]:
                del _LOGIN_ATTEMPTS_BY_IP[k]
    return len(attempts) <= _LOGIN_RATE_LIMIT_MAX


# Max size for any incoming request body (mainly file uploads — X-rays,
# bloodwork PDFs, etc). 100 MB gives generous headroom for a large scan
# while still blocking accidental/abusive multi-GB uploads from filling
# the clinic machine's disk. Flask turns anything over this into a 413,
# handled below with a friendly flash instead of a raw error page.
MAX_UPLOAD_MB = 100
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024


@app.after_request
def add_security_headers(resp):
    """Baseline defense-in-depth headers. Doesn't replace anything (Jinja
    autoescaping + parameterized SQL are the real XSS/injection defenses),
    just closes off a few classes of browser-side attack cheaply."""
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    return resp


# ---------------------------------------------------------------------------
# Crash logging — every unhandled exception gets a short reference ID shown
# on the error page (safe to text/screenshot) and the full exception detail
# written here (not safe to show every role — see handle_unexpected_error).
# A dedicated file+logger, independent of the DB, so a crash caused by the
# database itself being unreachable still gets captured.
# ---------------------------------------------------------------------------
ERROR_LOG_PATH = os.path.join(BASE_DIR, "logs", "errors.log")
os.makedirs(os.path.dirname(ERROR_LOG_PATH), exist_ok=True)

error_logger = logging.getLogger("vetzone.errors")
error_logger.setLevel(logging.ERROR)
if not error_logger.handlers:
    _err_handler = logging.handlers.RotatingFileHandler(
        ERROR_LOG_PATH, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    _err_handler.setFormatter(logging.Formatter("%(message)s"))
    error_logger.addHandler(_err_handler)


def get_db():
    if "db" not in g:
        g.db = dbmod.getconn()
    return g.db


def is_safe_local_path(path):
    """Only allow redirects to relative, in-app paths (no scheme/host)."""
    if not path:
        return False
    if not path.startswith("/"):
        return False
    if path.startswith("//"):
        return False
    if "\\" in path:
        return False
    return True


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        try:
            if exc is None:
                db.commit()
            else:
                db.rollback()
        finally:
            # Always return the connection to the pool, even if the
            # commit/rollback above raised (e.g. the connection dropped
            # mid-request) — the pool discards a connection it can't
            # reuse and opens a replacement, so this can never leak a
            # connection reference the way an un-guarded db.close() call
            # that never ran would have.
            dbmod.putconn(db)


class BadNumber(ValueError):
    """Raised by parse_money() when a submitted field isn't blank but also
    isn't a valid number — lets the route catch it once and show a friendly
    error instead of a raw ValueError (Python) or invalid-input-syntax
    error (Postgres) turning into an uncaught 500."""


def parse_money(raw, required=False):
    """
    Returns a Decimal, not a float — the JOD is a 3-decimal currency
    (ISO 4217 gives it, like KWD/BHD, a fils subunit actually in everyday
    use), unlike the IQD this app was originally forked from, where every
    real amount is a whole number and float64 loses nothing. float64
    can't exactly represent most 3-decimal fractions (0.1 + 0.2 != 0.3 in
    binary floating point), so every money column/value in this app is
    Decimal from parse through storage. Mixing Decimal and float in the
    same arithmetic expression raises TypeError immediately at that line
    — deliberate, since a silent implicit float coercion here would
    reintroduce exactly the precision loss this exists to prevent. Plain
    int literals (0, 100, a quantity from parse_int()) mix with Decimal
    fine; only float does not.
    """
    if raw is None or str(raw).strip() == "":
        if required:
            raise BadNumber("required")
        return None
    try:
        val = Decimal(str(raw).strip())
    except InvalidOperation:
        raise BadNumber(raw)
    # Decimal("nan")/Decimal("inf") parse without raising, the same trap
    # float() had — and every bound check elsewhere in the app (`x > cap`,
    # `x < 0`, etc.) silently evaluates to False against NaN, so an
    # unchecked NaN doesn't just slip past validation, it appears to
    # *pass* every check downstream. Reject both here, once, so every
    # one of this function's call sites inherits the fix instead of
    # needing its own guard.
    if not val.is_finite():
        raise BadNumber(raw)
    return val


def parse_int(raw, required=False):
    """Same shape as parse_money(), for INTEGER columns (e.g. lead_time_days).
    Blank collapses to None; non-numeric input raises BadNumber instead of
    reaching the DB and surfacing as a raw Postgres cast error."""
    if raw is None or str(raw).strip() == "":
        if required:
            raise BadNumber("required")
        return None
    try:
        return int(raw)
    except ValueError:
        raise BadNumber(raw)


class BadDate(ValueError):
    """Raised by clean_date() when a submitted date field isn't blank but
    also isn't a valid ISO date — lets the route catch it once and show a
    friendly error instead of a raw ValueError/Postgres 'invalid input
    syntax for type date' turning into an uncaught 500. More importantly:
    this is what stops an empty string ('') from ever reaching a date
    column. '' is not NULL, so a downstream query that assumes a date
    column is only ever 'a real date or NULL' (e.g. `WHERE date IS NOT
    NULL` followed by `date::date`) breaks the moment it meets one — see
    data_integrity_framework.md for the incident this fixes."""


def clean(v):
    """Collapse '' / whitespace-only to None; otherwise return the
    trimmed value. Use this on read-side filters and any field where
    blank-vs-missing are supposed to mean the same thing but format
    validation would be too strict (e.g. a bad ?date= query param should
    degrade to 'no results', not a hard error)."""
    if v is None:
        return None
    v = v.strip()
    return v or None


def clean_date(v, field="date"):
    """Same as clean(), but also validates the value is a real
    YYYY-MM-DD date if present. Use this on WRITE paths (form -> DB)
    where a bad value should be rejected outright — never on read-side
    filters, where clean() (no format check) is the right choice."""
    v = clean(v)
    if v is None:
        return None
    try:
        datetime.strptime(v, "%Y-%m-%d")
    except ValueError:
        raise BadDate(f"{field.replace('_', ' ').title()} must be a valid date (YYYY-MM-DD).")
    return v


def has_negative(*values):
    """True if any of the given already-parsed numbers (None is fine —
    skipped, since an absent value isn't a negative one) is below zero.
    Used to reject negative cost/sale prices, weights, and unit costs at
    the point of entry — parse_money()/parse_int() already reject NaN/
    Infinity/non-numeric input, but a plain negative number passes those
    checks fine, so this is the separate guard for fields where negative
    is never a valid real-world value."""
    return any(v is not None and v < 0 for v in values)


def stale_edit_error(old_updated_at, submitted_updated_at, what):
    """Optimistic-locking guard for "edit whole record" routes — previously
    last-write-wins with no warning: two staff editing the same record at
    once meant the second save silently erased the first's changes.
    Compares the updated_at the edit form was loaded with against the
    record's current value; a mismatch means someone else saved in
    between. Returns an error string, or None if it's safe to save.
    old_updated_at is None for a row this mechanism has never touched
    (created before this existed, or its very first edit), in which case
    there's nothing to compare against and saving proceeds."""
    if old_updated_at and submitted_updated_at != old_updated_at:
        return (f"This {what} was changed by someone else while you had it open — "
                f"reload the page to see the latest version before saving your changes.")
    return None


# Each deployment of this app serves exactly one clinic in one country, so a
# small self-contained normalizer (rather than pulling in a general-purpose
# library like `phonenumbers`) is simpler and has no extra dependency to
# install. Differs per clinic — these are the two lines that change between
# ChamPet (Iraq) and VetClinicSystem JO (Jordan).
PHONE_COUNTRY_CODE = "962"
PHONE_LOCAL_LENGTH = 9  # digits after the country code, for a number with no explicit +/00 prefix — Jordan mobile numbers (07X XXX XXXX) are 9 digits once the leading trunk 0 is stripped


class BadPhone(ValueError):
    """Raised by normalize_phone() when a submitted phone number isn't blank
    but also can't be confidently normalized to E.164 — lets the route show
    a friendly error instead of silently saving something WhatsApp/wa.me
    links won't be able to use later."""


def normalize_phone(raw):
    """
    Normalizes a phone number to E.164 (+<countrycode><number>). Returns
    None for a blank/optional field. Accepts a local number with a leading
    trunk 0 (e.g. "0791234567"), a number already carrying the country
    code (with or without a leading + or 00), or raises BadPhone if what
    was typed doesn't resemble a real phone number at all.

    A number with no explicit +/00 prefix is ambiguous — there's no way to
    tell "a local number, missing its usual leading 0" from "a foreign
    number, typed without its country code" from the digits alone — so
    that case is held to a strict PHONE_LOCAL_LENGTH-digit count (a real
    local mobile number's actual length) rather than just "looks like
    *some* valid-length phone number." Without this, an implausibly short
    entry (a typo, a truncated paste) or a foreign number missing its
    country code both silently normalize into *something* that passes a
    generic E.164 length check, just not the number anyone actually meant
    — and it's stored with no error, discovered only when a WhatsApp
    message to it fails later. A number given WITH an explicit +/00 is
    unambiguous (the owner is intentionally recording a foreign contact
    number), so that case only needs the general E.164 sanity check.
    """
    if raw is None or not str(raw).strip():
        return None
    raw = str(raw).strip()
    digits = re.sub(r"\D", "", raw)
    if not digits:
        raise BadPhone(raw)
    if raw.startswith("+"):
        candidate = "+" + digits
        if re.fullmatch(r"\+[1-9]\d{7,14}", candidate):
            return candidate
    elif digits.startswith("00"):
        candidate = "+" + digits[2:]
        if re.fullmatch(r"\+[1-9]\d{7,14}", candidate):
            return candidate
    else:
        if digits.startswith("0"):
            local = digits[1:]
        elif digits.startswith(PHONE_COUNTRY_CODE) and len(digits) == len(PHONE_COUNTRY_CODE) + PHONE_LOCAL_LENGTH:
            local = digits[len(PHONE_COUNTRY_CODE):]
        else:
            local = digits
        if len(local) == PHONE_LOCAL_LENGTH:
            return "+" + PHONE_COUNTRY_CODE + local
    raise BadPhone(raw)


@app.template_filter("money")
def money_filter(v):
    return logic.fmt_money(v)


def lan_address():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def vet_users(db):
    return db.execute("SELECT id, full_name FROM users WHERE role_id IN (SELECT id FROM roles WHERE is_vet_role=true) AND active=true ORDER BY full_name").fetchall()


def cached_dashboard_snapshot(db):
    """dashboard_snapshot() scans several tables. It's needed on every page
    (for the nav alert badge) and again on the dashboard route itself —
    cache it per-request so it only runs once."""
    if "dash_snap" not in g:
        g.dash_snap = logic.dashboard_snapshot(db)
    return g.dash_snap


# ---------------------------------------------------------------------------
# Pagination — 50 rows/page across every list view in the system
# ---------------------------------------------------------------------------
PER_PAGE = 50


def get_page():
    try:
        p = int(request.args.get("page", 1))
    except (TypeError, ValueError):
        p = 1
    return max(1, p)


def page_count(total, per_page=PER_PAGE):
    return max(1, (total + per_page - 1) // per_page)


def page_offset(page, per_page=PER_PAGE):
    return (page - 1) * per_page


def pagination_url(page, page_param="page"):
    """Builds a link to another page of the current view, preserving every
    other query-string filter (search terms, sort, date, etc)."""
    args = request.args.to_dict()
    args[page_param] = page
    return url_for(request.endpoint, **args)


app.jinja_env.globals["pagination_url"] = pagination_url
app.jinja_env.globals["has_permission"] = auth.has_permission
app.jinja_env.globals["bind_port"] = BIND_PORT


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------
OPEN_ENDPOINTS = {"login", "static"}


@app.before_request
def require_login():
    if request.endpoint in OPEN_ENDPOINTS or request.endpoint is None:
        return
    if not session.get("user_id"):
        return redirect(url_for("login", next=request.path))
    db = get_db()
    user = auth.current_user(db)
    if not user:
        session.clear()
        return redirect(url_for("login"))
    auth.refresh_session_permissions(db, user)
    if user["must_change_password"] and request.endpoint != "change_password":
        return redirect(url_for("change_password"))


@app.context_processor
def inject_globals():
    """Note: wrapped defensively — this context processor runs on every
    single page render (it feeds the sidebar/nav), including error pages.
    If the database itself is the reason a page is failing (its most
    likely failure mode), we still want the 500/403/404 pages to render
    with sane fallback values instead of throwing a second exception while
    trying to *show* the first one."""
    try:
        db = get_db()
        clinic_name = logic.get_setting(db, "clinic_name", "VetClinicSystem JO")
        clinic_location = logic.get_setting(db, "clinic_location", "Amman, Jordan")
        ctx = dict(clinic_name=clinic_name, clinic_location=clinic_location, today=date.today().isoformat(),
                   current_role=session.get("role"), current_username=session.get("username"))
        if session.get("user_id"):
            snap = cached_dashboard_snapshot(db)
            ctx["alert_count"] = (
                len(snap["due_today"]) + len(snap["low_stock"]) +
                len(snap["overdue_audit"]) + len(snap["expiring"]) +
                len(snap["wellness_due"])
            )
        else:
            ctx["alert_count"] = 0
        return ctx
    except Exception:
        error_logger.error(
            "inject_globals() itself failed (likely DB unreachable) while "
            "rendering %s %s — falling back to static nav values.\n%s",
            request.method, request.path, traceback.format_exc()
        )
        return dict(
            clinic_name="VetClinicSystem JO", clinic_location="",
            today=date.today().isoformat(),
            current_role=session.get("role"), current_username=session.get("username"),
            alert_count=0,
        )


@app.errorhandler(403)
def forbidden(e):
    return render_template("error_403.html"), 403


@app.errorhandler(404)
def not_found(e):
    return render_template("error_404.html"), 404


@app.errorhandler(413)
def too_large(e):
    """Note: deliberately returns a plain 302 (not a 413 body) so this
    plays nicely with the universal upload XHR in upload-progress.js —
    browsers/XHR don't auto-follow a redirect that's paired with a non-3xx
    status code, and we want the flash message to actually surface on the
    page the user lands on, not get silently stranded in the session."""
    flash(f"That file is too large — the limit is {MAX_UPLOAD_MB} MB per upload.", "error")
    from urllib.parse import urlparse
    ref_path = urlparse(request.referrer or "").path
    target = ref_path if is_safe_local_path(ref_path) else None
    return redirect(target or url_for("dashboard"))


def _fallback_redirect():
    """Best-effort 'send them back where they came from' for the global
    validation safety net below — falls back to the dashboard if there's
    no safe referrer to bounce to."""
    ref = request.referrer or ""
    # referrer is a full URL; is_safe_local_path only wants the path part
    from urllib.parse import urlparse
    path = urlparse(ref).path if ref else ""
    if is_safe_local_path(path):
        return redirect(path)
    return redirect(url_for("dashboard"))


@app.errorhandler(BadNumber)
def handle_bad_number(e):
    """Safety net for BadNumber. Most routes already catch this themselves
    with a field-specific message (e.g. 'Payment amount must be a valid
    number.'); this exists so a route that forgets to catch it degrades to
    a flashed error instead of an uncaught 500."""
    flash("One of the number fields on that form wasn't valid. Please check the amounts and try again.", "error")
    return _fallback_redirect()


@app.errorhandler(BadPhone)
def handle_bad_phone(e):
    """Safety net for BadPhone — same idea as handle_bad_number() above."""
    flash("That phone number doesn't look valid. Please check it and try again.", "error")
    return _fallback_redirect()


_REDACT_PATTERNS = [
    # Postgres constraint-violation detail lines look like:
    #   Key (phone)=(0770123456) already exists.
    # Keep the column name (that's genuinely useful for debugging — it
    # tells you *which* field collided) but blank out the actual value.
    (re.compile(r"(Key \([^)]+\)=\()[^)]*(\))"), r"\1REDACTED\2"),
    # Email addresses, anywhere they appear in a message.
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[REDACTED EMAIL]"),
    # Runs of 6+ digits (with optional spaces/dashes) — catches phone
    # numbers without needing to know the clinic's local phone format.
    (re.compile(r"\b\d[\d\-\s]{5,}\d\b"), "[REDACTED NUMBER]"),
]


def redact_sensitive(text):
    """Masks the specific patterns most likely to carry real patient/owner
    data inside an error message (see _REDACT_PATTERNS above) before that
    text is ever shown on a page or copied into a support message. Used
    only for what's displayed in the browser — logs/errors.log always
    keeps the original, unredacted text for real debugging."""
    if not text:
        return text
    for pattern, repl in _REDACT_PATTERNS:
        text = pattern.sub(repl, text)
    return text


@app.errorhandler(Exception)
def handle_unexpected_error(e):
    """Catch-all for anything not handled above — a real bug, a DB outage,
    a third-party library error, etc. Flask walks the exception's class
    hierarchy to find the closest registered handler, so this only ever
    fires for exceptions with no more specific handler; 403/404/413/
    BadNumber/BadPhone (and any other HTTPException) are all matched
    before falling through to here and are passed through untouched.

    Design choice: the on-screen copy box is shown to *every* user, not
    gated by role. A reference ID + route + exception type alone isn't
    enough for anyone to actually diagnose a bug from — the real message
    and traceback are what matter, and whoever happens to hit a crash
    (not necessarily an Admin, and not necessarily technical) is the
    person who'll realistically be the one sending it along. Gating the
    useful part behind "Admin only" would defeat the point.

    What IS scrubbed: the raw exception *message* text specifically —
    because Postgres constraint-violation errors sometimes echo the
    actual offending value inline (e.g. "Key (phone)=(0770123456) already
    exists"), which would otherwise put a real patient/owner's data
    on-screen for anyone who hits that crash. redact_sensitive() masks
    those values (keeping the field name, which is what's actually useful
    for debugging) before anything is rendered. The unredacted original
    always goes to logs/errors.log regardless, for whoever has real
    server access and needs the exact value to reproduce something.
    """
    if isinstance(e, HTTPException):
        return e

    error_id = uuid.uuid4().hex[:8].upper()
    when = datetime.now()
    tb_text = traceback.format_exc()

    error_logger.error(
        "\n".join([
            "=" * 78,
            f"Error ID:   {error_id}",
            f"Time:       {when.isoformat(timespec='seconds')}",
            f"User:       {session.get('username') or '(not logged in)'} ({session.get('role') or '-'})",
            f"Request:    {request.method} {request.path}",
            f"Query:      {request.query_string.decode('utf-8', 'replace') or '-'}",
            f"Referrer:   {request.referrer or '-'}",
            "-" * 78,
            tb_text.rstrip(),  # unredacted — this file is for whoever has server access
            "",
        ])
    )

    return render_template(
        "error_500.html",
        error_id=error_id,
        error_time=when.strftime("%Y-%m-%d %H:%M:%S"),
        request_line=f"{request.method} {request.path}",
        exc_type=type(e).__name__,
        exc_message=redact_sensitive(str(e)),
        traceback_text=redact_sensitive(tb_text),
    ), 500


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        if not _login_rate_limit_check(request.remote_addr):
            flash("Too many login attempts from this network. Please wait a few minutes and try again.", "error")
            return render_template("login.html")
        db = get_db()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        locked, minutes_left, unlock_at = auth.login_lock_status(db, username)
        if locked:
            flash(f"Too many failed attempts for that account. Try again in about {minutes_left} minute(s) "
                  f"(around {unlock_at.strftime('%H:%M')}).", "error")
            return render_template("login.html", lockout_unlock_at=unlock_at.isoformat(timespec="seconds"))
        row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        # verify_password() runs unconditionally, even for a username that
        # doesn't exist — against a dummy hash in that case (see
        # auth._DUMMY_PASSWORD_HASH's own comment) — so a nonexistent/
        # disabled username doesn't respond measurably faster than a real
        # one and leak which usernames exist via response timing.
        password_ok = auth.verify_password(row["password_hash"] if row else auth._DUMMY_PASSWORD_HASH, password)
        ok = row and row["active"] and password_ok
        auth.log_login(db, row["id"] if row else None, username, bool(ok))
        if not ok:
            flash("Incorrect username or password, or account is disabled.", "error")
            return render_template("login.html")
        session["user_id"] = row["id"]
        session["username"] = row["full_name"]
        # Gives the session an actual server-enforced expiry (see
        # PERMANENT_SESSION_LIFETIME above) instead of relying solely on
        # the browser dropping the cookie on close — which doesn't happen
        # on a front-desk machine left open for a whole shift.
        session.permanent = True
        auth.refresh_session_permissions(db, row)
        nxt = request.args.get("next")
        if not is_safe_local_path(nxt):
            nxt = url_for("dashboard")
        return redirect(nxt)
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/change-password", methods=["GET", "POST"])
def change_password():
    db = get_db()
    forced = bool(auth.current_user(db)["must_change_password"])
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        user = db.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
        if not auth.verify_password(user["password_hash"], current):
            flash("Current password is incorrect.", "error")
        elif len(new) < 8:
            flash("New password must be at least 8 characters.", "error")
        elif new != confirm:
            flash("New password and confirmation don't match.", "error")
        else:
            db.execute("UPDATE users SET password_hash=?, must_change_password=false WHERE id=?",
                       (auth.hash_password(new), user["id"]))
            auth.log_change(db, "users", user["id"], "update", {"password": ("(hidden)", "(self-service change)")})
            db.commit()
            flash("Password updated.", "success")
            return redirect(url_for("dashboard"))
    return render_template("change_password.html", forced=forced)


# ---------------------------------------------------------------------------
# Admin — user management
# ---------------------------------------------------------------------------
@app.route("/admin/users")
@auth.permission_required("manage_users_roles")
def admin_users():
    db = get_db()
    users = db.execute(
        "SELECT u.*, r.name AS role_name FROM users u JOIN roles r ON r.id=u.role_id ORDER BY u.full_name"
    ).fetchall()
    roles = db.execute("SELECT id, name FROM roles ORDER BY name").fetchall()
    return render_template("admin_users.html", users=users, roles=roles)


@app.route("/admin/users/new", methods=["POST"])
@auth.permission_required("manage_users_roles")
def admin_user_new():
    db = get_db()
    f = request.form
    username = f.get("username", "").strip()
    password = f.get("password", "")
    full_name = f.get("full_name", "").strip()
    role_id = f.get("role", "")
    role = db.execute("SELECT id FROM roles WHERE id=?", (role_id,)).fetchone()
    if not username or not full_name or not role:
        flash("Fill in a username, full name, and role.", "error")
        return redirect(url_for("admin_users"))
    if len(password) < 8:
        flash("Password must be at least 8 characters.", "error")
        return redirect(url_for("admin_users"))
    if db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        flash("That username is already taken.", "error")
        return redirect(url_for("admin_users"))
    uid = auth.new_user_id()
    db.execute(
        "INSERT INTO users (id,username,password_hash,full_name,role_id,active,must_change_password,created_at) "
        "VALUES (?,?,?,?,?,true,true,?)",
        (uid, username, auth.hash_password(password), full_name, role_id,
         datetime.now().isoformat(timespec="seconds")),
    )
    auth.log_change(db, "users", uid, "create")
    db.commit()
    flash(f"User {username} created. They'll be asked to set a new password on first login.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<user_id>/toggle-active", methods=["POST"])
@auth.permission_required("manage_users_roles")
def admin_user_toggle(user_id):
    db = get_db()
    if user_id == session["user_id"]:
        flash("You can't disable your own account.", "error")
        return redirect(url_for("admin_users"))
    row = db.execute("SELECT active FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None:
        flash("User not found.", "error")
        return redirect(url_for("admin_users"))
    new_val = not row["active"]
    db.execute("UPDATE users SET active=? WHERE id=?", (new_val, user_id))
    auth.log_change(db, "users", user_id, "update", {"active": (row["active"], new_val)})
    db.commit()
    flash("User updated.", "success")
    if new_val is False:
        future_appts = db.execute(
            "SELECT COUNT(*) c FROM appointments WHERE resource_type='vet' AND resource_id=? AND appt_date >= ?",
            (user_id, date.today().isoformat()),
        ).fetchone()["c"]
        if future_appts:
            flash(f"Heads up: {future_appts} upcoming appointment(s) were booked against this person — "
                  f"they won't show on the Appointments grid anymore. Check Appointments for the "
                  f"\"need attention\" list to reschedule them.", "error")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<user_id>/role", methods=["POST"])
@auth.permission_required("manage_users_roles")
def admin_user_role(user_id):
    db = get_db()
    new_role_id = request.form.get("role", "")
    new_role = db.execute("SELECT id, name FROM roles WHERE id=?", (new_role_id,)).fetchone()
    if not new_role:
        flash("Not a valid role.", "error")
        return redirect(url_for("admin_users"))
    row = db.execute(
        "SELECT u.role_id, r.name AS role_name FROM users u JOIN roles r ON r.id=u.role_id WHERE u.id=?",
        (user_id,),
    ).fetchone()
    if row and row["role_id"] == new_role["id"]:
        flash("Role updated.", "success")
        return redirect(url_for("admin_users"))
    if user_id == session["user_id"] and row and row["role_name"] == "Admin" and new_role["name"] != "Admin":
        remaining_admins = db.execute(
            "SELECT COUNT(*) AS n FROM users u JOIN roles r ON r.id=u.role_id "
            "WHERE r.name='Admin' AND u.active=true AND u.id != ?",
            (user_id,),
        ).fetchone()["n"]
        if remaining_admins == 0:
            flash("You can't remove your own Admin role — there must be at least one active Admin.", "error")
            return redirect(url_for("admin_users"))
    db.execute("UPDATE users SET role_id=? WHERE id=?", (new_role["id"], user_id))
    auth.log_change(db, "users", user_id, "update", {"role": (row["role_name"] if row else None, new_role["name"])})
    db.commit()
    flash("Role updated.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<user_id>/reset-password", methods=["POST"])
@auth.permission_required("manage_users_roles")
def admin_user_reset_password(user_id):
    db = get_db()
    new_pw = request.form.get("new_password", "")
    if len(new_pw) < 8:
        flash("Password must be at least 8 characters.", "error")
        return redirect(url_for("admin_users"))
    db.execute("UPDATE users SET password_hash=?, must_change_password=true WHERE id=?",
               (auth.hash_password(new_pw), user_id))
    auth.log_change(db, "users", user_id, "update", {"password": ("(hidden)", "(reset by admin)")})
    db.commit()
    flash("Password reset. The user will be asked to set a new one on next login.", "success")
    return redirect(url_for("admin_users"))


# ---------------------------------------------------------------------------
# Logins and Changes (admin-only audit page)
# ---------------------------------------------------------------------------
@app.route("/admin/logs")
@auth.permission_required("view_logins_changes")
def admin_logs():
    db = get_db()
    day = request.args.get("date", date.today().isoformat())
    changes = logic.changes_on_date(db, day)
    logins = logic.logins_on_date(db, day)
    return render_template("admin_logs.html", day=day, today=date.today().isoformat(), changes=changes, logins=logins)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@app.route("/")
def dashboard():
    db = get_db()
    snap = cached_dashboard_snapshot(db)
    # "Needs Admin Review" is a cross-cutting oversight panel that doesn't map
    # to one single permission from the checklist — shown to anyone with at
    # least one of the Admin-group permissions, as the closest match to "some
    # kind of clinic administrator" (its previous Admin-only gate, made
    # granular so a custom role with equivalent permissions still sees it).
    is_overseer = (auth.has_permission("manage_users_roles") or auth.has_permission("manage_settings")
                   or auth.has_permission("view_logins_changes"))
    all_missed = logic.missed_items(db) if is_overseer else []
    missed_page = get_page()
    missed_total = len(all_missed)
    missed_offset = page_offset(missed_page)
    missed = all_missed[missed_offset:missed_offset + PER_PAGE]
    opex_due = logic.opex_reminder_due(db) if auth.has_permission("view_financial_reports") else False
    backup_alert = None
    if auth.has_permission("manage_settings"):
        import backup as backup_mod
        backup_alert = logic.backup_alert_message(backup_mod.last_backup(db))
    return render_template("dashboard.html", snap=snap, lan_address=lan_address(), missed=missed,
                            is_overseer=is_overseer, opex_due=opex_due, backup_alert=backup_alert,
                            missed_page=missed_page, missed_total_pages=page_count(missed_total),
                            missed_total=missed_total)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.route("/api/patients/search")
def api_patients_search():
    db = get_db()
    term = request.args.get("q", "").strip()
    if len(term) < 2:
        return jsonify([])
    rows = logic.search_patients(db, term)
    return jsonify([{"id": r["id"], "animal_name": r["animal_name"], "species": r["species"],
                      "owner_name": r["owner_name"], "owner_phone": r["owner_phone"]} for r in rows])


@app.route("/api/inventory/lookup")
def api_inventory_lookup():
    db = get_db()
    barcode_val = request.args.get("barcode", "").strip()
    q = request.args.get("q", "").strip()
    if barcode_val:
        row = db.execute("SELECT id, name, barcode FROM inventory_list WHERE barcode=? AND active=true", (barcode_val,)).fetchone()
        if not row:
            return jsonify(None)
        price = logic.item_sale_price(db, row["id"])
        status = logic.inventory_status_by_id(db, row["id"])
        return jsonify({"id": row["id"], "name": row["name"], "price": price,
                        "stock": status["current_stock"] if status else None})
    if q:
        rows = db.execute("SELECT id, name FROM inventory_list WHERE active=true AND category='Retail' AND name ILIKE ? LIMIT 10",
                          (f"%{q}%",)).fetchall()
        # inventory_status_by_id() re-runs the whole catalog-wide status
        # computation and linear-scans for one item — fine called once, not
        # once per matched row here (up to 10x per autocomplete keystroke
        # otherwise). Computed once up front and looked up by item_id
        # instead.
        status_by_item = {s["item_id"]: s for s in logic.inventory_status(db)}
        out = []
        for r in rows:
            price = logic.item_sale_price(db, r["id"])
            status = status_by_item.get(r["id"])
            out.append({"id": r["id"], "name": r["name"], "price": price,
                        "stock": status["current_stock"] if status else None})
        return jsonify(out)
    return jsonify([])


@app.route("/api/price-list/lookup")
def api_price_list_lookup():
    db = get_db()
    q = request.args.get("q", "").strip()
    # Repeatable — e.g. ?category=Service&category=Medicine. Both callers
    # (visit billing, inpatient billing) always pass at least one; Retail
    # is never a valid value here — Retail is sold exclusively through POS
    # (its own dedicated search against inventory_list, untouched by this).
    categories = [c for c in request.args.getlist("category") if c]
    if len(q) < 2 or not categories:
        return jsonify([])
    placeholders = ",".join("?" * len(categories))
    sql = (f"SELECT id, name, category, sale_price FROM price_list "
           f"WHERE active=true AND sale_price IS NOT NULL AND category IN ({placeholders}) "
           f"AND (id ILIKE ? OR name ILIKE ?) ORDER BY name LIMIT 15")
    params = [*categories, f"%{q}%", f"%{q}%"]
    rows = db.execute(sql, params).fetchall()
    return jsonify([{"id": r["id"], "name": r["name"], "category": r["category"], "price": r["sale_price"]} for r in rows])


@app.route("/api/sales/<int:sale_id>/refundable-items")
@auth.permission_required("manage_refunds")
def api_sale_refundable_items(sale_id):
    sale, lines = logic.refundable_sale_items(get_db(), sale_id)
    if not sale:
        return jsonify({"error": "No sale with that ID."}), 404
    return jsonify({
        "sale_id": sale["id"],
        "sale_date": sale["sale_date"],
        "lines": [
            {
                "sale_item_id": l["sale_item_id"],
                "item_id": l["item_id"],
                "name": l["name"],
                "unit_price": l["unit_price"],
                "quantity": l["quantity"],
                "remaining": l["remaining"],
            }
            for l in lines
        ],
    })


# ---------------------------------------------------------------------------
# Owners
# ---------------------------------------------------------------------------
@app.route("/owners")
def owners_list():
    db = get_db()
    search = request.args.get("q", "").strip()
    page = get_page()
    if search:
        total = db.execute("SELECT COUNT(*) c FROM owners WHERE name ILIKE ? OR phone ILIKE ?",
                            (f"%{search}%", f"%{search}%")).fetchone()["c"]
        rows = db.execute(
            "SELECT * FROM owners WHERE name ILIKE ? OR phone ILIKE ? ORDER BY name LIMIT ? OFFSET ?",
            (f"%{search}%", f"%{search}%", PER_PAGE, page_offset(page)),
        ).fetchall()
    else:
        total = db.execute("SELECT COUNT(*) c FROM owners").fetchone()["c"]
        rows = db.execute("SELECT * FROM owners ORDER BY name LIMIT ? OFFSET ?",
                          (PER_PAGE, page_offset(page))).fetchall()
    counts = {r["owner_id"]: r["c"] for r in db.execute("SELECT owner_id, COUNT(*) c FROM patients GROUP BY owner_id").fetchall()}
    return render_template("owners_list.html", owners=rows, search=search, counts=counts,
                            page=page, total_pages=page_count(total), total_count=total)


@app.route("/owners/new", methods=["GET", "POST"])
def owner_new():
    db = get_db()
    if request.method == "POST":
        f = request.form
        try:
            phone = normalize_phone(f.get("phone"))
        except BadPhone:
            flash("That phone number doesn't look valid — check the digits and try again.", "error")
            return render_template("owner_form.html", owner=None)
        oid = dbmod.next_id(db, "OW")
        db.execute("INSERT INTO owners (id,name,phone,address,notes) VALUES (?,?,?,?,?)",
                  (oid, f["name"], phone, f.get("address"), f.get("notes")))
        auth.log_change(db, "owners", oid, "create")
        db.commit()
        flash(f"Owner {oid} added.", "success")
        return redirect(url_for("owner_detail", owner_id=oid))
    return render_template("owner_form.html", owner=None)


@app.route("/owners/<owner_id>")
def owner_detail(owner_id):
    db = get_db()
    owner = db.execute("SELECT * FROM owners WHERE id=?", (owner_id,)).fetchone()
    if not owner:
        flash("Owner not found.", "error")
        return redirect(url_for("owners_list"))
    patients = db.execute("SELECT * FROM patients WHERE owner_id=? ORDER BY animal_name", (owner_id,)).fetchall()
    return render_template("owner_detail.html", owner=owner, patients=patients)


@app.route("/owners/<owner_id>/edit", methods=["GET", "POST"])
def owner_edit(owner_id):
    db = get_db()
    owner = db.execute("SELECT * FROM owners WHERE id=?", (owner_id,)).fetchone()
    if not owner:
        flash("Owner not found.", "error")
        return redirect(url_for("owners_list"))
    if request.method == "POST":
        f = request.form
        try:
            phone = normalize_phone(f.get("phone"))
        except BadPhone:
            flash("That phone number doesn't look valid — check the digits and try again.", "error")
            return redirect(url_for("owner_edit", owner_id=owner_id))
        new_vals = {"name": f["name"], "phone": phone, "address": f.get("address"), "notes": f.get("notes")}
        changes = auth.diff_dict(owner, new_vals)
        db.execute("UPDATE owners SET name=?, phone=?, address=?, notes=? WHERE id=?",
                  (new_vals["name"], new_vals["phone"], new_vals["address"], new_vals["notes"], owner_id))
        auth.log_change(db, "owners", owner_id, "update", changes)
        db.commit()
        flash("Owner updated.", "success")
        return redirect(url_for("owner_detail", owner_id=owner_id))
    return render_template("owner_form.html", owner=owner)


# ---------------------------------------------------------------------------
# Patients (sortable)
# ---------------------------------------------------------------------------
PATIENT_SORT_COLUMNS = {
    "id": "p.id", "animal_name": "p.animal_name", "species": "p.species", "owner": "o.name",
}


@app.route("/patients")
def patients_list():
    db = get_db()
    search = request.args.get("q", "").strip()
    sort = request.args.get("sort", "id")
    direction = request.args.get("dir", "desc" if sort == "id" else "asc")
    sort_col = PATIENT_SORT_COLUMNS.get(sort, "p.id")
    direction_sql = "DESC" if direction == "desc" else "ASC"
    page = get_page()

    if search:
        # search_patients() is already capped to the top 25 best matches —
        # a single page's worth, so no further pagination needed here.
        rows = logic.search_patients(db, search)
        total = len(rows)
        total_pages_ = 1
    else:
        total = db.execute("SELECT COUNT(*) c FROM patients").fetchone()["c"]
        rows = db.execute(
            f"SELECT p.*, o.name as owner_name, o.phone as owner_phone FROM patients p "
            f"JOIN owners o ON o.id=p.owner_id ORDER BY {sort_col} {direction_sql} LIMIT ? OFFSET ?",
            (PER_PAGE, page_offset(page)),
        ).fetchall()
        total_pages_ = page_count(total)
    return render_template("patients_list.html", patients=rows, search=search, sort=sort, direction=direction,
                            page=page, total_pages=total_pages_, total_count=total)


@app.route("/patients/<patient_id>")
def patient_detail(patient_id):
    db = get_db()
    patient = db.execute(
        "SELECT p.*, o.name as owner_name, o.phone as owner_phone, o.id as owner_id FROM patients p "
        "JOIN owners o ON o.id=p.owner_id WHERE p.id=?", (patient_id,)
    ).fetchone()
    if not patient:
        flash("Patient not found.", "error")
        return redirect(url_for("patients_list"))
    visits = db.execute("SELECT * FROM visits WHERE patient_id=? ORDER BY date DESC", (patient_id,)).fetchall()
    visits = [dict(v) for v in visits]
    for v in visits:
        v["billing"] = logic.visit_billing_summary(db, v["id"])
    grooming_sessions = [v for v in visits if v["grooming_needed"] == "Y"]
    cases = db.execute("SELECT * FROM inpatient_cases WHERE patient_id=? ORDER BY admission_date DESC", (patient_id,)).fetchall()
    boarding_sessions = logic.boarding_sessions_for_patient(db, patient_id)
    return render_template("patient_detail.html", patient=patient, visits=visits, cases=cases,
                            grooming_sessions=grooming_sessions, boarding_sessions=boarding_sessions)


@app.route("/patients/<patient_id>/edit", methods=["GET", "POST"])
def patient_edit(patient_id):
    db = get_db()
    patient = db.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
    if not patient:
        flash("Patient not found.", "error")
        return redirect(url_for("patients_list"))
    if request.method == "POST":
        f = request.form
        new_vals = {"animal_name": f["animal_name"], "species": f["species"], "sex": f.get("sex"),
                    "age_note": f.get("age_note"), "repro_status": f.get("repro_status"),
                    "housing": f.get("housing"), "notes": f.get("notes")}
        changes = auth.diff_dict(patient, new_vals)
        db.execute(
            "UPDATE patients SET animal_name=?, species=?, sex=?, age_note=?, repro_status=?, housing=?, notes=? WHERE id=?",
            (*new_vals.values(), patient_id),
        )
        auth.log_change(db, "patients", patient_id, "update", changes)
        db.commit()
        flash("Patient updated.", "success")
        return redirect(url_for("patient_detail", patient_id=patient_id))
    return render_template("patient_form_edit.html", patient=patient)


@app.route("/patients/<patient_id>/history")
def patient_history(patient_id):
    db = get_db()
    patient = db.execute(
        "SELECT p.*, o.name as owner_name FROM patients p JOIN owners o ON o.id=p.owner_id WHERE p.id=?", (patient_id,)
    ).fetchone()
    if not patient:
        flash("Patient not found.", "error")
        return redirect(url_for("patients_list"))
    events = logic.patient_history(db, patient_id)
    return render_template("patient_history.html", patient=patient, events=events)


@app.route("/patients/<patient_id>/export/file")
def patient_export_file(patient_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM patients WHERE id=?", (patient_id,)).fetchone():
        abort(404)
    buf = pdf_export.export_patient_file(db, patient_id)
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=f"{patient_id}_patient_file.pdf")


@app.route("/patients/<patient_id>/export/billing")
def patient_export_billing(patient_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM patients WHERE id=?", (patient_id,)).fetchone():
        abort(404)
    buf = pdf_export.export_patient_billing(db, patient_id)
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=f"{patient_id}_billing.pdf")


@app.route("/pos/history/<int:sale_id>/export")
def pos_export_receipt(sale_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM sales WHERE id=?", (sale_id,)).fetchone():
        abort(404)
    buf = pdf_export.export_sale_receipt(db, sale_id)
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=f"sale_{sale_id}_receipt.pdf")


@app.route("/visits/<visit_id>/export")
def visit_export_pdf(visit_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM visits WHERE id=?", (visit_id,)).fetchone():
        abort(404)
    buf = pdf_export.export_visit_pdf(db, visit_id)
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=f"{visit_id}_visit.pdf")


@app.route("/inpatient/<int:case_id>/export")
def inpatient_export_pdf(case_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM inpatient_cases WHERE id=?", (case_id,)).fetchone():
        abort(404)
    buf = pdf_export.export_inpatient_pdf(db, case_id)
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=f"inpatient_{case_id}.pdf")


# ---------------------------------------------------------------------------
# New Visit workflow
# ---------------------------------------------------------------------------
CASE_STATUSES = ["Needs Filling", "Ongoing", "Admitted to Inpatient", "Deceased/Euthanized",
                  "Lost to Follow Up", "Resolved", "Referred"]
FOLLOWUP_REASONS = ["Surgery Follow Up", "Medical Follow Up", "Vaccine", "Deworming", "Spot On", "Other"]
WELLNESS_TYPES = ["Annual Vaccine", "First Vaccine", "Rabies Vaccine", "Deworming", "Spot On/Pill"]
GROOMING_SERVICES = logic.GROOMING_SERVICES


@app.route("/visits/new")
def visit_new_start():
    return render_template("visit_new_start.html")


@app.route("/visits/new/existing", methods=["GET", "POST"])
def visit_new_existing():
    db = get_db()
    if request.method == "POST":
        patient_id = request.form.get("patient_id", "").strip()
        if not patient_id or not db.execute("SELECT 1 FROM patients WHERE id=?", (patient_id,)).fetchone():
            flash("Pick a patient from the search results before logging a visit.", "error")
            return redirect(url_for("visit_new_existing"))
        vid = _create_visit(db, patient_id, request.form)
        return redirect(url_for("visit_detail", visit_id=vid))
    return render_template("visit_new_existing.html", vets=vet_users(db), wellness_types=WELLNESS_TYPES,
                            grooming_services=GROOMING_SERVICES)


@app.route("/visits/new/new-patient", methods=["GET", "POST"])
def visit_new_patient():
    db = get_db()
    if request.method == "POST":
        f = request.form
        try:
            owner_phone = normalize_phone(f.get("owner_phone"))
        except BadPhone:
            flash("That owner phone number doesn't look valid — check the digits and try again.", "error")
            return redirect(url_for("visit_new_patient"))
        oid = dbmod.next_id(db, "OW")
        db.execute("INSERT INTO owners (id,name,phone,address) VALUES (?,?,?,?)",
                  (oid, f["owner_name"], owner_phone, f.get("owner_address")))
        auth.log_change(db, "owners", oid, "create")

        pid = dbmod.next_id(db, "PT")
        db.execute(
            "INSERT INTO patients (id,owner_id,animal_name,species,sex,age_note,repro_status,housing) VALUES (?,?,?,?,?,?,?,?)",
            (pid, oid, f["animal_name"], f["species"], f.get("sex"), f.get("age_note"), f.get("repro_status"), f.get("housing")),
        )
        auth.log_change(db, "patients", pid, "create")
        db.commit()

        vid = _create_visit(db, pid, f)
        return redirect(url_for("visit_detail", visit_id=vid))
    return render_template("visit_new_patient.html", vets=vet_users(db), wellness_types=WELLNESS_TYPES,
                            grooming_services=GROOMING_SERVICES)


def _create_visit(db, patient_id, f):
    vid = dbmod.next_id(db, "V")
    admit_now = f.get("admit_inpatient") == "on"
    try:
        visit_date = clean_date(f.get("date"), field="date") or date.today().isoformat()
    except BadDate as e:
        flash(str(e), "error")
        return redirect(request.referrer or url_for("dashboard"))
    wellness_needed = f.get("wellness_needed", "N")
    grooming_needed = f.get("grooming_needed", "N")
    grooming_services = ",".join(f.getlist("grooming_services")) if grooming_needed == "Y" else None
    try:
        weight_kg = parse_money(f.get("weight_kg"))
        bcs = parse_int(f.get("bcs"))
    except BadNumber:
        flash("Weight and BCS must be valid numbers.", "error")
        return redirect(request.referrer or url_for("dashboard"))

    db.execute(
        """INSERT INTO visits (id,patient_id,visit_type,date,doctor,weight_kg,bcs,complaint,history,exam,treatment,
           case_status,case_status_changed_at,updates_log,
           followup_needed,followup_method,followup_reason,followup_date,followup_status,
           wellness_needed,wellness_type,wellness_next_dose_date,wellness_contacted,wellness_contact_method,
           grooming_needed,grooming_services,grooming_notes,grooming_admitted_items,grooming_status,grooming_contacted,
           payment_status,created_by)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (vid, patient_id, "Inpatient" if admit_now else "Outpatient", visit_date,
         f.get("doctor"), weight_kg, bcs, f.get("complaint"), f.get("history"), None, None,
         "Admitted to Inpatient" if admit_now else "Needs Filling", visit_date, None,
         "N", None, None, None, "N/A",
         wellness_needed, f.get("wellness_type") if wellness_needed == "Y" else None,
         clean_date(f.get("wellness_next_dose_date"), field="wellness_next_dose_date") if wellness_needed == "Y" else None, "N", None,
         grooming_needed, grooming_services, f.get("grooming_notes") if grooming_needed == "Y" else None,
         f.get("grooming_admitted_items") if grooming_needed == "Y" else None,
         "Waiting" if grooming_needed == "Y" else None, "N",
         "N/A", session.get("user_id")),
    )
    auth.log_change(db, "visits", vid, "create")
    if admit_now:
        _create_inpatient_case(db, patient_id, vid, f.get("complaint"), visit_date, weight_kg, bcs)
    db.commit()
    return vid


def _create_inpatient_case(db, patient_id, visit_id, complaint, admission_date, weight_kg=None, bcs=None):
    cur = db.execute(
        "INSERT INTO inpatient_cases (patient_id, visit_id, complaint, admission_date, weight_kg, bcs, dismissed, created_by) VALUES (?,?,?,?,?,?,false,?) RETURNING id",
        (patient_id, visit_id, complaint, admission_date or date.today().isoformat(), weight_kg, bcs, session.get("user_id")),
    )
    case_id = cur.fetchone()["id"]
    auth.log_change(db, "inpatient_cases", str(case_id), "create")
    return case_id


# ---------------------------------------------------------------------------
# Visits (sortable + date filter)
# ---------------------------------------------------------------------------
@app.route("/visits")
def visits_list():
    db = get_db()
    sort = request.args.get("sort", "date")
    day_filter = request.args.get("date")
    search = request.args.get("q", "").strip()
    page = get_page()

    from_join = "FROM visits v JOIN patients p ON p.id=v.patient_id JOIN owners o ON o.id=p.owner_id"
    params = []
    where = []
    if day_filter:
        where.append("v.date=?")
        params.append(day_filter)
    if search:
        where.append("(p.animal_name ILIKE ? OR o.name ILIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    total = db.execute(f"SELECT COUNT(*) c {from_join}{where_sql}", params).fetchone()["c"]

    order_map = {
        "date": "v.date DESC, v.id DESC",
        "type": "v.visit_type ASC, v.date DESC",
        "status": "v.case_status ASC, v.date DESC",
        "payment": "v.payment_status ASC, v.date DESC",
    }
    q = f"SELECT v.*, p.animal_name, o.name as owner_name {from_join}{where_sql}"
    q += " ORDER BY " + order_map.get(sort, order_map["date"])
    q += " LIMIT ? OFFSET ?"

    rows = [dict(r) for r in db.execute(q, params + [PER_PAGE, page_offset(page)]).fetchall()]
    for r in rows:
        r["billing"] = logic.visit_billing_summary(db, r["id"])
    return render_template("visits_list.html", visits=rows, sort=sort, day_filter=day_filter or "", search=search,
                            page=page, total_pages=page_count(total), total_count=total)


@app.route("/visits/<visit_id>")
def visit_detail(visit_id):
    db = get_db()
    visit = db.execute(
        "SELECT v.*, p.animal_name, p.id as patient_id, o.name as owner_name, o.phone as owner_phone FROM visits v "
        "JOIN patients p ON p.id=v.patient_id JOIN owners o ON o.id=p.owner_id WHERE v.id=?", (visit_id,)
    ).fetchone()
    if not visit:
        flash("Visit not found.", "error")
        return redirect(url_for("visits_list"))
    billing_row = db.execute("SELECT * FROM billing WHERE visit_id=?", (visit_id,)).fetchone()
    summary = logic.visit_billing_summary(db, visit_id)
    payments = db.execute("SELECT * FROM payments WHERE visit_id=? ORDER BY date DESC", (visit_id,)).fetchall()
    files = attach_mod.list_attachments(db, "visit", visit_id)
    cap = auth.discount_cap_for()
    return render_template("visit_detail.html", visit=visit, billing=billing_row, summary=summary,
                            payments=payments, files=files, discount_cap=cap)


@app.route("/visits/<visit_id>/edit", methods=["GET", "POST"])
def visit_edit(visit_id):
    db = get_db()
    visit = db.execute("SELECT * FROM visits WHERE id=?", (visit_id,)).fetchone()
    if not visit:
        flash("Visit not found.", "error")
        return redirect(url_for("visits_list"))
    if request.method == "POST":
        f = request.form
        conflict = stale_edit_error(visit["updated_at"], f.get("expected_updated_at"), "visit")
        if conflict:
            flash(conflict, "error")
            return redirect(url_for("visit_edit", visit_id=visit_id))
        wellness_needed = f.get("wellness_needed", "N")
        grooming_needed = f.get("grooming_needed", "N")
        grooming_services = ",".join(f.getlist("grooming_services")) if grooming_needed == "Y" else None
        new_case_status = f.get("case_status", visit["case_status"])
        status_changed_at = visit["case_status_changed_at"]
        if new_case_status != visit["case_status"]:
            status_changed_at = date.today().isoformat()

        try:
            edited_date = clean_date(f.get("date"), field="date")
            edited_followup_date = clean_date(f.get("followup_date"), field="followup_date")
            edited_wellness_next_dose_date = clean_date(f.get("wellness_next_dose_date"), field="wellness_next_dose_date") if wellness_needed == "Y" else None
            edited_weight_kg = parse_money(f.get("weight_kg"))
            edited_bcs = parse_int(f.get("bcs"))
        except BadDate as e:
            flash(str(e), "error")
            return redirect(url_for("visit_edit", visit_id=visit_id))
        except BadNumber:
            flash("Weight and BCS must be valid numbers.", "error")
            return redirect(url_for("visit_edit", visit_id=visit_id))
        if has_negative(edited_weight_kg):
            flash("Weight can't be negative.", "error")
            return redirect(url_for("visit_edit", visit_id=visit_id))

        new_vals = {
            "visit_type": f.get("visit_type"), "date": edited_date, "doctor": f.get("doctor"),
            "weight_kg": edited_weight_kg, "bcs": edited_bcs,
            "complaint": f.get("complaint"), "history": f.get("history"), "exam": f.get("exam"),
            "treatment": f.get("treatment"), "case_status": new_case_status, "case_status_changed_at": status_changed_at,
            "updates_log": f.get("updates_log"),
            "followup_needed": f.get("followup_needed", "N"), "followup_method": f.get("followup_method") or None,
            "followup_reason": f.get("followup_reason") or None, "followup_date": edited_followup_date,
            "followup_status": f.get("followup_status", "N/A"),
            "wellness_needed": wellness_needed, "wellness_type": f.get("wellness_type") if wellness_needed == "Y" else None,
            "wellness_next_dose_date": edited_wellness_next_dose_date,
            "wellness_contacted": f.get("wellness_contacted", "N"), "wellness_contact_method": f.get("wellness_contact_method") or None,
            "grooming_needed": grooming_needed, "grooming_services": grooming_services,
            "grooming_notes": f.get("grooming_notes") if grooming_needed == "Y" else None,
            "grooming_admitted_items": f.get("grooming_admitted_items") if grooming_needed == "Y" else None,
            "grooming_status": (f.get("grooming_status") or "Waiting") if grooming_needed == "Y" else None,
            "grooming_contacted": f.get("grooming_contacted", "N"),
            "payment_status": f.get("payment_status", "N/A"),
        }
        changes = auth.diff_dict(visit, new_vals)
        db.execute(
            """UPDATE visits SET visit_type=?, date=?, doctor=?, weight_kg=?, bcs=?, complaint=?, history=?, exam=?, treatment=?,
               case_status=?, case_status_changed_at=?, updates_log=?, followup_needed=?, followup_method=?,
               followup_reason=?, followup_date=?, followup_status=?, wellness_needed=?, wellness_type=?,
               wellness_next_dose_date=?, wellness_contacted=?, wellness_contact_method=?, grooming_needed=?,
               grooming_services=?, grooming_notes=?, grooming_admitted_items=?, grooming_status=?,
               grooming_contacted=?, payment_status=?, updated_at=? WHERE id=?""",
            (*new_vals.values(), datetime.now().isoformat(timespec="seconds"), visit_id),
        )
        auth.log_change(db, "visits", visit_id, "update", changes)
        db.commit()
        flash("Visit updated.", "success")
        return redirect(url_for("visit_detail", visit_id=visit_id))
    return render_template("visit_form_edit.html", visit=visit, case_statuses=CASE_STATUSES,
                            followup_reasons=FOLLOWUP_REASONS, wellness_types=WELLNESS_TYPES,
                            grooming_services=GROOMING_SERVICES, vets=vet_users(db))


@app.route("/visits/<visit_id>/billing", methods=["POST"])
def visit_billing_save(visit_id):
    db = get_db()
    f = request.form
    billing_type = f.get("billing_type", "Automatic")
    if billing_type not in BILLING_TYPES:
        flash("Billing type must be one of: " + ", ".join(BILLING_TYPES) + ".", "error")
        return redirect(url_for("visit_detail", visit_id=visit_id))
    priced_lines = []
    had_bad_number = had_bad_price = False
    if billing_type == "Automatic":
        # Same pattern as inpatient_billing_add(): each cart row is a
        # validated search-result pick (price_id + qty_{id}), not typed
        # free text, so an invalid price_id here only happens on a
        # tampered request — skipped with a flash rather than a raw
        # database error.
        for pid in f.getlist("price_id"):
            try:
                qty = parse_money(f.get(f"qty_{pid}", "").strip())
            except BadNumber:
                had_bad_number = True
                continue
            if not qty or qty <= 0:
                continue
            price_row = db.execute(
                "SELECT name, category, sale_price, cost_price FROM price_list WHERE id=?", (pid,)
            ).fetchone()
            if not price_row:
                had_bad_price = True
                continue
            priced_lines.append({
                "price_id": pid, "name": price_row["name"], "category": price_row["category"],
                "quantity": qty, "unit_price": price_row["sale_price"], "unit_cost": price_row["cost_price"],
            })
        if not priced_lines:
            flash("Add at least one billed item.", "error")
            return redirect(url_for("visit_detail", visit_id=visit_id))
        # visit_discount_save() only checks non-discountable items against
        # whatever's on the bill *at the moment a discount is applied* — it
        # has no way to know the bill will change later. Re-checking here
        # too closes the gap where a discount already applied earlier would
        # otherwise silently carry forward onto items added afterward that
        # were never supposed to be discountable at all.
        existing_discount = db.execute(
            "SELECT discount_percent FROM billing WHERE visit_id=?", (visit_id,)
        ).fetchone()
        if existing_discount and (existing_discount["discount_percent"] or 0) > 0:
            blocked = logic.non_discountable_line_names(db, [l["price_id"] for l in priced_lines])
            if blocked:
                flash(f"Can't save — this bill has a {existing_discount['discount_percent']:.0f}% discount applied, "
                      f"but includes item(s) marked as not discountable: {', '.join(blocked)}. "
                      "Remove the discount first, or leave these items off this bill.", "error")
                return redirect(url_for("visit_detail", visit_id=visit_id))
    try:
        manual_amount = parse_money(f.get("manual_amount")) if billing_type == "Manual" else None
    except BadNumber:
        flash("Manual amount must be a valid number.", "error")
        return redirect(url_for("visit_detail", visit_id=visit_id))
    if billing_type == "Manual" and (manual_amount is None or manual_amount <= 0):
        flash("Manual Entry requires a Billed Amount greater than 0.", "error")
        return redirect(url_for("visit_detail", visit_id=visit_id))
    try:
        date_billed = clean_date(f.get("date_billed"), field="date_billed")
    except BadDate as e:
        flash(str(e), "error")
        return redirect(url_for("visit_detail", visit_id=visit_id))
    notes = f.get("notes")
    existing = db.execute("SELECT * FROM billing WHERE visit_id=?", (visit_id,)).fetchone()
    old_month = logic.month_key(existing["date_billed"]) if existing else None
    # UPSERT rather than a SELECT-then-branch INSERT/UPDATE — visit_id is
    # billing's primary key, so two near-simultaneous saves (double-click,
    # a retried request) racing this as a plain branch could both read no
    # existing row and both attempt an INSERT, the second raising an
    # unhandled UniqueViolation. ON CONFLICT makes the second one an
    # atomic update instead of a crash.
    db.execute(
        "INSERT INTO billing (visit_id, billing_type, manual_amount, date_billed, notes) VALUES (?,?,?,?,?) "
        "ON CONFLICT (visit_id) DO UPDATE SET billing_type=excluded.billing_type, "
        "manual_amount=excluded.manual_amount, date_billed=excluded.date_billed, notes=excluded.notes",
        (visit_id, billing_type, manual_amount, date_billed, notes),
    )
    if billing_type == "Automatic":
        # Snapshot the current Price List values for every item in the
        # cart right now, at Save time — this is what stops a price edit
        # next month from silently changing what this visit's bill (and
        # the revenue/COGS report for the month it was billed) says today.
        logic.save_visit_billing_lines(db, visit_id, priced_lines)
    else:
        # Switched to (or re-saved as) Manual — any prior Automatic
        # snapshot for this visit no longer applies.
        db.execute("DELETE FROM visit_billing_lines WHERE visit_id=?", (visit_id,))
    logic.refresh_visit_billing_total(db, visit_id)
    new_month = logic.month_key(date_billed)
    logic.recompute_months_summary(db, [old_month, new_month])
    auth.log_change(db, "billing", visit_id, "update" if existing else "create")
    db.commit()
    if had_bad_number:
        flash("Some quantities weren't valid numbers and were skipped.", "error")
    if had_bad_price:
        flash("Some selected items no longer exist in the Price List and were skipped.", "error")
    flash("Billing saved.", "success")
    return redirect(url_for("visit_detail", visit_id=visit_id))


@app.route("/visits/<visit_id>/discount", methods=["POST"])
def visit_discount_save(visit_id):
    db = get_db()
    try:
        percent = parse_money(request.form.get("discount_percent")) or 0
    except BadNumber:
        flash("Discount must be a valid number.", "error")
        return redirect(url_for("visit_detail", visit_id=visit_id))
    cap = auth.discount_cap_for()
    if percent > cap or percent < 0:
        flash(f"Discount must be between 0% and {cap}% for your role.", "error")
        return redirect(url_for("visit_detail", visit_id=visit_id))
    if percent > 0:
        summary = logic.visit_billing_summary(db, visit_id)
        blocked = logic.non_discountable_line_names(db, [l["id"] for l in summary["lines"]])
        if blocked:
            flash(f"Can't apply a discount — this bill includes item(s) marked as not discountable: {', '.join(blocked)}.", "error")
            return redirect(url_for("visit_detail", visit_id=visit_id))
    existing = db.execute("SELECT * FROM billing WHERE visit_id=?", (visit_id,)).fetchone()
    # Same UPSERT reasoning as visit_billing_save() — visit_id is billing's
    # primary key, so a plain SELECT-then-branch here is racy the same way.
    db.execute(
        "INSERT INTO billing (visit_id, discount_percent, discount_applied_by) VALUES (?,?,?) "
        "ON CONFLICT (visit_id) DO UPDATE SET discount_percent=excluded.discount_percent, "
        "discount_applied_by=excluded.discount_applied_by",
        (visit_id, percent, session["user_id"]),
    )
    logic.refresh_visit_billing_total(db, visit_id)
    if existing and existing["date_billed"]:
        logic.recompute_month_summary(db, logic.month_key(existing["date_billed"]))
    auth.log_change(db, "billing", visit_id, "update", {"discount_percent": (existing["discount_percent"] if existing else 0, percent)})
    db.commit()
    flash(f"{percent:.0f}% discount applied.", "success")
    return redirect(url_for("visit_detail", visit_id=visit_id))


@app.route("/visits/<visit_id>/payment", methods=["POST"])
def visit_payment_add(visit_id):
    db = get_db()
    f = request.form
    try:
        amount = parse_money(f.get("amount"), required=True)
    except BadNumber:
        flash("Payment amount must be a valid number.", "error")
        return redirect(url_for("visit_detail", visit_id=visit_id))
    if amount <= 0:
        flash("Payment amount must be greater than 0.", "error")
        return redirect(url_for("visit_detail", visit_id=visit_id))
    try:
        payment_date = clean_date(f.get("date"), field="date") or date.today().isoformat()
    except BadDate as e:
        flash(str(e), "error")
        return redirect(url_for("visit_detail", visit_id=visit_id))
    cur = db.execute(
        "INSERT INTO payments (visit_id, amount, method, date, user_id, notes) VALUES (?,?,?,?,?,?) RETURNING id",
        (visit_id, amount, f.get("method"), payment_date, session["user_id"], f.get("notes")),
    )
    payment_id = cur.fetchone()["id"]
    auth.log_change(db, "payments", str(payment_id), "create")
    db.commit()
    flash("Payment recorded.", "success")
    return redirect(url_for("visit_detail", visit_id=visit_id))


@app.route("/visits/<visit_id>/attachments", methods=["POST"])
def visit_attachment_upload(visit_id):
    db = get_db()
    patient_row = db.execute("SELECT patient_id FROM visits WHERE id=?", (visit_id,)).fetchone()
    if patient_row is None:
        flash("Visit not found.", "error")
        return redirect(url_for("visits_list"))
    file = request.files.get("file")
    if not file or not file.filename:
        flash("No file selected.", "error")
        return redirect(url_for("visit_detail", visit_id=visit_id))
    _, err = attach_mod.save_attachment(db, patient_row["patient_id"], "visit", visit_id, file, session["user_id"])
    flash(err if err else "File uploaded.", "error" if err else "success")
    return redirect(url_for("visit_detail", visit_id=visit_id))


@app.route("/files/<path:relpath>")
def serve_attachment(relpath):
    db = get_db()
    row = db.execute("SELECT relative_path FROM attachments WHERE relative_path=?", (relpath,)).fetchone()
    if row is None:
        flash("File not found.", "error")
        return redirect(url_for("dashboard"))
    return send_from_directory(attach_mod.UPLOAD_ROOT, relpath)


@app.route("/attachments/<int:attachment_id>/delete", methods=["POST"])
def attachment_delete(attachment_id):
    """
    Deletes one uploaded Additional Test / X-Ray — from both the database
    and the uploads/ folder on disk — and records it in the audit log so
    a removed file still shows up in Admin > Logins and Changes. Shared by
    every place in the app that lists attachments (Visit Detail, Inpatient
    Detail), since a visit's and an inpatient case's attachments both live
    in the same `attachments` table.
    """
    db = get_db()
    row = attach_mod.get_attachment(db, attachment_id)
    if row is None:
        flash("File not found — it may have already been deleted.", "error")
        return redirect(request.referrer or url_for("dashboard"))

    if row["visit_id"]:
        redirect_target = url_for("visit_detail", visit_id=row["visit_id"])
    elif row["inpatient_case_id"]:
        redirect_target = url_for("inpatient_detail", case_id=row["inpatient_case_id"])
    else:
        redirect_target = url_for("dashboard")

    deleted, err = attach_mod.delete_attachment(db, attachment_id)
    if err:
        flash(err, "error")
        return redirect(redirect_target)
    if deleted is None:
        flash("File not found — it may have already been deleted.", "error")
        return redirect(redirect_target)
    auth.log_change(db, "attachments", str(attachment_id), "delete")
    db.commit()
    flash(f"Deleted {deleted['original_name']}.", "success")
    return redirect(redirect_target)


# ---------------------------------------------------------------------------
# Follow-ups
# ---------------------------------------------------------------------------
@app.route("/followups")
def followups_list():
    db = get_db()
    show_all = request.args.get("all") == "1"
    page = get_page()
    rows, total = logic.followups_page(db, only_pending=not show_all, limit=PER_PAGE, offset=page_offset(page))
    return render_template("followups_list.html", followups=rows, show_all=show_all,
                            page=page, total_pages=page_count(total), total_count=total)


@app.route("/followups/<visit_id>/status", methods=["POST"])
def followup_status_update(visit_id):
    db = get_db()
    status = request.form.get("status")
    old = db.execute("SELECT followup_status FROM visits WHERE id=?", (visit_id,)).fetchone()
    if not old:
        flash("Visit not found.", "error")
        return redirect(request.referrer or url_for("followups_list"))
    db.execute("UPDATE visits SET followup_status=? WHERE id=?", (status, visit_id))
    auth.log_change(db, "visits", visit_id, "update", {"followup_status": (old["followup_status"], status)})
    db.commit()
    flash("Follow-up status updated.", "success")
    return redirect(request.referrer or url_for("followups_list"))


# ---------------------------------------------------------------------------
# Wellness
# ---------------------------------------------------------------------------
@app.route("/wellness")
def wellness_list():
    db = get_db()
    page = get_page()
    rows, total = logic.wellness_reminders_page(db, limit=PER_PAGE, offset=page_offset(page))
    return render_template("wellness_list.html", rows=rows,
                            page=page, total_pages=page_count(total), total_count=total)


@app.route("/wellness/<visit_id>/update", methods=["POST"])
def wellness_update(visit_id):
    db = get_db()
    f = request.form
    old = db.execute("SELECT wellness_contacted, wellness_contact_method FROM visits WHERE id=?", (visit_id,)).fetchone()
    if not old:
        flash("Visit not found.", "error")
        return redirect(url_for("wellness_list"))
    db.execute("UPDATE visits SET wellness_contacted=?, wellness_contact_method=? WHERE id=?",
              (f.get("wellness_contacted", "N"), f.get("wellness_contact_method") or None, visit_id))
    auth.log_change(db, "visits", visit_id, "update", {"wellness_contacted": (old["wellness_contacted"], f.get("wellness_contacted", "N"))})
    db.commit()
    flash("Wellness reminder updated.", "success")
    return redirect(url_for("wellness_list"))


# ---------------------------------------------------------------------------
# Grooming
# ---------------------------------------------------------------------------
@app.route("/grooming")
def grooming_list():
    db = get_db()
    include_finished = request.args.get("all") == "1"
    page = get_page()
    rows, total = logic.grooming_queue_page(db, include_finished=include_finished, limit=PER_PAGE, offset=page_offset(page))
    return render_template("grooming_list.html", rows=rows, include_finished=include_finished,
                            page=page, total_pages=page_count(total), total_count=total)


@app.route("/grooming/<visit_id>/update", methods=["POST"])
def grooming_update(visit_id):
    db = get_db()
    f = request.form
    old = db.execute("SELECT grooming_status, grooming_contacted FROM visits WHERE id=?", (visit_id,)).fetchone()
    if not old:
        flash("Visit not found.", "error")
        return redirect(url_for("grooming_list"))
    db.execute("UPDATE visits SET grooming_status=?, grooming_contacted=? WHERE id=?",
              (f.get("grooming_status"), f.get("grooming_contacted", "N"), visit_id))
    auth.log_change(db, "visits", visit_id, "update", {"grooming_status": (old["grooming_status"], f.get("grooming_status"))})
    db.commit()
    flash("Grooming entry updated.", "success")
    return redirect(url_for("grooming_list"))


# ---------------------------------------------------------------------------
# Price List (Admin only)
# ---------------------------------------------------------------------------
PRICE_CATEGORIES = ["Service", "Medicine", "Retail"]


@app.route("/price-list")
@auth.permission_required("manage_price_list")
def price_list():
    db = get_db()
    cat = request.args.get("category")
    search = request.args.get("q", "").strip()
    page = get_page()
    where = ["active=true"]
    params = []
    if cat:
        where.append("category=?")
        params.append(cat)
    if search:
        where.append("name ILIKE ?")
        params.append(f"%{search}%")
    where_sql = " WHERE " + " AND ".join(where)
    total = db.execute(f"SELECT COUNT(*) c FROM price_list{where_sql}", params).fetchone()["c"]
    q = f"SELECT * FROM price_list{where_sql} ORDER BY category, name LIMIT ? OFFSET ?"
    rows = db.execute(q, params + [PER_PAGE, page_offset(page)]).fetchall()
    inv_items = db.execute("SELECT id, name, cost_price FROM inventory_list WHERE active=true AND category='Retail' ORDER BY name").fetchall()
    flagged_price, _ = logic.retail_consistency_flags(db)
    return render_template("price_list.html", items=rows, categories=PRICE_CATEGORIES, active_cat=cat,
                            inv_items=inv_items, search=search, flagged_price=flagged_price,
                            page=page, total_pages=page_count(total), total_count=total)


@app.route("/price-list/new", methods=["POST"])
@auth.permission_required("manage_price_list")
def price_list_new():
    db = get_db()
    f = request.form
    try:
        cost_price = parse_money(f.get("cost_price"))
        sale_price = parse_money(f.get("sale_price"))
    except BadNumber:
        flash("Cost Price and Sale Price must be valid numbers.", "error")
        return redirect(url_for("price_list"))
    if has_negative(cost_price, sale_price):
        flash("Cost Price and Sale Price can't be negative.", "error")
        return redirect(url_for("price_list"))
    if f.get("category") not in PRICE_CATEGORIES:
        flash("Category must be one of: " + ", ".join(PRICE_CATEGORIES) + ".", "error")
        return redirect(url_for("price_list"))
    linked_item_id = f.get("linked_item_id") or None
    if linked_item_id:
        # Two active rows linking to the same inventory item makes POS
        # pricing nondeterministic — item_sale_price() picks whichever one
        # a plain LIMIT 1 happens to return, with no ordering guarantee,
        # so the same product could ring up at two different prices with
        # no error or warning telling staff the catalog is inconsistent.
        existing_link = db.execute(
            "SELECT id, name FROM price_list WHERE linked_item_id=? AND active=true", (linked_item_id,)
        ).fetchone()
        if existing_link:
            flash(f"That inventory item is already linked to {existing_link['id']} ({existing_link['name']}) — "
                  f"an item can only be linked from one active Price List row at a time.", "error")
            return redirect(url_for("price_list"))
    pid = dbmod.next_id(db, "P")
    can_discount = f.get("can_discount") == "on"
    db.execute(
        "INSERT INTO price_list (id,name,category,cost_price,sale_price,notes,active,linked_item_id,can_discount) VALUES (?,?,?,?,?,?,true,?,?)",
        (pid, f["name"], f["category"], cost_price, sale_price,
         f.get("notes"), linked_item_id, can_discount),
    )
    auth.log_change(db, "price_list", pid, "create")
    db.commit()
    flash(f"{pid} added to price list.", "success")
    return redirect(url_for("price_list"))


@app.route("/price-list/<item_id>/edit", methods=["POST"])
@auth.permission_required("manage_price_list")
def price_list_edit(item_id):
    db = get_db()
    f = request.form
    try:
        cost_price = parse_money(f.get("cost_price"))
        sale_price = parse_money(f.get("sale_price"))
    except BadNumber:
        flash("Cost Price and Sale Price must be valid numbers.", "error")
        return redirect(url_for("price_list"))
    if has_negative(cost_price, sale_price):
        flash("Cost Price and Sale Price can't be negative.", "error")
        return redirect(url_for("price_list"))
    if f.get("category") not in PRICE_CATEGORIES:
        flash("Category must be one of: " + ", ".join(PRICE_CATEGORIES) + ".", "error")
        return redirect(url_for("price_list"))
    old = db.execute("SELECT * FROM price_list WHERE id=?", (item_id,)).fetchone()
    if not old:
        flash("Price list item not found.", "error")
        return redirect(url_for("price_list"))
    new_linked_item_id = (f.get("linked_item_id") or None) if "linked_item_id" in f else old["linked_item_id"]
    if new_linked_item_id and new_linked_item_id != old["linked_item_id"]:
        dup = db.execute(
            "SELECT id, name FROM price_list WHERE linked_item_id=? AND active=true AND id != ?",
            (new_linked_item_id, item_id),
        ).fetchone()
        if dup:
            flash(f"That inventory item is already linked to {dup['id']} ({dup['name']}) — "
                  f"an item can only be linked from one active Price List row at a time.", "error")
            return redirect(url_for("price_list"))
    new_vals = {"name": f["name"], "category": f["category"], "cost_price": cost_price,
                "sale_price": sale_price, "notes": f.get("notes"),
                "linked_item_id": new_linked_item_id,
                "can_discount": f.get("can_discount") == "on"}
    changes = auth.diff_dict(old, new_vals)
    db.execute("UPDATE price_list SET name=?, category=?, cost_price=?, sale_price=?, notes=?, linked_item_id=?, can_discount=? WHERE id=?",
              (*new_vals.values(), item_id))
    if "cost_price" in changes or "sale_price" in changes:
        # Billing/inpatient revenue and COGS are computed against the
        # *current* Price List value, not one frozen at transaction time —
        # so a cost/sale price edit can retroactively change any past
        # month that ever billed this code. Full rebuild is the only way
        # to know which months without re-scanning anyway.
        logic.recompute_full_summary(db)
    auth.log_change(db, "price_list", item_id, "update", changes)
    db.commit()
    flash("Price updated.", "success")
    return redirect(url_for("price_list"))


@app.route("/price-list/bulk-edit", methods=["POST"])
@auth.permission_required("manage_price_list")
def price_list_bulk_edit():
    """
    Saves many Price List row edits in a single request instead of one
    request per row. This matters a lot at scale: each row edit that
    touches cost_price/sale_price triggers a full recompute of the
    materialized financial summary (since billing/inpatient revenue and
    COGS are looked up against the *current* Price List value — see
    logic._revenue_and_cogs_by_month) — that full recompute is cheap once,
    but doing it 50 separate times back-to-back for a 50-row bulk edit is
    what actually caused the lag. Batching means it runs at most once
    total, in one DB transaction, with one response instead of 50 full
    page redirects being fetched and thrown away by the browser.
    """
    db = get_db()
    payload = request.get_json(silent=True) or {}
    items = payload.get("items") or []
    saved, errors = [], {}
    any_price_changed = False
    claimed_in_batch = {}
    for item in items:
        item_id = str(item.get("id", ""))
        fields = item.get("fields") or {}
        try:
            cost_price = parse_money(fields.get("cost_price"))
            sale_price = parse_money(fields.get("sale_price"))
        except BadNumber:
            errors[item_id] = "Cost Price and Sale Price must be valid numbers."
            continue
        if has_negative(cost_price, sale_price):
            errors[item_id] = "Cost Price and Sale Price can't be negative."
            continue
        old = db.execute("SELECT * FROM price_list WHERE id=?", (item_id,)).fetchone()
        if not old:
            errors[item_id] = "Item not found."
            continue
        new_linked_item_id = (fields.get("linked_item_id") or None) if "linked_item_id" in fields else old["linked_item_id"]
        if new_linked_item_id:
            # Checked against both the database (another row, unrelated to
            # this batch) and what this same batch has already claimed (two
            # rows in one bulk save both trying to link the same item).
            dup = db.execute(
                "SELECT id FROM price_list WHERE linked_item_id=? AND active=true AND id != ?",
                (new_linked_item_id, item_id),
            ).fetchone()
            dup_id = dup["id"] if dup else claimed_in_batch.get(new_linked_item_id)
            if dup_id and dup_id != item_id:
                errors[item_id] = f"That inventory item is already linked to {dup_id} — an item can only be linked from one active row at a time."
                continue
            claimed_in_batch[new_linked_item_id] = item_id
        new_vals = {"name": fields.get("name", ""), "category": fields.get("category", ""),
                    "cost_price": cost_price, "sale_price": sale_price, "notes": fields.get("notes"),
                    "linked_item_id": new_linked_item_id,
                    "can_discount": fields.get("can_discount") == "on"}
        changes = auth.diff_dict(old, new_vals)
        db.execute(
            "UPDATE price_list SET name=?, category=?, cost_price=?, sale_price=?, notes=?, linked_item_id=?, can_discount=? WHERE id=?",
            (*new_vals.values(), item_id),
        )
        if "cost_price" in changes or "sale_price" in changes:
            any_price_changed = True
        auth.log_change(db, "price_list", item_id, "update", changes)
        saved.append(item_id)
    if any_price_changed:
        logic.recompute_full_summary(db)
    db.commit()
    return jsonify({"ok": len(errors) == 0, "saved": saved, "errors": errors})


@app.route("/price-list/<item_id>/delete", methods=["POST"])
@auth.permission_required("manage_price_list")
def price_list_delete(item_id):
    db = get_db()
    db.execute("UPDATE price_list SET active=false WHERE id=?", (item_id,))
    auth.log_change(db, "price_list", item_id, "delete")
    db.commit()
    flash("Item removed from price list.", "success")
    return redirect(url_for("price_list"))


# ---------------------------------------------------------------------------
# Inventory catalog
# ---------------------------------------------------------------------------
INVENTORY_CATEGORIES = ["Medical", "Retail"]

# Mirror the DB CHECK constraints (schema_postgres.sql) so a bypassed <select>
# produces a clean flash message instead of a raw constraint-violation 500.
RESOURCE_TYPES = ["vet", "grooming"]
APPOINTMENT_TYPES = ["Medical", "Grooming"]
BILLING_TYPES = ["Automatic", "Manual"]


@app.route("/inventory-catalog")
def inventory_catalog():
    db = get_db()
    show_inactive = request.args.get("inactive") == "1"
    search = request.args.get("q", "").strip()
    page = get_page()
    where = []
    params = []
    if not show_inactive:
        where.append("i.active=true")
    if search:
        where.append("i.name ILIKE ?")
        params.append(f"%{search}%")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    total = db.execute(f"SELECT COUNT(*) c FROM inventory_list i{where_sql}", params).fetchone()["c"]
    q = ("SELECT i.*, d.name as distributor_name FROM inventory_list i LEFT JOIN distributors d ON d.id=i.distributor_id"
         + where_sql + " ORDER BY i.category, i.name LIMIT ? OFFSET ?")
    rows = db.execute(q, params + [PER_PAGE, page_offset(page)]).fetchall()
    distributors = db.execute("SELECT * FROM distributors ORDER BY name").fetchall()
    _, flagged_inventory = logic.retail_consistency_flags(db)
    has_barcodes = db.execute(
        "SELECT EXISTS(SELECT 1 FROM inventory_list WHERE barcode IS NOT NULL AND active=true) AS e"
    ).fetchone()["e"]
    return render_template("inventory_catalog.html", items=rows, distributors=distributors,
                            show_inactive=show_inactive, categories=INVENTORY_CATEGORIES, search=search,
                            flagged_inventory=flagged_inventory, has_barcodes=has_barcodes,
                            page=page, total_pages=page_count(total), total_count=total)


@app.route("/inventory-catalog/new", methods=["POST"])
def inventory_catalog_new():
    db = get_db()
    f = request.form
    try:
        cost_price = parse_money(f.get("cost_price"))
    except BadNumber:
        flash("Cost Price must be a valid number.", "error")
        return redirect(url_for("inventory_catalog"))
    if f.get("category", "Medical") not in INVENTORY_CATEGORIES:
        flash("Category must be one of: " + ", ".join(INVENTORY_CATEGORIES) + ".", "error")
        return redirect(url_for("inventory_catalog"))
    iid = dbmod.next_id(db, "INV")
    db.execute(
        "INSERT INTO inventory_list (id,name,category,unit,track_expiry,cost_price,distributor_id,active,notes) "
        "VALUES (?,?,?,?,?,?,?,true,?)",
        (iid, f["name"], f.get("category", "Medical"), f.get("unit"), f.get("track_expiry") == "on",
         cost_price, f.get("distributor_id") or None, f.get("notes")),
    )
    auth.log_change(db, "inventory_list", iid, "create")
    db.commit()
    flash(f"{iid} added to inventory catalog.", "success")
    return redirect(url_for("inventory_catalog"))


@app.route("/inventory-catalog/<item_id>/edit", methods=["POST"])
def inventory_catalog_edit(item_id):
    db = get_db()
    f = request.form
    try:
        cost_price = parse_money(f.get("cost_price"))
    except BadNumber:
        flash("Cost Price must be a valid number.", "error")
        return redirect(url_for("inventory_catalog"))
    if has_negative(cost_price):
        flash("Cost Price can't be negative.", "error")
        return redirect(url_for("inventory_catalog"))
    if f.get("category", "Medical") not in INVENTORY_CATEGORIES:
        flash("Category must be one of: " + ", ".join(INVENTORY_CATEGORIES) + ".", "error")
        return redirect(url_for("inventory_catalog"))
    old = db.execute("SELECT * FROM inventory_list WHERE id=?", (item_id,)).fetchone()
    new_vals = {"name": f["name"], "category": f.get("category", "Medical"), "unit": f.get("unit"),
                "track_expiry": f.get("track_expiry") == "on", "cost_price": cost_price,
                "distributor_id": f.get("distributor_id") or None,
                "notes": f.get("notes", old["notes"]), "active": old["active"]}
    changes = auth.diff_dict(old, new_vals)
    db.execute(
        "UPDATE inventory_list SET name=?, category=?, unit=?, track_expiry=?, cost_price=?, distributor_id=?, notes=?, active=? WHERE id=?",
        (*new_vals.values(), item_id),
    )
    if "cost_price" in changes:
        # Retail COGS is computed against the *current* inventory cost_price,
        # not a value frozen at sale time — so this can retroactively change
        # COGS for any past month that ever sold or refunded this item.
        logic.recompute_full_summary(db)
    auth.log_change(db, "inventory_list", item_id, "update", changes)
    db.commit()
    flash("Inventory item updated.", "success")
    return redirect(url_for("inventory_catalog"))


@app.route("/inventory-catalog/bulk-edit", methods=["POST"])
def inventory_catalog_bulk_edit():
    """Same batching rationale as price_list_bulk_edit — see that route's
    docstring. One request, one transaction, at most one financial-summary
    recompute for the whole batch instead of one per row."""
    db = get_db()
    payload = request.get_json(silent=True) or {}
    items = payload.get("items") or []
    saved, errors = [], {}
    any_cost_changed = False
    for item in items:
        item_id = str(item.get("id", ""))
        fields = item.get("fields") or {}
        try:
            cost_price = parse_money(fields.get("cost_price"))
        except BadNumber:
            errors[item_id] = "Cost Price must be a valid number."
            continue
        old = db.execute("SELECT * FROM inventory_list WHERE id=?", (item_id,)).fetchone()
        if not old:
            errors[item_id] = "Item not found."
            continue
        new_vals = {"name": fields.get("name", ""), "category": fields.get("category", "Medical"),
                    "unit": fields.get("unit"), "track_expiry": fields.get("track_expiry") == "on",
                    "cost_price": cost_price, "distributor_id": fields.get("distributor_id") or None,
                    "notes": fields.get("notes", old["notes"]), "active": old["active"]}
        changes = auth.diff_dict(old, new_vals)
        db.execute(
            "UPDATE inventory_list SET name=?, category=?, unit=?, track_expiry=?, cost_price=?, distributor_id=?, notes=?, active=? WHERE id=?",
            (*new_vals.values(), item_id),
        )
        if "cost_price" in changes:
            any_cost_changed = True
        auth.log_change(db, "inventory_list", item_id, "update", changes)
        saved.append(item_id)
    if any_cost_changed:
        logic.recompute_full_summary(db)
    db.commit()
    return jsonify({"ok": len(errors) == 0, "saved": saved, "errors": errors})


@app.route("/inventory-catalog/<item_id>/toggle-active", methods=["POST"])
def inventory_catalog_toggle(item_id):
    db = get_db()
    row = db.execute("SELECT active FROM inventory_list WHERE id=?", (item_id,)).fetchone()
    if row is None:
        flash("Item not found.", "error")
        return redirect(url_for("inventory_catalog"))
    new_val = not row["active"]
    db.execute("UPDATE inventory_list SET active=? WHERE id=?", (new_val, item_id))
    auth.log_change(db, "inventory_list", item_id, "update", {"active": (row["active"], new_val)})
    db.commit()
    flash("Item " + ("reactivated." if new_val else "deactivated."), "success")
    return redirect(url_for("inventory_catalog"))


@app.route("/inventory-catalog/<item_id>/create-barcode", methods=["POST"])
def inventory_catalog_create_barcode(item_id):
    db = get_db()
    item = db.execute("SELECT barcode FROM inventory_list WHERE id=?", (item_id,)).fetchone()
    if not item:
        flash("Item not found.", "error")
        return redirect(url_for("inventory_catalog"))
    if item["barcode"]:
        flash("This item already has a barcode. Remove it first if you want a different one.", "error")
        return redirect(url_for("inventory_barcode_label", item_id=item_id))
    code = barcode_mod.generate_barcode(db)
    # The check above is a friendly fast-path, not the real guarantee — two
    # concurrent "Generate" clicks racing into the same candidate code (low
    # but non-zero probability) would otherwise surface as a raw 500 instead
    # of a friendly error; inventory_list.barcode is DB-UNIQUE, so the loser
    # raises instead of corrupting anything.
    try:
        db.execute("UPDATE inventory_list SET barcode=? WHERE id=?", (code, item_id))
    except dbmod.IntegrityError:
        db.rollback()
        flash("That code was just claimed by another item — try again.", "error")
        return redirect(url_for("inventory_catalog"))
    auth.log_change(db, "inventory_list", item_id, "update", {"barcode": (None, code)})
    db.commit()
    flash(f"Barcode {code} created.", "success")
    return redirect(url_for("inventory_barcode_label", item_id=item_id))


@app.route("/inventory-catalog/<item_id>/barcode-label")
def inventory_barcode_label(item_id):
    db = get_db()
    item = db.execute("SELECT * FROM inventory_list WHERE id=?", (item_id,)).fetchone()
    if not item or not item["barcode"]:
        flash("This item doesn't have a barcode yet.", "error")
        return redirect(url_for("inventory_catalog"))
    return render_template("barcode_label.html", item=item)


@app.route("/inventory-catalog/barcodes/generated")
def inventory_catalog_barcodes_generated():
    """Every active item that has a barcode, across the whole catalog
    regardless of which page of Inventory Catalog is showing — feeds the
    Bulk Barcode Print picker. Unlike VetClinicSystem_IQ, Jordan has no
    barcode_source column distinguishing 'generated' from a manually
    entered code — there's no manual-barcode-entry feature here at all,
    every barcode is created via inventory_catalog_create_barcode — so any
    item with a non-null barcode is eligible."""
    db = get_db()
    rows = db.execute(
        "SELECT id, name, barcode FROM inventory_list "
        "WHERE barcode IS NOT NULL AND active=true ORDER BY name"
    ).fetchall()
    return jsonify([{"id": r["id"], "name": r["name"], "barcode": r["barcode"]} for r in rows])


@app.route("/inventory-catalog/barcodes/bulk-print", methods=["POST"])
def inventory_catalog_barcodes_bulk_print():
    db = get_db()
    try:
        requested = json.loads(request.form.get("items") or "[]")
    except (ValueError, TypeError):
        requested = []
    labels = []
    for entry in requested if isinstance(requested, list) else []:
        if not isinstance(entry, dict):
            continue
        item_id = str(entry.get("id", ""))
        try:
            qty = int(entry.get("qty", 1))
        except (TypeError, ValueError):
            qty = 1
        qty = max(1, min(qty, 500))
        # Re-checked server-side, same as everywhere else a client-supplied
        # id gets acted on — only an item that currently has a barcode can
        # end up on the printed sheet, no matter what the client sent.
        item = db.execute(
            "SELECT id, name, barcode FROM inventory_list WHERE id=? AND barcode IS NOT NULL",
            (item_id,),
        ).fetchone()
        if item and item["barcode"]:
            labels.append({"id": item["id"], "name": item["name"], "barcode": item["barcode"], "qty": qty})
    if not labels:
        flash("No barcodes selected to print.", "error")
        return redirect(url_for("inventory_catalog"))
    return render_template("barcode_bulk_print.html", labels=labels)


# ---------------------------------------------------------------------------
# Distributors
# ---------------------------------------------------------------------------
@app.route("/distributors")
def distributors_list():
    db = get_db()
    search = request.args.get("q", "").strip()
    if search:
        rows = db.execute("SELECT * FROM distributors WHERE name ILIKE ? ORDER BY name", (f"%{search}%",)).fetchall()
    else:
        rows = db.execute("SELECT * FROM distributors ORDER BY name").fetchall()
    outstanding = logic.distributor_outstanding_totals(db)
    payables = logic.distributor_payables_summary(db)
    return render_template("distributors.html", distributors=rows, search=search,
                            outstanding=outstanding, payables=payables)


@app.route("/distributors/new", methods=["POST"])
def distributor_new():
    db = get_db()
    f = request.form
    try:
        phone = normalize_phone(f.get("phone"))
    except BadPhone:
        flash("That phone number doesn't look valid — check the digits and try again.", "error")
        return redirect(url_for("distributors_list"))
    try:
        lead_time_days = parse_int(f.get("lead_time_days"))
    except BadNumber:
        flash("Lead Time (Days) must be a whole number.", "error")
        return redirect(url_for("distributors_list"))
    did = dbmod.next_id(db, "D")
    db.execute(
        "INSERT INTO distributors (id,name,contact_person,phone,email,catalog_link,lead_time_days,payment_terms,notes) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (did, f["name"], f.get("contact_person"), phone, f.get("email"), f.get("catalog_link"),
         lead_time_days, f.get("payment_terms"), f.get("notes")),
    )
    auth.log_change(db, "distributors", did, "create")
    db.commit()
    flash(f"{did} added.", "success")
    return redirect(url_for("distributors_list"))


@app.route("/distributors/<dist_id>/edit", methods=["POST"])
def distributor_edit(dist_id):
    db = get_db()
    f = request.form
    try:
        phone = normalize_phone(f.get("phone"))
    except BadPhone:
        flash("That phone number doesn't look valid — check the digits and try again.", "error")
        return redirect(url_for("distributors_list"))
    try:
        lead_time_days = parse_int(f.get("lead_time_days"))
    except BadNumber:
        flash("Lead Time (Days) must be a whole number.", "error")
        return redirect(url_for("distributors_list"))
    old = db.execute("SELECT * FROM distributors WHERE id=?", (dist_id,)).fetchone()
    new_vals = {"name": f["name"], "contact_person": f.get("contact_person"), "phone": phone,
                "email": f.get("email"), "catalog_link": f.get("catalog_link"),
                "lead_time_days": lead_time_days, "payment_terms": f.get("payment_terms"),
                "notes": f.get("notes")}
    changes = auth.diff_dict(old, new_vals)
    db.execute(
        "UPDATE distributors SET name=?, contact_person=?, phone=?, email=?, catalog_link=?, lead_time_days=?, payment_terms=?, notes=? WHERE id=?",
        (*new_vals.values(), dist_id),
    )
    auth.log_change(db, "distributors", dist_id, "update", changes)
    db.commit()
    flash("Distributor updated.", "success")
    return redirect(url_for("distributors_list"))


@app.route("/distributors/<dist_id>/delete", methods=["POST"])
def distributor_delete(dist_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM distributors WHERE id=?", (dist_id,)).fetchone():
        flash("Distributor not found.", "error")
        return redirect(url_for("distributors_list"))
    # A distributor can be referenced from inventory items and manual
    # ledger bills — a bare DELETE would just crash with a raw
    # ForeignKeyViolation the moment either has a row. Check first and
    # name what's still linked, rather than let Postgres reject it as an
    # unhandled 500.
    still_linked = []
    for label, table in [("inventory item(s)", "inventory_list"), ("distributor bill(s)", "distributor_bills")]:
        if db.execute(f"SELECT 1 FROM {table} WHERE distributor_id=? LIMIT 1", (dist_id,)).fetchone():
            still_linked.append(label)
    if still_linked:
        flash("Can't delete this distributor — it still has " + ", ".join(still_linked) +
              " linked to it. Remove or reassign those first.", "error")
        return redirect(url_for("distributors_list"))
    db.execute("DELETE FROM distributors WHERE id=?", (dist_id,))
    auth.log_change(db, "distributors", dist_id, "delete")
    db.commit()
    flash("Distributor deleted.", "success")
    return redirect(url_for("distributors_list"))


# ---------------------------------------------------------------------------
# Distributor Ledger — manual bookkeeping for what a distributor has billed
# you and what you've paid them. Lump-sum bills only, no link to inventory,
# POS, or any report; balance/status are always computed (never stored).
# ---------------------------------------------------------------------------
@app.route("/distributors/<dist_id>")
def distributor_detail(dist_id):
    db = get_db()
    dist = db.execute("SELECT * FROM distributors WHERE id=?", (dist_id,)).fetchone()
    if not dist:
        flash("Distributor not found.", "error")
        return redirect(url_for("distributors_list"))
    ledger = logic.distributor_ledger(db, dist_id)
    return render_template("distributor_detail.html", distributor=dist, **ledger)


@app.route("/distributors/<dist_id>/bills/new", methods=["POST"])
def distributor_bill_new(dist_id):
    db = get_db()
    f = request.form
    try:
        total_amount = parse_money(f.get("total_amount"), required=True)
    except BadNumber:
        flash("Total amount must be a valid number.", "error")
        return redirect(url_for("distributor_detail", dist_id=dist_id))
    if total_amount <= 0:
        flash("Total amount must be greater than zero.", "error")
        return redirect(url_for("distributor_detail", dist_id=dist_id))
    try:
        bill_date = clean_date(f.get("bill_date"), field="bill_date") or date.today().isoformat()
    except BadDate as e:
        flash(str(e), "error")
        return redirect(url_for("distributor_detail", dist_id=dist_id))
    bid = dbmod.next_id(db, "DB")
    db.execute(
        "INSERT INTO distributor_bills (id,distributor_id,bill_date,bill_reference,total_amount,notes,created_at,created_by) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (bid, dist_id, bill_date, f.get("bill_reference"), total_amount, f.get("notes"),
         datetime.now().isoformat(timespec="seconds"), session.get("user_id")),
    )
    auth.log_change(db, "distributor_bills", bid, "create")
    db.commit()
    flash(f"Bill {bid} logged.", "success")
    return redirect(url_for("distributor_detail", dist_id=dist_id))


@app.route("/distributors/<dist_id>/bills/<bill_id>/delete", methods=["POST"])
def distributor_bill_delete(dist_id, bill_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM distributor_bills WHERE id=? AND distributor_id=?", (bill_id, dist_id)).fetchone():
        flash("Bill not found.", "error")
        return redirect(url_for("distributor_detail", dist_id=dist_id))
    has_payments = db.execute(
        "SELECT 1 FROM distributor_bill_payments WHERE bill_id=? LIMIT 1", (bill_id,)
    ).fetchone()
    if has_payments:
        flash("Delete the payments on this bill first.", "error")
        return redirect(url_for("distributor_detail", dist_id=dist_id))
    db.execute("DELETE FROM distributor_bills WHERE id=? AND distributor_id=?", (bill_id, dist_id))
    auth.log_change(db, "distributor_bills", bill_id, "delete")
    db.commit()
    flash("Bill deleted.", "success")
    return redirect(url_for("distributor_detail", dist_id=dist_id))


@app.route("/distributors/<dist_id>/bills/<bill_id>/payments/new", methods=["POST"])
def distributor_payment_new(dist_id, bill_id):
    db = get_db()
    f = request.form
    # Locked before computing the balance, same reasoning as
    # pos_checkout()'s cart-item locking: without this, two payments each
    # individually within the balance shown at page-load could both pass
    # the check below and both insert, together overpaying the bill.
    bill = db.execute(
        "SELECT * FROM distributor_bills WHERE id=? AND distributor_id=? FOR UPDATE", (bill_id, dist_id)
    ).fetchone()
    if not bill:
        flash("Bill not found.", "error")
        return redirect(url_for("distributor_detail", dist_id=dist_id))
    try:
        amount = parse_money(f.get("amount"), required=True)
    except BadNumber:
        flash("Payment amount must be a valid number.", "error")
        return redirect(url_for("distributor_detail", dist_id=dist_id))
    if amount <= 0:
        flash("Payment amount must be greater than zero.", "error")
        return redirect(url_for("distributor_detail", dist_id=dist_id))
    paid_so_far = db.execute(
        "SELECT COALESCE(SUM(amount),0) s FROM distributor_bill_payments WHERE bill_id=?", (bill_id,)
    ).fetchone()["s"]
    balance = bill["total_amount"] - paid_so_far
    # The HTML max= on the amount field already stops this in the normal
    # UI, but that's client-side only — a crafted request or a stale page
    # (someone else already paid part of it) can still submit more than
    # what's actually left owed, which would flip the bill to a "Paid"
    # badge next to a negative balance with nothing indicating an
    # overpayment/credit happened.
    if amount > balance:
        flash(f"That's more than the remaining balance of {logic.fmt_money(balance)} JOD on this bill.", "error")
        return redirect(url_for("distributor_detail", dist_id=dist_id))
    try:
        payment_date = clean_date(f.get("payment_date"), field="payment_date") or date.today().isoformat()
    except BadDate as e:
        flash(str(e), "error")
        return redirect(url_for("distributor_detail", dist_id=dist_id))
    cur = db.execute(
        "INSERT INTO distributor_bill_payments (bill_id,amount,payment_date,method,notes,created_at,created_by) "
        "VALUES (?,?,?,?,?,?,?) RETURNING id",
        (bill_id, amount, payment_date, f.get("method"), f.get("notes"),
         datetime.now().isoformat(timespec="seconds"), session.get("user_id")),
    )
    pid = cur.fetchone()["id"]
    auth.log_change(db, "distributor_bill_payments", str(pid), "create")
    db.commit()
    flash("Payment recorded.", "success")
    return redirect(url_for("distributor_detail", dist_id=dist_id))


@app.route("/distributors/<dist_id>/payments/<int:payment_id>/delete", methods=["POST"])
def distributor_payment_delete(dist_id, payment_id):
    db = get_db()
    owned = db.execute(
        "SELECT 1 FROM distributor_bill_payments p JOIN distributor_bills b ON b.id = p.bill_id "
        "WHERE p.id=? AND b.distributor_id=?",
        (payment_id, dist_id),
    ).fetchone()
    if not owned:
        flash("Payment not found.", "error")
        return redirect(url_for("distributor_detail", dist_id=dist_id))
    db.execute("DELETE FROM distributor_bill_payments WHERE id=?", (payment_id,))
    auth.log_change(db, "distributor_bill_payments", str(payment_id), "delete")
    db.commit()
    flash("Payment deleted.", "success")
    return redirect(url_for("distributor_detail", dist_id=dist_id))


@app.route("/distributors/<dist_id>/export.pdf")
def distributor_export_pdf(dist_id):
    db = get_db()
    dist = db.execute("SELECT id FROM distributors WHERE id=?", (dist_id,)).fetchone()
    if not dist:
        flash("Distributor not found.", "error")
        return redirect(url_for("distributors_list"))
    buf = pdf_export.export_distributor_ledger(db, dist_id)
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=f"{dist_id}_ledger.pdf")


# ---------------------------------------------------------------------------
# Consignment — a distributor's stock sitting on your shelf; they're owed
# cost_price per unit once it sells, you keep the markup. Consignment
# items are ordinary Retail inventory_list rows (ownership_type=
# 'Consignment') and already flow through POS/audit/P&L unmodified — this
# section is the distributor-facing receiving/shrinkage/returns/
# settlement layer on top of that shared data.
# ---------------------------------------------------------------------------
@app.route("/consignment")
@auth.permission_required("view_consignment")
def consignment_overview():
    db = get_db()
    rows = logic.consignment_distributors_overview(db)
    return render_template("consignment_overview.html", rows=rows)


@app.route("/consignment/items")
@auth.permission_required("view_consignment")
def consignment_items():
    db = get_db()
    page = get_page()
    total = db.execute("SELECT COUNT(*) c FROM inventory_list WHERE category='Retail' AND active=true").fetchone()["c"]
    rows = db.execute(
        "SELECT i.*, d.name AS distributor_name FROM inventory_list i "
        "LEFT JOIN distributors d ON d.id = i.distributor_id "
        "WHERE i.category='Retail' AND i.active=true ORDER BY i.ownership_type DESC, i.name LIMIT ? OFFSET ?",
        (PER_PAGE, page_offset(page)),
    ).fetchall()
    distributors = db.execute("SELECT * FROM distributors ORDER BY name").fetchall()
    locked = {r["id"]: logic.consignment_item_locked(db, r["id"]) for r in rows if r["ownership_type"] == "Consignment"}
    return render_template("consignment_items.html", items=rows, distributors=distributors, locked=locked,
                            page=page, total_pages=page_count(total), total_count=total)


@app.route("/consignment/items/bulk-edit", methods=["POST"])
@auth.permission_required("manage_consignment_items")
def consignment_items_bulk_edit():
    """
    Same batching rationale as inventory_catalog_bulk_edit() — one
    request, one transaction, instead of one click (and full page reload)
    per item. Flips ownership_type via an inline "Consignment?" checkbox +
    Distributor + Cost Price, saved together through the shared
    unsaved-changes.js Save Changes button, the same pattern Inventory
    Catalog's Track Expiry column already uses.

    A locked item (real receiving/sale/settlement activity already
    against it) is skipped entirely, silently — its checkbox/distributor
    are disabled client-side so a normal user can't reach this, but
    nothing here trusts that alone.
    """
    db = get_db()
    payload = request.get_json(silent=True) or {}
    items = payload.get("items") or []
    saved, errors = [], {}
    for item in items:
        item_id = str(item.get("id", ""))
        fields = item.get("fields") or {}
        old = db.execute("SELECT * FROM inventory_list WHERE id=?", (item_id,)).fetchone()
        if not old or old["category"] != "Retail":
            errors[item_id] = "Item not found."
            continue
        if logic.consignment_item_locked(db, item_id):
            continue
        want_consignment = fields.get("is_consignment") == "on"
        if want_consignment:
            distributor_id = fields.get("distributor_id") or None
            if not distributor_id:
                errors[item_id] = "Pick a distributor to flag this item as Consignment."
                continue
            try:
                cost_price = parse_money(fields.get("cost_price"), required=True)
            except BadNumber:
                errors[item_id] = "Cost Price is required and must be a valid number to flag an item as Consignment."
                continue
            if cost_price < 0:
                errors[item_id] = "Cost Price can't be negative."
                continue
            consignment_since = (
                old["consignment_since"] if old["ownership_type"] == "Consignment"
                else datetime.now().isoformat(timespec="seconds")
            )
            new_vals = {
                "ownership_type": "Consignment", "distributor_id": distributor_id,
                "cost_price": cost_price, "consignment_since": consignment_since,
            }
        else:
            new_vals = {
                "ownership_type": "Owned", "distributor_id": None,
                "cost_price": old["cost_price"], "consignment_since": old["consignment_since"],
            }
        changes = auth.diff_dict(old, new_vals)
        if not changes:
            continue
        db.execute(
            "UPDATE inventory_list SET ownership_type=?, distributor_id=?, cost_price=?, consignment_since=? WHERE id=?",
            (new_vals["ownership_type"], new_vals["distributor_id"], new_vals["cost_price"],
             new_vals["consignment_since"], item_id),
        )
        auth.log_change(db, "inventory_list", item_id, "update", changes)
        saved.append(item_id)
    db.commit()
    return jsonify({"ok": len(errors) == 0, "saved": saved, "errors": errors})


def _consignment_item_choices(db):
    """Consignment items for the Receiving/Shrinkage/Returns pickers,
    each with its distributor attached so the form can filter/label."""
    return db.execute(
        "SELECT i.id, i.name, i.unit, i.cost_price, i.distributor_id, d.name AS distributor_name "
        "FROM inventory_list i JOIN distributors d ON d.id = i.distributor_id "
        "WHERE i.ownership_type='Consignment' AND i.active=true ORDER BY d.name, i.name"
    ).fetchall()


@app.route("/consignment/receiving")
@auth.permission_required("view_consignment")
def consignment_receiving_page():
    db = get_db()
    page = get_page()
    total = db.execute("SELECT COUNT(*) c FROM consignment_receipts").fetchone()["c"]
    rows = db.execute(
        "SELECT cr.*, i.name AS item_name, d.name AS distributor_name FROM consignment_receipts cr "
        "JOIN inventory_list i ON i.id=cr.item_id JOIN distributors d ON d.id=cr.distributor_id "
        "ORDER BY cr.created_at DESC LIMIT ? OFFSET ?", (PER_PAGE, page_offset(page)),
    ).fetchall()
    return render_template("consignment_receiving.html", receipts=rows, items=_consignment_item_choices(db),
                            page=page, total_pages=page_count(total), total_count=total)


@app.route("/consignment/receiving/new", methods=["POST"])
@auth.permission_required("manage_consignment_stock")
def consignment_receiving_new():
    db = get_db()
    f = request.form
    item_id = f.get("item_id")
    item = db.execute("SELECT * FROM inventory_list WHERE id=? AND ownership_type='Consignment'", (item_id,)).fetchone()
    if not item:
        flash("Pick a Consignment item first.", "error")
        return redirect(url_for("consignment_receiving_page"))
    try:
        quantity = parse_money(f.get("quantity"), required=True)
        unit_cost = parse_money(f.get("unit_cost"), required=True)
    except BadNumber:
        flash("Quantity and Unit Cost must be valid numbers.", "error")
        return redirect(url_for("consignment_receiving_page"))
    if quantity <= 0:
        flash("Quantity must be greater than 0.", "error")
        return redirect(url_for("consignment_receiving_page"))
    if unit_cost < 0:
        flash("Unit Cost can't be negative.", "error")
        return redirect(url_for("consignment_receiving_page"))
    try:
        received_date = clean_date(f.get("received_date"), field="received_date") or date.today().isoformat()
    except BadDate as e:
        flash(str(e), "error")
        return redirect(url_for("consignment_receiving_page"))
    logic.record_consignment_receipt(db, item_id, item["distributor_id"], quantity, unit_cost,
                                      received_date, f.get("delivery_reference"), f.get("notes"), session["user_id"])
    auth.log_change(db, "consignment_receipts", item_id, "create")
    db.commit()
    flash(f"Received {quantity:g} {item['name']}.", "success")
    return redirect(url_for("consignment_receiving_page"))


@app.route("/consignment/shrinkage")
@auth.permission_required("view_consignment")
def consignment_shrinkage_page():
    db = get_db()
    page = get_page()
    total = db.execute("SELECT COUNT(*) c FROM consignment_shrinkage").fetchone()["c"]
    rows = db.execute(
        "SELECT cs.*, i.name AS item_name, d.name AS distributor_name FROM consignment_shrinkage cs "
        "JOIN inventory_list i ON i.id=cs.item_id JOIN distributors d ON d.id=cs.distributor_id "
        "ORDER BY cs.logged_at DESC LIMIT ? OFFSET ?", (PER_PAGE, page_offset(page)),
    ).fetchall()
    return render_template("consignment_shrinkage.html", lines=rows, items=_consignment_item_choices(db),
                            page=page, total_pages=page_count(total), total_count=total)


@app.route("/consignment/shrinkage/new", methods=["POST"])
@auth.permission_required("manage_consignment_stock")
def consignment_shrinkage_new():
    db = get_db()
    f = request.form
    item_id = f.get("item_id")
    item = db.execute("SELECT * FROM inventory_list WHERE id=? AND ownership_type='Consignment'", (item_id,)).fetchone()
    if not item:
        flash("Pick a Consignment item first.", "error")
        return redirect(url_for("consignment_shrinkage_page"))
    try:
        quantity = parse_money(f.get("quantity"), required=True)
    except BadNumber:
        flash("Quantity must be a valid number.", "error")
        return redirect(url_for("consignment_shrinkage_page"))
    if quantity <= 0:
        flash("Quantity must be greater than 0.", "error")
        return redirect(url_for("consignment_shrinkage_page"))
    reason = f.get("reason")
    if reason not in ("Damaged", "Expired", "Other"):
        flash("Reason must be Damaged, Expired, or Other.", "error")
        return redirect(url_for("consignment_shrinkage_page"))
    # Default liability by reason: Expired defaults to Distributor (bad
    # stock rotation on their end), Damaged/Other default to Clinic
    # (mishandled on-site) — either can be overridden per line.
    default_liable = "Distributor" if reason == "Expired" else "Clinic"
    liable_party = f.get("liable_party") or default_liable
    if liable_party not in ("Distributor", "Clinic"):
        flash("Liable Party must be Distributor or Clinic.", "error")
        return redirect(url_for("consignment_shrinkage_page"))
    overridden = liable_party != default_liable
    ok, _, error = logic.record_consignment_shrinkage(
        db, item_id, item["distributor_id"], quantity, reason, liable_party, overridden,
        f.get("notes"), session["user_id"],
    )
    if not ok:
        flash(error, "error")
        return redirect(url_for("consignment_shrinkage_page"))
    auth.log_change(db, "consignment_shrinkage", item_id, "create")
    db.commit()
    flash(f"Logged {quantity:g} {item['name']} as shrinkage ({liable_party} liable).", "success")
    return redirect(url_for("consignment_shrinkage_page"))


@app.route("/consignment/returns")
@auth.permission_required("view_consignment")
def consignment_returns_page():
    db = get_db()
    page = get_page()
    total = db.execute("SELECT COUNT(*) c FROM consignment_returns").fetchone()["c"]
    rows = db.execute(
        "SELECT cr.*, i.name AS item_name, d.name AS distributor_name FROM consignment_returns cr "
        "JOIN inventory_list i ON i.id=cr.item_id JOIN distributors d ON d.id=cr.distributor_id "
        "ORDER BY cr.created_at DESC LIMIT ? OFFSET ?", (PER_PAGE, page_offset(page)),
    ).fetchall()
    return render_template("consignment_returns.html", returns=rows, items=_consignment_item_choices(db),
                            page=page, total_pages=page_count(total), total_count=total)


@app.route("/consignment/returns/new", methods=["POST"])
@auth.permission_required("manage_consignment_stock")
def consignment_returns_new():
    db = get_db()
    f = request.form
    item_id = f.get("item_id")
    item = db.execute("SELECT * FROM inventory_list WHERE id=? AND ownership_type='Consignment'", (item_id,)).fetchone()
    if not item:
        flash("Pick a Consignment item first.", "error")
        return redirect(url_for("consignment_returns_page"))
    try:
        quantity = parse_money(f.get("quantity"), required=True)
    except BadNumber:
        flash("Quantity must be a valid number.", "error")
        return redirect(url_for("consignment_returns_page"))
    if quantity <= 0:
        flash("Quantity must be greater than 0.", "error")
        return redirect(url_for("consignment_returns_page"))
    try:
        return_date = clean_date(f.get("return_date"), field="return_date") or date.today().isoformat()
    except BadDate as e:
        flash(str(e), "error")
        return redirect(url_for("consignment_returns_page"))
    ok, _, error = logic.record_consignment_return(
        db, item_id, item["distributor_id"], quantity, return_date, f.get("reason"), f.get("notes"), session["user_id"],
    )
    if not ok:
        flash(error, "error")
        return redirect(url_for("consignment_returns_page"))
    auth.log_change(db, "consignment_returns", item_id, "create")
    db.commit()
    flash(f"Returned {quantity:g} {item['name']} to {item['distributor_id']}.", "success")
    return redirect(url_for("consignment_returns_page"))


@app.route("/consignment/sales")
@auth.permission_required("view_consignment")
def consignment_sales_page():
    db = get_db()
    distributor_id = request.args.get("distributor_id") or None
    date_from = request.args.get("date_from") or None
    date_to = request.args.get("date_to") or None
    all_rows = logic.consignment_sales_by_distributor(db, distributor_id, date_from, date_to)
    page = get_page()
    total = len(all_rows)
    rows = all_rows[page_offset(page):page_offset(page) + PER_PAGE]
    distributors = db.execute(
        "SELECT DISTINCT d.id, d.name FROM distributors d JOIN inventory_list i ON i.distributor_id=d.id "
        "WHERE i.ownership_type='Consignment' ORDER BY d.name"
    ).fetchall()
    return render_template("consignment_sales.html", rows=rows, distributors=distributors,
                            distributor_id=distributor_id, date_from=date_from or "", date_to=date_to or "",
                            page=page, total_pages=page_count(total), total_count=total)


@app.route("/consignment/settlements/<distributor_id>")
@auth.permission_required("manage_consignment_settlements")
def consignment_settlements_page(distributor_id):
    db = get_db()
    distributor = db.execute("SELECT * FROM distributors WHERE id=?", (distributor_id,)).fetchone()
    if not distributor:
        flash("Distributor not found.", "error")
        return redirect(url_for("consignment_overview"))
    balance = logic.consignment_balance(db, distributor_id)
    history = db.execute(
        "SELECT s.*, u.full_name AS settled_by_name FROM consignment_settlements s "
        "LEFT JOIN users u ON u.id=s.settled_by WHERE s.distributor_id=? ORDER BY s.created_at DESC",
        (distributor_id,),
    ).fetchall()
    return render_template("consignment_settlements.html", distributor=distributor, balance=balance, history=history)


@app.route("/consignment/settlements/<distributor_id>/new", methods=["POST"])
@auth.permission_required("manage_consignment_settlements")
def consignment_settlement_new(distributor_id):
    db = get_db()
    # Locked before computing the balance — consignment_balance() reads
    # whatever settlement was most recently committed as its starting
    # point, so without this, two near-simultaneous submissions (double-
    # click, a retried request) could both read the same "last
    # settlement" before either commits, both compute a balance covering
    # the identical sales window, and both insert as separate settlement
    # rows — crediting/paying out the same batch of sales twice. The lock
    # is purely a mutex here (nothing about the distributor row itself
    # changes); same technique record_consignment_shrinkage() and
    # record_consignment_return() already use on inventory_list rows.
    distributor = db.execute("SELECT * FROM distributors WHERE id=? FOR UPDATE", (distributor_id,)).fetchone()
    if not distributor:
        flash("Distributor not found.", "error")
        return redirect(url_for("consignment_overview"))
    # Recomputed fresh at submit time, not trusted from a hidden form
    # field — the balance is a live figure (more could have sold since
    # the page was opened) and this is a cash-recording action, not
    # something to take on faith from the client.
    balance = logic.consignment_balance(db, distributor_id)
    try:
        amount_paid = parse_money(request.form.get("amount_paid"), required=True)
    except BadNumber:
        flash("Amount Paid must be a valid number.", "error")
        return redirect(url_for("consignment_settlements_page", distributor_id=distributor_id))
    if amount_paid < 0:
        flash("Amount Paid can't be negative.", "error")
        return redirect(url_for("consignment_settlements_page", distributor_id=distributor_id))
    amount_paid = round(amount_paid, 3)
    cur = db.execute(
        "INSERT INTO consignment_settlements (distributor_id, period_start, period_end, amount_owed, amount_paid, "
        "payment_method, notes, settled_by, created_at) VALUES (?,?,?,?,?,?,?,?,?) RETURNING id",
        (distributor_id, balance["period_start"], balance["period_end"], balance["amount_owed"], amount_paid,
         request.form.get("payment_method"), request.form.get("notes"), session["user_id"],
         datetime.now().isoformat(timespec="seconds")),
    )
    settlement_id = cur.fetchone()["id"]
    auth.log_change(db, "consignment_settlements", str(settlement_id), "create")
    db.commit()
    residual = round(balance["amount_owed"] - amount_paid, 3)
    if residual > 0:
        flash(f"Settlement recorded: {logic.fmt_money(amount_paid)} JOD paid of "
              f"{logic.fmt_money(balance['amount_owed'])} JOD owed — {logic.fmt_money(residual)} JOD carries forward.", "success")
    else:
        flash(f"Settlement recorded: {logic.fmt_money(amount_paid)} JOD paid, settled in full.", "success")
    return redirect(url_for("consignment_settlements_page", distributor_id=distributor_id))


@app.route("/consignment/settlements/export/<int:settlement_id>")
@auth.permission_required("manage_consignment_settlements")
def consignment_settlement_export(settlement_id):
    db = get_db()
    settlement = db.execute("SELECT id FROM consignment_settlements WHERE id=?", (settlement_id,)).fetchone()
    if not settlement:
        flash("Settlement not found.", "error")
        return redirect(url_for("consignment_overview"))
    buf = pdf_export.export_consignment_settlement_pdf(db, settlement_id)
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=f"settlement_{settlement_id}.pdf")


# ---------------------------------------------------------------------------
# Cash Register — a unified daily view of every place money actually
# changed hands (POS sales, Visit/Inpatient/Boarding payments, refunds),
# built for end-of-day cash-up: compare what the system says came in
# against what's physically in the till. "Pay From Cash Register" logs
# manual cash leaving the drawer for a reason that isn't a refund (petty
# cash, paying a supplier directly out of the till). "Perform Audit"
# records what staff actually counted against the system's Cash total for
# that day and immutably logs the outcome (Deficit/Surplus/Perfect) — see
# logic.cash_register_* for the actual math.
# ---------------------------------------------------------------------------
@app.route("/cash-register")
@auth.permission_required("manage_cash_register")
def cash_register_page():
    db = get_db()
    day = request.args.get("date", "").strip() or date.today().isoformat()
    try:
        logic.parse_date(day)
    except ValueError:
        flash("That date wasn't valid — showing today instead.", "error")
        day = date.today().isoformat()
    ledger = logic.cash_register_ledger(db, day)
    totals = logic.cash_register_totals(db, day)
    payouts = logic.cash_register_payouts_for_day(db, day)
    latest_audit = logic.cash_register_latest_audit(db, day)
    return render_template("cash_register.html", day=day, ledger=ledger, totals=totals,
                            payouts=payouts, latest_audit=latest_audit)


@app.route("/cash-register/payout", methods=["POST"])
@auth.permission_required("manage_cash_register")
def cash_register_payout_new():
    db = get_db()
    f = request.form
    try:
        day = clean_date(f.get("day"), field="day") or date.today().isoformat()
    except BadDate as e:
        flash(str(e), "error")
        return redirect(url_for("cash_register_page"))
    try:
        amount = parse_money(f.get("amount"), required=True)
    except BadNumber:
        flash("Amount must be a valid number.", "error")
        return redirect(url_for("cash_register_page", date=day))
    if amount <= 0:
        flash("Amount must be greater than 0.", "error")
        return redirect(url_for("cash_register_page", date=day))
    reason = (f.get("reason") or "").strip()
    if not reason:
        flash("Enter a reason for this payout.", "error")
        return redirect(url_for("cash_register_page", date=day))
    cur = db.execute(
        "INSERT INTO cash_register_payouts (payout_date, amount, reason, logged_by, created_at) "
        "VALUES (?,?,?,?,?) RETURNING id",
        (day, amount, reason, session["user_id"], datetime.now().isoformat(timespec="seconds")),
    )
    payout_id = cur.fetchone()["id"]
    auth.log_change(db, "cash_register_payouts", str(payout_id), "create")
    db.commit()
    flash(f"{logic.fmt_money(amount)} JOD logged out of the register.", "success")
    return redirect(url_for("cash_register_page", date=day))


@app.route("/cash-register/audit", methods=["POST"])
@auth.permission_required("manage_cash_register")
def cash_register_audit_new():
    db = get_db()
    f = request.form
    try:
        day = clean_date(f.get("day"), field="day") or date.today().isoformat()
    except BadDate as e:
        flash(str(e), "error")
        return redirect(url_for("cash_register_page"))
    try:
        counted_cash = parse_money(f.get("counted_cash"), required=True)
    except BadNumber:
        flash("Counted cash must be a valid number.", "error")
        return redirect(url_for("cash_register_page", date=day))
    if counted_cash < 0:
        flash("Counted cash can't be negative.", "error")
        return redirect(url_for("cash_register_page", date=day))
    # Recomputed fresh here, never trusted from the form — same reasoning
    # as consignment_settlement_new(): this is the figure the audit result
    # gets permanently compared against, so it has to be the real live
    # number, not whatever the page happened to show when it was loaded.
    totals = logic.cash_register_totals(db, day)
    difference = round(counted_cash - totals["Cash"], 3)
    if abs(difference) < 1:
        status = "Perfect"
    elif difference < 0:
        status = "Deficit"
    else:
        status = "Surplus"
    cur = db.execute(
        "INSERT INTO cash_register_audits (audit_date, system_cash, system_card, system_transfer, "
        "counted_cash, difference, status, notes, performed_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?) RETURNING id",
        (day, totals["Cash"], totals["Card"], totals["Transfer"], counted_cash, difference, status,
         (f.get("notes") or "").strip() or None, session["user_id"], datetime.now().isoformat(timespec="seconds")),
    )
    audit_id = cur.fetchone()["id"]
    auth.log_change(db, "cash_register_audits", str(audit_id), "create")
    db.commit()
    if status == "Perfect":
        flash(f"Audit recorded for {day}: Perfect — counted cash matches the system exactly.", "success")
    else:
        flash(f"Audit recorded for {day}: {status} of {logic.fmt_money(abs(difference))} JOD.", "error")
    return redirect(url_for("cash_register_page", date=day))


# ---------------------------------------------------------------------------
# Inventory Status / Ordering Sheet
# ---------------------------------------------------------------------------
@app.route("/inventory-status")
def inventory_status_page():
    db = get_db()
    rows = logic.inventory_status(db)
    filter_ = request.args.get("filter")
    if filter_ == "low_stock":
        rows = [r for r in rows if r["stock_status"] == "LOW STOCK"]
    elif filter_ == "overdue":
        rows = [r for r in rows if r["audit_status"] in ("OVERDUE", "Never audited")]
    elif filter_ == "expiring":
        rows = [r for r in rows if r["expiry_status"] in ("EXPIRING SOON", "EXPIRED")]
    search = request.args.get("q", "").strip()
    if search:
        needle = search.lower()
        rows = [r for r in rows if needle in (r["name"] or "").lower()]
    return render_template("inventory_status.html", rows=rows, filter_=filter_, search=search)


@app.route("/ordering-sheet")
def ordering_sheet_page():
    db = get_db()
    rows = logic.ordering_sheet(db)
    return render_template("ordering_sheet.html", rows=rows)


# ---------------------------------------------------------------------------
# Audit sessions (whole-catalog Save / Confirm)
# ---------------------------------------------------------------------------
@app.route("/audit-history")
def audit_history_list():
    db = get_db()
    page = get_page()
    sessions, total = logic.list_audit_sessions(db, limit=PER_PAGE, offset=page_offset(page))
    return render_template("audit_sessions_list.html", sessions=sessions,
                            page=page, total_pages=page_count(total), total_count=total)


@app.route("/audit-history/start", methods=["POST"])
def audit_session_start():
    db = get_db()
    session_id = logic.get_or_create_draft_session(db, date.today().isoformat(), session["user_id"])
    return redirect(url_for("audit_session_view", session_id=session_id))


@app.route("/audit-history/session/<int:session_id>")
def audit_session_view(session_id):
    db = get_db()
    sess = db.execute("SELECT s.*, u.full_name as performed_by_name FROM audit_sessions s "
                      "LEFT JOIN users u ON u.id=s.performed_by WHERE s.id=?", (session_id,)).fetchone()
    if not sess:
        flash("Audit session not found.", "error")
        return redirect(url_for("audit_history_list"))
    items = db.execute("SELECT * FROM inventory_list WHERE active=true ORDER BY category, name").fetchall()
    existing_lines = {r["item_id"]: dict(r) for r in db.execute(
        "SELECT * FROM audit_session_lines WHERE session_id=?", (session_id,)).fetchall()}
    # Effective (carried-forward) values from the last CONFIRMED audit, for placeholder display
    confirmed_rows = logic.confirmed_audit_rows_by_item(db)
    latest_confirmed = {}
    for r in confirmed_rows:
        latest_confirmed[r["item_id"]] = r
    readonly = sess["status"] == "Confirmed"
    return render_template("audit_session_view.html", sess=sess, items=items, existing_lines=existing_lines,
                            latest_confirmed=latest_confirmed, readonly=readonly)


def _save_audit_lines(db, session_id):
    """Persists whatever count values are in the submitted form into
    audit_session_lines. Shared by Save and Confirm so that clicking Confirm
    directly (without Save first) can never silently discard the numbers
    someone just typed in."""
    items = db.execute("SELECT id FROM inventory_list WHERE active=true").fetchall()
    for it in items:
        iid = it["id"]
        stock = request.form.get(f"stock_{iid}", "").strip()
        if stock == "":
            continue
        received = request.form.get(f"received_{iid}", "").strip() or "0"
        threshold = request.form.get(f"threshold_{iid}", "").strip()
        critical = request.form.get(f"critical_{iid}", "").strip()
        target = request.form.get(f"target_{iid}", "").strip()
        expiry = request.form.get(f"expiry_{iid}", "").strip()
        notes = request.form.get(f"notes_{iid}", "").strip()

        try:
            vals = (
                float(stock), float(received),
                float(threshold) if threshold else None,
                (1 if critical == "Y" else (0 if critical == "N" else None)),
                float(target) if target else None,
                expiry or None, notes or None,
            )
        except ValueError:
            raise BadNumber(iid)
        # UPSERT rather than a SELECT-then-branch INSERT/UPDATE — closes
        # the race where two concurrent saves for the same item could
        # both read no existing row and both attempt an INSERT, the
        # second raising an unhandled UniqueViolation against the
        # (session_id, item_id) UNIQUE constraint.
        db.execute(
            "INSERT INTO audit_session_lines (session_id,item_id,stock_counted,received_since_prior,"
            "reorder_threshold,critical_item,target_coverage_days,nearest_expiry_date,notes) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (session_id, item_id) DO UPDATE SET stock_counted=excluded.stock_counted, "
            "received_since_prior=excluded.received_since_prior, reorder_threshold=excluded.reorder_threshold, "
            "critical_item=excluded.critical_item, target_coverage_days=excluded.target_coverage_days, "
            "nearest_expiry_date=excluded.nearest_expiry_date, notes=excluded.notes",
            (session_id, iid, *vals),
        )


@app.route("/audit-history/session/<int:session_id>/save", methods=["POST"])
def audit_session_save(session_id):
    db = get_db()
    sess = db.execute("SELECT * FROM audit_sessions WHERE id=?", (session_id,)).fetchone()
    if not sess or sess["status"] != "Draft":
        flash("This audit is confirmed and can no longer be edited.", "error")
        return redirect(url_for("audit_history_list"))

    try:
        _save_audit_lines(db, session_id)
    except BadNumber:
        flash("Audit counts must be valid numbers. The draft was not saved — please correct the highlighted value(s).", "error")
        return redirect(url_for("audit_session_view", session_id=session_id))
    auth.log_change(db, "audit_sessions", str(session_id), "update")
    db.commit()
    flash("Audit saved. You can come back and finish it later, or confirm it once it's complete.", "success")
    return redirect(url_for("audit_session_view", session_id=session_id))


@app.route("/audit-history/session/<int:session_id>/confirm", methods=["POST"])
def audit_session_confirm(session_id):
    db = get_db()
    sess = db.execute("SELECT * FROM audit_sessions WHERE id=?", (session_id,)).fetchone()
    if not sess or sess["status"] != "Draft":
        flash("This audit is already confirmed.", "error")
        return redirect(url_for("audit_history_list"))
    try:
        _save_audit_lines(db, session_id)
    except BadNumber:
        flash("Audit counts must be valid numbers. Nothing was confirmed — please correct the highlighted value(s).", "error")
        return redirect(url_for("audit_session_view", session_id=session_id))
    db.execute("UPDATE audit_sessions SET status='Confirmed', confirmed_at=? WHERE id=?",
              (datetime.now().isoformat(timespec="seconds"), session_id))
    auth.log_change(db, "audit_sessions", str(session_id), "update", {"status": ("Draft", "Confirmed")})
    db.commit()
    flash("Audit confirmed and locked. Inventory Status and Ordering Sheet now reflect these counts.", "success")
    return redirect(url_for("audit_session_view", session_id=session_id))


# ---------------------------------------------------------------------------
# Boarding
# ---------------------------------------------------------------------------
@app.route("/boarding")
def boarding_page():
    db = get_db()
    show_all = request.args.get("all") == "1"
    page = get_page()
    count_where = "" if show_all else " WHERE dismissed=false"
    total = db.execute(f"SELECT COUNT(*) c FROM boarding_sessions{count_where}").fetchone()["c"]
    q = ("SELECT b.*, p.animal_name, p.species, o.id AS owner_id, o.name AS owner_name, o.phone AS owner_phone "
         "FROM boarding_sessions b JOIN patients p ON p.id=b.patient_id JOIN owners o ON o.id=p.owner_id")
    if not show_all:
        q += " WHERE b.dismissed=false"
    q += " ORDER BY b.entry_date DESC LIMIT ? OFFSET ?"
    rows = [dict(r) for r in db.execute(q, (PER_PAGE, page_offset(page))).fetchall()]
    # Batched across the whole page instead of a paid-sum + incident-count
    # query per row (boarding_billing_summary() alone was also redundantly
    # re-fetching the boarding_sessions row this page already has) — see
    # logic.boarding_billing_summary_from_fields().
    ids = [r["id"] for r in rows]
    paid_by_id = {}
    incidents_by_id = {}
    if ids:
        placeholders = ",".join("?" * len(ids))
        paid_by_id = {p["boarding_id"]: p["s"] for p in db.execute(
            f"SELECT boarding_id, COALESCE(SUM(amount),0) s FROM payments "
            f"WHERE boarding_id IN ({placeholders}) GROUP BY boarding_id", ids
        ).fetchall()}
        incidents_by_id = {i["boarding_id"]: i["c"] for i in db.execute(
            f"SELECT boarding_id, COUNT(*) c FROM boarding_incidents "
            f"WHERE boarding_id IN ({placeholders}) GROUP BY boarding_id", ids
        ).fetchall()}
    for r in rows:
        r["billing"] = logic.boarding_billing_summary_from_fields(r, paid_by_id.get(r["id"], 0))
        r["incident_count"] = incidents_by_id.get(r["id"], 0)
    return render_template("boarding.html", sessions=rows, show_all=show_all, today=date.today().isoformat(),
                            page=page, total_pages=page_count(total), total_count=total)


@app.route("/boarding/new", methods=["POST"])
def boarding_new():
    db = get_db()
    f = request.form
    patient_id = f.get("patient_id")
    if not patient_id:
        flash("Pick a patient first.", "error")
        return redirect(url_for("boarding_page"))
    try:
        price_per_day = parse_money(f.get("price_per_day"))
        total = parse_money(f.get("total"))
    except BadNumber:
        flash("Price per Day and Total must be valid numbers.", "error")
        return redirect(url_for("boarding_page"))
    if has_negative(price_per_day, total):
        flash("Price per Day and Total can't be negative.", "error")
        return redirect(url_for("boarding_page"))
    try:
        entry_date = clean_date(f.get("entry_date"), field="entry_date") or date.today().isoformat()
        dismissal_date = clean_date(f.get("dismissal_date"), field="dismissal_date")
    except BadDate as e:
        flash(str(e), "error")
        return redirect(url_for("boarding_page"))
    total_is_auto = total is None
    if total_is_auto:
        total = logic.boarding_suggested_total(price_per_day, entry_date, dismissal_date)
    special_needs = f.get("special_needs") == "on"
    cur = db.execute(
        "INSERT INTO boarding_sessions (patient_id, entry_date, dismissal_date, admitted_items, special_needs, "
        "special_needs_notes, room, price_per_day, total, total_is_auto, dismissed, created_by) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,false,?) RETURNING id",
        (patient_id, entry_date, dismissal_date, f.get("admitted_items"), special_needs,
         f.get("special_needs_notes") if special_needs else None, f.get("room"), price_per_day, total,
         total_is_auto, session.get("user_id")),
    )
    boarding_id = cur.fetchone()["id"]
    logic.refresh_boarding_total(db, boarding_id)
    logic.recompute_month_summary(db, logic.month_key(entry_date))
    auth.log_change(db, "boarding_sessions", str(boarding_id), "create")
    db.commit()
    flash("Boarding session added.", "success")
    return redirect(url_for("boarding_page"))


@app.route("/boarding/<int:boarding_id>/edit", methods=["POST"])
def boarding_edit(boarding_id):
    db = get_db()
    f = request.form
    old = db.execute("SELECT * FROM boarding_sessions WHERE id=?", (boarding_id,)).fetchone()
    if not old:
        flash("Boarding session not found.", "error")
        return redirect(url_for("boarding_page"))
    conflict = stale_edit_error(old["updated_at"], f.get("expected_updated_at"), "boarding session")
    if conflict:
        flash(conflict, "error")
        return redirect(url_for("boarding_page"))
    try:
        price_per_day = parse_money(f.get("price_per_day"))
        total = parse_money(f.get("total"))
    except BadNumber:
        flash("Price per Day and Total must be valid numbers.", "error")
        return redirect(url_for("boarding_page"))
    if has_negative(price_per_day, total):
        flash("Price per Day and Total can't be negative.", "error")
        return redirect(url_for("boarding_page"))
    try:
        entry_date = clean_date(f.get("entry_date"), field="entry_date") or old["entry_date"]
        dismissal_date = clean_date(f.get("dismissal_date"), field="dismissal_date")
    except BadDate as e:
        flash(str(e), "error")
        return redirect(url_for("boarding_page"))
    total_is_auto = total is None
    if total_is_auto:
        total = logic.boarding_suggested_total(price_per_day, entry_date, dismissal_date)
    # Once picked up, boarding_dismiss() locked in the final billed figure —
    # dates/price/total from this form are ignored from that point on, same
    # as a settled invoice. Other fields (room, admitted items, special
    # needs) stay editable for ordinary record corrections.
    if old["dismissed"]:
        entry_date, dismissal_date = old["entry_date"], old["dismissal_date"]
        price_per_day, total, total_is_auto = old["price_per_day"], old["total"], bool(old["total_is_auto"])
    special_needs = f.get("special_needs") == "on"
    new_vals = {
        "entry_date": entry_date, "dismissal_date": dismissal_date, "admitted_items": f.get("admitted_items"),
        "special_needs": special_needs, "special_needs_notes": f.get("special_needs_notes") if special_needs else None,
        "room": f.get("room"), "price_per_day": price_per_day, "total": total, "total_is_auto": total_is_auto,
    }
    changes = auth.diff_dict(old, new_vals)
    db.execute(
        "UPDATE boarding_sessions SET entry_date=?, dismissal_date=?, admitted_items=?, special_needs=?, "
        "special_needs_notes=?, room=?, price_per_day=?, total=?, total_is_auto=?, updated_at=? WHERE id=?",
        (*new_vals.values(), datetime.now().isoformat(timespec="seconds"), boarding_id),
    )
    logic.refresh_boarding_total(db, boarding_id)
    old_month = logic.month_key(old["entry_date"])
    new_month = logic.month_key(entry_date)
    logic.recompute_months_summary(db, [old_month, new_month])
    auth.log_change(db, "boarding_sessions", str(boarding_id), "update", changes)
    db.commit()
    flash("Boarding session updated.", "success")
    if old["dismissed"]:
        flash("This stay is already picked up, so dates/price/total stayed locked at the billed figure — "
              "only room, admitted items, and special needs were changed.", "error")
    return redirect(url_for("boarding_page"))


@app.route("/boarding/<int:boarding_id>/dismiss", methods=["POST"])
def boarding_dismiss(boarding_id):
    db = get_db()
    row = db.execute(
        "SELECT price_per_day, entry_date, dismissal_date, total, total_is_auto FROM boarding_sessions WHERE id=?",
        (boarding_id,),
    ).fetchone()
    if not row:
        flash("Boarding session not found.", "error")
        return redirect(url_for("boarding_page"))
    dismissal_date = row["dismissal_date"] or date.today().isoformat()
    final_total = row["total"]
    if row["total_is_auto"] and row["price_per_day"]:
        # Lock in the final night count now that the stay is actually
        # over — while active, boarding_billing_summary() was recomputing
        # this live; once dismissed, nothing recomputes it anymore, so
        # `total` needs to hold the real final figure, not whatever
        # (usually 1 night) it was left at when the session was created.
        final_total = logic.boarding_suggested_total(row["price_per_day"], row["entry_date"], dismissal_date)
    db.execute("UPDATE boarding_sessions SET dismissed=true, dismissal_date=?, total=? WHERE id=?",
               (dismissal_date, final_total, boarding_id))
    logic.refresh_boarding_total(db, boarding_id)
    # Boarding revenue is attributed to entry_date's month, and that
    # month's P&L was already cached back when this session was created —
    # using whatever `total` was at that moment (usually a 1-night
    # placeholder, per the comment above). Locking in the real final total
    # here without this would leave that month's cached revenue
    # permanently understated.
    logic.recompute_month_summary(db, logic.month_key(row["entry_date"]))
    auth.log_change(db, "boarding_sessions", str(boarding_id), "update", {"dismissed": (False, True)})
    db.commit()
    flash("Marked as picked up.", "success")
    return redirect(url_for("boarding_page"))


@app.route("/boarding/<int:boarding_id>/incident", methods=["POST"])
def boarding_incident(boarding_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM boarding_sessions WHERE id=?", (boarding_id,)).fetchone():
        flash("Boarding session not found.", "error")
        return redirect(url_for("boarding_page"))
    f = request.form
    issue = (f.get("issue") or "").strip()
    if not issue:
        flash("Describe what's wrong before submitting.", "error")
        return redirect(url_for("boarding_page"))
    contacted = "Y" if f.get("contacted") == "on" else "N"
    cur = db.execute(
        "INSERT INTO boarding_incidents (boarding_id, timestamp, issue, contacted, contact_method, response, user_id) "
        "VALUES (?,?,?,?,?,?,?) RETURNING id",
        (boarding_id, datetime.now().isoformat(timespec="seconds"), issue, contacted,
         f.get("contact_method") if contacted == "Y" else None, f.get("response"), session.get("user_id")),
    )
    incident_id = cur.fetchone()["id"]
    auth.log_change(db, "boarding_incidents", str(incident_id), "create")
    db.commit()
    flash("Incident logged.", "success")
    return redirect(url_for("boarding_page"))


@app.route("/boarding/<int:boarding_id>/payment", methods=["POST"])
def boarding_payment(boarding_id):
    db = get_db()
    # Locked before computing the balance, same reasoning as
    # distributor_payment_new()/consignment_settlement_new() — there's no
    # delete/edit route for a payment once recorded, so an overpayment here
    # can never be undone, only journaled around.
    session_row = db.execute("SELECT id FROM boarding_sessions WHERE id=? FOR UPDATE", (boarding_id,)).fetchone()
    if not session_row:
        flash("Boarding session not found.", "error")
        return redirect(url_for("boarding_page"))
    try:
        amount = parse_money(request.form.get("amount")) or 0
    except BadNumber:
        flash("Payment amount must be a valid number.", "error")
        return redirect(url_for("boarding_page"))
    if amount <= 0:
        flash("Payment amount must be greater than 0.", "error")
        return redirect(url_for("boarding_page"))
    balance = logic.boarding_billing_summary(db, boarding_id)["balance"]
    if amount > balance:
        flash(f"That's more than the remaining balance of {logic.fmt_money(balance)} JOD on this stay.", "error")
        return redirect(url_for("boarding_page"))
    cur = db.execute(
        "INSERT INTO payments (boarding_id, amount, method, date, user_id, notes) VALUES (?,?,?,?,?,?) RETURNING id",
        (boarding_id, amount, request.form.get("method"), date.today().isoformat(),
         session.get("user_id"), request.form.get("notes")),
    )
    payment_id = cur.fetchone()["id"]
    auth.log_change(db, "payments", str(payment_id), "create")
    db.commit()
    flash("Payment recorded.", "success")
    return redirect(url_for("boarding_page"))


@app.route("/boarding/<int:boarding_id>/export")
def boarding_export_pdf(boarding_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM boarding_sessions WHERE id=?", (boarding_id,)).fetchone():
        abort(404)
    buf = pdf_export.export_boarding_pdf(db, boarding_id)
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=f"boarding_{boarding_id}.pdf")


# ---------------------------------------------------------------------------
# Point of Sale (Retail only)
# ---------------------------------------------------------------------------
@app.route("/pos")
def pos_page():
    db = get_db()
    cap = auth.discount_cap_for()
    return render_template("pos.html", discount_cap=cap)


@app.route("/pos/checkout", methods=["POST"])
def pos_checkout():
    db = get_db()
    f = request.form
    item_ids = request.form.getlist("item_id")
    quantities = request.form.getlist("quantity")
    try:
        discount_percent = parse_money(f.get("discount_percent")) or 0
    except BadNumber:
        flash("Discount must be a valid number.", "error")
        return redirect(url_for("pos_page"))
    cap = auth.discount_cap_for()
    if discount_percent > cap or discount_percent < 0:
        flash(f"Discount must be between 0% and {cap}% for your role.", "error")
        return redirect(url_for("pos_page"))
    if not item_ids:
        flash("Cart is empty.", "error")
        return redirect(url_for("pos_page"))
    if discount_percent > 0:
        blocked = logic.non_discountable_line_names_for_items(db, item_ids)
        if blocked:
            flash(f"Can't apply a discount — the cart includes item(s) marked as not discountable: {', '.join(blocked)}.", "error")
            return redirect(url_for("pos_page"))

    # Merge quantities for any item that appears in more than one cart line
    # before checking stock. The normal UI cart already merges duplicates
    # client-side, but nothing on the server enforced that — checking each
    # submitted line against the *live* current_stock independently meant
    # two lines of the same item (e.g. 3 + 3 against a stock of 5) could
    # each individually pass the check and together oversell the item.
    qty_by_item = {}
    for iid, qty in zip(item_ids, quantities):
        try:
            qty = parse_money(qty, required=True)
        except BadNumber:
            flash("Cart quantities must be valid numbers.", "error")
            return redirect(url_for("pos_page"))
        if qty <= 0:
            continue
        qty_by_item[iid] = qty_by_item.get(iid, 0) + qty

    subtotal, lines = 0, []
    # Lock every cart item's inventory_list row up front, in a fixed order
    # (sorted by id — never "the order items happen to be in this cart"),
    # before computing or checking stock for any of them. This is what
    # actually closes the oversell race: previously two concurrent
    # checkouts for the same item could both read "5 in stock" before
    # either had written its sale, and both would pass the check. Now the
    # second checkout's SELECT ... FOR UPDATE blocks until the first
    # checkout's transaction commits (or rolls back) and releases the
    # lock, and Postgres gives that blocked SELECT a fresh read once it
    # proceeds — so the stock check below always reflects any sale that
    # just committed for the same item, not a stale snapshot from before
    # this request started waiting. Locking every cart item in the same
    # fixed order (regardless of the order either cart added them) is
    # what prevents two carts sharing two items from deadlocking on each
    # other (cart A locks item1 then waits on item2, while cart B locks
    # item2 then waits on item1).
    for iid in sorted(qty_by_item.keys()):
        db.execute("SELECT id FROM inventory_list WHERE id=? FOR UPDATE", (iid,))

    # Cost basis snapshotted alongside price at checkout time, so COGS
    # reporting reflects what this item actually cost when it was sold —
    # not whatever inventory_list.cost_price says whenever the report is
    # later run (see sale_items.unit_cost's own column comment). Read
    # once, right after locking, alongside the row lock above — this is
    # the cost that will actually be recorded against this sale.
    cost_by_item = {r["id"]: r["cost_price"] for r in db.execute(
        "SELECT id, cost_price FROM inventory_list WHERE id IN (" + ",".join("?" * len(qty_by_item)) + ")",
        list(qty_by_item.keys()),
    ).fetchall()} if qty_by_item else {}

    for iid, qty in qty_by_item.items():
        price = logic.item_sale_price(db, iid)
        if price is None:
            flash(f"Item {iid} has no sale price set in the Price List — skipped.", "error")
            continue
        status = logic.inventory_status_by_id(db, iid)
        # current_stock is None until this item has been through at least
        # one confirmed inventory audit — treated as zero available stock
        # here (fail closed) rather than skipping the check, since
        # skipping it let a never-audited item be oversold via POS with
        # no limit at all, silently and deterministically (not just under
        # a race). A clinic sells a brand-new item for the first time by
        # running a quick audit on it first, same as any other item.
        if status and status["current_stock"] is None:
            flash(f"{status['name']} hasn't been through an inventory audit yet — run an audit before selling it.", "error")
            return redirect(url_for("pos_page"))
        if status and qty > status["current_stock"]:
            flash(f"Only {status['current_stock']} {status['unit'] or ''} of {status['name']} in stock — sale blocked.", "error")
            return redirect(url_for("pos_page"))
        line_total = price * qty
        subtotal += line_total
        lines.append((iid, qty, price, line_total, cost_by_item.get(iid)))

    if not lines:
        flash("Nothing to sell.", "error")
        return redirect(url_for("pos_page"))

    total = round(subtotal * (1 - discount_percent / Decimal(100)), 3)
    payment_method = f.get("payment_method")
    cash_received = change_given = None
    if payment_method == "Cash":
        try:
            cash_received = parse_money(f.get("cash_received"))
        except BadNumber:
            flash("Cash Received must be a valid number.", "error")
            return redirect(url_for("pos_page"))
        if cash_received is not None:
            change_given = max(round(cash_received - total, 3), 0)
    now = datetime.now().isoformat(timespec="seconds")
    cur = db.execute(
        "INSERT INTO sales (sale_date, cashier_id, subtotal, discount_percent, discount_applied_by, total, "
        "payment_method, cash_received, change_given) VALUES (?,?,?,?,?,?,?,?,?) RETURNING id",
        (now, session["user_id"], round(subtotal, 3), discount_percent,
         session["user_id"] if discount_percent else None, total, payment_method, cash_received, change_given),
    )
    sale_id = cur.fetchone()["id"]
    for iid, qty, price, line_total, unit_cost in lines:
        db.execute("INSERT INTO sale_items (sale_id, item_id, quantity, unit_price, line_total, unit_cost) VALUES (?,?,?,?,?,?)",
                  (sale_id, iid, qty, price, round(line_total, 3), unit_cost))
        db.execute("INSERT INTO inventory_transactions (item_id, change_qty, reason, ref_id, timestamp, user_id) "
                  "VALUES (?,?,?,?,?,?)", (iid, -qty, "sale", str(sale_id), now, session["user_id"]))
    logic.recompute_month_summary(db, now[:7])
    auth.log_change(db, "sales", str(sale_id), "create")
    db.commit()
    flash(f"Sale #{sale_id} completed — total {logic.fmt_money(total)} JOD.", "success")
    return redirect(url_for("pos_receipt", sale_id=sale_id))


@app.route("/pos/receipt/<int:sale_id>")
def pos_receipt(sale_id):
    db = get_db()
    sale = db.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
    if sale is None:
        flash("Sale not found.", "error")
        return redirect(url_for("pos_history"))
    items = db.execute(
        "SELECT si.*, i.name FROM sale_items si JOIN inventory_list i ON i.id=si.item_id WHERE si.sale_id=?", (sale_id,)
    ).fetchall()
    return render_template("pos_receipt.html", sale=sale, items=items)


@app.route("/pos/history")
def pos_history():
    db = get_db()
    page = get_page()
    date_filter = request.args.get("date", "").strip() or None
    where = " WHERE s.sale_date LIKE ?" if date_filter else ""
    params = [date_filter + "%"] if date_filter else []
    total = db.execute(f"SELECT COUNT(*) c FROM sales s{where}", params).fetchone()["c"]
    sales = db.execute(
        f"SELECT s.*, u.full_name as cashier_name FROM sales s LEFT JOIN users u ON u.id=s.cashier_id{where} "
        "ORDER BY s.sale_date DESC LIMIT ? OFFSET ?", params + [PER_PAGE, page_offset(page)]
    ).fetchall()
    return render_template("pos_history.html", sales=sales, date_filter=date_filter,
                            page=page, total_pages=page_count(total), total_count=total)


# ---------------------------------------------------------------------------
# Inpatient system
# ---------------------------------------------------------------------------
@app.route("/inpatient")
def inpatient_list():
    db = get_db()
    show_all = request.args.get("all") == "1"
    # Discharged cases drop off the two views above by design (dismissed=false),
    # so a charge added *after* discharge (a forgotten procedure billed
    # late) has no natural collection point — nothing ever resurfaces that
    # case for staff to notice the balance and follow up. This view exists
    # specifically to close that gap: any discharged case still owing
    # money, regardless of why.
    balance_due = request.args.get("view") == "balance_due"
    page = get_page()
    if balance_due:
        paid_join = ("LEFT JOIN (SELECT inpatient_case_id, SUM(amount) AS paid FROM payments "
                     "GROUP BY inpatient_case_id) pay ON pay.inpatient_case_id = c.id")
        where = " WHERE c.dismissed=true AND c.total > COALESCE(pay.paid, 0)"
        total = db.execute(f"SELECT COUNT(*) c FROM inpatient_cases c {paid_join}{where}").fetchone()["c"]
        q = (f"SELECT c.*, p.animal_name, o.name as owner_name, COALESCE(pay.paid, 0) AS paid "
             f"FROM inpatient_cases c JOIN patients p ON p.id=c.patient_id JOIN owners o ON o.id=p.owner_id "
             f"{paid_join}{where} ORDER BY c.admission_date DESC LIMIT ? OFFSET ?")
        cases = db.execute(q, (PER_PAGE, page_offset(page))).fetchall()
    else:
        count_where = "" if show_all else " WHERE dismissed=false"
        total = db.execute(f"SELECT COUNT(*) c FROM inpatient_cases{count_where}").fetchone()["c"]
        q = ("SELECT c.*, p.animal_name, o.name as owner_name FROM inpatient_cases c "
             "JOIN patients p ON p.id=c.patient_id JOIN owners o ON o.id=p.owner_id")
        if not show_all:
            q += " WHERE c.dismissed=false"
        q += " ORDER BY c.admission_date DESC LIMIT ? OFFSET ?"
        cases = db.execute(q, (PER_PAGE, page_offset(page))).fetchall()
    return render_template("inpatient_list.html", cases=cases, show_all=show_all, balance_due=balance_due,
                            page=page, total_pages=page_count(total), total_count=total)


@app.route("/inpatient/new", methods=["GET", "POST"])
def inpatient_new():
    db = get_db()
    if request.method == "POST":
        f = request.form
        try:
            new_weight_kg = parse_money(f.get("weight_kg"))
            new_bcs = parse_int(f.get("bcs"))
            new_admission_date = clean_date(f.get("admission_date"), field="admission_date")
        except BadNumber:
            flash("Weight and BCS must be valid numbers.", "error")
            return redirect(url_for("inpatient_new"))
        except BadDate as e:
            flash(str(e), "error")
            return redirect(url_for("inpatient_new"))
        case_id = _create_inpatient_case(db, f["patient_id"], None, f.get("complaint"), new_admission_date,
                                          new_weight_kg, new_bcs)
        db.execute(
            "UPDATE inpatient_cases SET exam_findings=?, admitted_items=?, attending_vet_id=?, supervising_vet_id=? WHERE id=?",
            (f.get("exam_findings"), f.get("admitted_items"), f.get("attending_vet_id") or None,
             f.get("supervising_vet_id") or None, case_id),
        )
        db.commit()
        flash("Patient admitted.", "success")
        return redirect(url_for("inpatient_detail", case_id=case_id))
    return render_template("inpatient_new.html", vets=vet_users(db))


@app.route("/inpatient/<int:case_id>")
def inpatient_detail(case_id):
    db = get_db()
    case = db.execute(
        "SELECT c.*, p.animal_name, p.species, p.sex, p.age_note, o.name as owner_name, o.phone as owner_phone, "
        "p.id as patient_id FROM inpatient_cases c JOIN patients p ON p.id=c.patient_id "
        "JOIN owners o ON o.id=p.owner_id WHERE c.id=?", (case_id,)
    ).fetchone()
    if not case:
        flash("Inpatient case not found.", "error")
        return redirect(url_for("inpatient_list"))

    updates = db.execute("SELECT u.*, us.full_name FROM inpatient_updates u LEFT JOIN users us ON us.id=u.user_id "
                         "WHERE case_id=? ORDER BY timestamp DESC", (case_id,)).fetchall()
    contacts = db.execute("SELECT c.*, us.full_name FROM inpatient_contact_log c LEFT JOIN users us ON us.id=c.staff_user_id "
                          "WHERE case_id=? ORDER BY timestamp DESC", (case_id,)).fetchall()
    billing = logic.inpatient_billing_summary(db, case_id)
    payments = db.execute("SELECT * FROM payments WHERE inpatient_case_id=? ORDER BY date DESC", (case_id,)).fetchall()
    proc_items = db.execute("SELECT * FROM price_list WHERE category='Service' AND active=true ORDER BY id").fetchall()
    files = attach_mod.list_attachments(db, "inpatient", case_id)
    cap = auth.discount_cap_for()

    return render_template("inpatient_detail.html", case=case, updates=updates, recent_updates=updates[:3],
                            contacts=contacts, recent_contacts=contacts[:3], billing=billing, payments=payments,
                            proc_items=proc_items, vets=vet_users(db), files=files, discount_cap=cap)


@app.route("/inpatient/<int:case_id>/edit", methods=["POST"])
def inpatient_edit(case_id):
    db = get_db()
    f = request.form
    old = db.execute("SELECT * FROM inpatient_cases WHERE id=?", (case_id,)).fetchone()
    conflict = stale_edit_error(old["updated_at"] if old else None, f.get("expected_updated_at"), "inpatient case")
    if conflict:
        flash(conflict, "error")
        return redirect(url_for("inpatient_detail", case_id=case_id))
    dismissed = f.get("dismissed") == "on"
    try:
        edited_dismissal_date = clean_date(f.get("dismissal_date"), field="dismissal_date") if dismissed else None
        edited_weight_kg = parse_money(f.get("weight_kg"))
        edited_bcs = parse_int(f.get("bcs"))
    except (BadDate, BadNumber) as e:
        flash(str(e) if isinstance(e, BadDate) else "Weight and BCS must be valid numbers.", "error")
        return redirect(url_for("inpatient_detail", case_id=case_id))
    if has_negative(edited_weight_kg):
        flash("Weight can't be negative.", "error")
        return redirect(url_for("inpatient_detail", case_id=case_id))
    new_vals = {
        "complaint": f.get("complaint"), "exam_findings": f.get("exam_findings"),
        "weight_kg": edited_weight_kg, "bcs": edited_bcs,
        "admitted_items": f.get("admitted_items"), "dismissed": dismissed,
        "dismissal_date": edited_dismissal_date,
        "attending_vet_id": f.get("attending_vet_id") or None, "supervising_vet_id": f.get("supervising_vet_id") or None,
    }
    changes = auth.diff_dict(old, new_vals)
    db.execute(
        "UPDATE inpatient_cases SET complaint=?, exam_findings=?, weight_kg=?, bcs=?, admitted_items=?, dismissed=?, dismissal_date=?, "
        "attending_vet_id=?, supervising_vet_id=?, updated_at=? WHERE id=?",
        (*new_vals.values(), datetime.now().isoformat(timespec="seconds"), case_id),
    )
    auth.log_change(db, "inpatient_cases", str(case_id), "update", changes)
    db.commit()
    flash("Case updated.", "success")
    return redirect(url_for("inpatient_detail", case_id=case_id))


@app.route("/inpatient/<int:case_id>/update", methods=["POST"])
def inpatient_update_add(case_id):
    db = get_db()
    note = request.form.get("note", "").strip()
    if note:
        db.execute("INSERT INTO inpatient_updates (case_id, timestamp, note, user_id) VALUES (?,?,?,?)",
                  (case_id, datetime.now().isoformat(timespec="seconds"), note, session["user_id"]))
        auth.log_change(db, "inpatient_updates", str(case_id), "create")
        db.commit()
        flash("Update logged.", "success")
    return redirect(url_for("inpatient_detail", case_id=case_id))


@app.route("/inpatient/<int:case_id>/update/<int:update_id>/edit", methods=["POST"])
def inpatient_update_edit(case_id, update_id):
    db = get_db()
    note = request.form.get("note", "").strip()
    old = db.execute("SELECT note FROM inpatient_updates WHERE id=? AND case_id=?", (update_id, case_id)).fetchone()
    if old and note:
        db.execute("UPDATE inpatient_updates SET note=? WHERE id=?", (note, update_id))
        auth.log_change(db, "inpatient_updates", str(update_id), "update", {"note": (old["note"], note)})
        db.commit()
        flash("Update edited.", "success")
    return redirect(url_for("inpatient_detail", case_id=case_id))


@app.route("/inpatient/<int:case_id>/contact", methods=["POST"])
def inpatient_contact_add(case_id):
    db = get_db()
    f = request.form
    picked_up = 1 if f.get("picked_up") == "yes" else 0
    db.execute("INSERT INTO inpatient_contact_log (case_id, timestamp, picked_up, staff_user_id, notes) VALUES (?,?,?,?,?)",
              (case_id, datetime.now().isoformat(timespec="seconds"), picked_up, session["user_id"], f.get("notes")))
    auth.log_change(db, "inpatient_contact_log", str(case_id), "create")
    db.commit()
    flash("Contact attempt logged.", "success")
    return redirect(url_for("inpatient_detail", case_id=case_id))


@app.route("/inpatient/<int:case_id>/billing", methods=["POST"])
def inpatient_billing_add(case_id):
    db = get_db()
    price_ids = request.form.getlist("price_id")
    now = datetime.now().isoformat(timespec="seconds")
    added = 0
    had_bad_number = False
    had_bad_price = False
    for pid in price_ids:
        raw_qty = request.form.get(f"qty_{pid}", "").strip()
        try:
            qty = parse_money(raw_qty)
        except BadNumber:
            had_bad_number = True
            continue
        if not qty or qty <= 0:
            continue
        # Snapshot the current Price List sale price/cost right now, at
        # the moment this procedure is added to the bill — so a price
        # edit made next month can't reach back and change what this
        # stay's bill (or that month's revenue/COGS report) says today.
        price_row = db.execute("SELECT sale_price, cost_price FROM price_list WHERE id=?", (pid,)).fetchone()
        if not price_row:
            had_bad_price = True
            continue
        db.execute(
            "INSERT INTO inpatient_billing (case_id, price_id, quantity, unit_price, unit_cost, logged_by, timestamp) "
            "VALUES (?,?,?,?,?,?,?)",
            (case_id, pid, qty, price_row["sale_price"], price_row["cost_price"], session["user_id"], now),
        )
        added += 1
    if added:
        logic.refresh_inpatient_total(db, case_id)
        logic.recompute_month_summary(db, now[:7])
        auth.log_change(db, "inpatient_billing", str(case_id), "create")
    db.commit()
    if had_bad_number:
        flash("Some quantities weren't valid numbers and were skipped.", "error")
    if had_bad_price:
        flash("Some selected items no longer exist in the Price List and were skipped.", "error")
    if added:
        flash(f"{added} procedure(s) added to the bill.", "success")
    return redirect(url_for("inpatient_detail", case_id=case_id))


@app.route("/inpatient/<int:case_id>/billing/<int:line_id>/delete", methods=["POST"])
def inpatient_billing_delete(case_id, line_id):
    db = get_db()
    row = db.execute("SELECT timestamp FROM inpatient_billing WHERE id=? AND case_id=?", (line_id, case_id)).fetchone()
    if not row:
        flash("That billing line was already removed.", "error")
        return redirect(url_for("inpatient_detail", case_id=case_id))
    db.execute("DELETE FROM inpatient_billing WHERE id=? AND case_id=?", (line_id, case_id))
    logic.refresh_inpatient_total(db, case_id)
    if row["timestamp"]:
        logic.recompute_month_summary(db, row["timestamp"][:7])
    auth.log_change(db, "inpatient_billing", str(line_id), "delete")
    db.commit()
    flash("Line removed.", "success")
    return redirect(url_for("inpatient_detail", case_id=case_id))


@app.route("/inpatient/<int:case_id>/discount", methods=["POST"])
def inpatient_discount_save(case_id):
    db = get_db()
    try:
        percent = parse_money(request.form.get("discount_percent")) or 0
    except BadNumber:
        flash("Discount must be a valid number.", "error")
        return redirect(url_for("inpatient_detail", case_id=case_id))
    cap = auth.discount_cap_for()
    if percent > cap or percent < 0:
        flash(f"Discount must be between 0% and {cap}% for your role.", "error")
        return redirect(url_for("inpatient_detail", case_id=case_id))
    if percent > 0:
        price_ids = [r["price_id"] for r in db.execute(
            "SELECT DISTINCT price_id FROM inpatient_billing WHERE case_id=?", (case_id,)
        ).fetchall()]
        blocked = logic.non_discountable_line_names(db, price_ids)
        if blocked:
            flash(f"Can't apply a discount — this bill includes item(s) marked as not discountable: {', '.join(blocked)}.", "error")
            return redirect(url_for("inpatient_detail", case_id=case_id))
    old = db.execute("SELECT discount_percent FROM inpatient_cases WHERE id=?", (case_id,)).fetchone()
    db.execute("UPDATE inpatient_cases SET discount_percent=?, discount_applied_by=? WHERE id=?",
              (percent, session["user_id"], case_id))
    logic.refresh_inpatient_total(db, case_id)
    logic.recompute_months_summary(db, logic.months_touched_by_inpatient_case(db, case_id))
    auth.log_change(db, "inpatient_cases", str(case_id), "update", {"discount_percent": (old["discount_percent"], percent)})
    db.commit()
    flash(f"{percent:.0f}% discount applied.", "success")
    return redirect(url_for("inpatient_detail", case_id=case_id))


@app.route("/inpatient/<int:case_id>/payment", methods=["POST"])
def inpatient_payment_add(case_id):
    db = get_db()
    f = request.form
    try:
        amount = parse_money(f.get("amount"), required=True)
    except BadNumber:
        flash("Payment amount must be a valid number.", "error")
        return redirect(url_for("inpatient_detail", case_id=case_id))
    if amount <= 0:
        flash("Payment amount must be greater than 0.", "error")
        return redirect(url_for("inpatient_detail", case_id=case_id))
    try:
        payment_date = clean_date(f.get("date"), field="date") or date.today().isoformat()
    except BadDate as e:
        flash(str(e), "error")
        return redirect(url_for("inpatient_detail", case_id=case_id))
    cur = db.execute(
        "INSERT INTO payments (inpatient_case_id, amount, method, date, user_id, notes) VALUES (?,?,?,?,?,?) RETURNING id",
        (case_id, amount, f.get("method"), payment_date, session["user_id"], f.get("notes")),
    )
    payment_id = cur.fetchone()["id"]
    auth.log_change(db, "payments", str(payment_id), "create")
    db.commit()
    flash("Payment recorded.", "success")
    return redirect(url_for("inpatient_detail", case_id=case_id))


@app.route("/inpatient/<int:case_id>/attachments", methods=["POST"])
def inpatient_attachment_upload(case_id):
    db = get_db()
    case = db.execute("SELECT patient_id FROM inpatient_cases WHERE id=?", (case_id,)).fetchone()
    if not case:
        flash("Inpatient case not found.", "error")
        return redirect(url_for("inpatient_list"))
    file = request.files.get("file")
    if not file or not file.filename:
        flash("No file selected.", "error")
        return redirect(url_for("inpatient_detail", case_id=case_id))
    _, err = attach_mod.save_attachment(db, case["patient_id"], "inpatient", case_id, file, session["user_id"])
    flash(err if err else "File uploaded.", "error" if err else "success")
    return redirect(url_for("inpatient_detail", case_id=case_id))


# ---------------------------------------------------------------------------
# Appointments
# ---------------------------------------------------------------------------
@app.route("/appointments")
def appointments_page():
    db = get_db()
    today_iso = date.today().isoformat()
    week_anchor = request.args.get("week", today_iso)
    try:
        logic.parse_date(week_anchor)
    except ValueError:
        flash("That week link wasn't valid, showing the current week instead.", "error")
        week_anchor = today_iso
    days = logic.week_dates(week_anchor)
    selected_day = request.args.get("day", today_iso)
    try:
        logic.parse_date(selected_day)
    except ValueError:
        flash("That date wasn't valid, showing today instead.", "error")
        selected_day = today_iso
    columns, grid = logic.day_grid(db, selected_day)
    prev_week = (days[0] - timedelta(days=7)).isoformat()
    next_week = (days[0] + timedelta(days=7)).isoformat()
    orphaned = logic.orphaned_appointments(db)
    return render_template("appointments.html", days=days, selected_day=selected_day, columns=columns,
                            grid=grid, week_anchor=week_anchor, prev_week=prev_week, next_week=next_week,
                            today_iso=today_iso, orphaned=orphaned)


@app.route("/appointments/new", methods=["POST"])
def appointment_new():
    db = get_db()
    f = request.form
    try:
        appt_date = clean_date(f.get("appt_date"), field="appt_date")
    except BadDate as e:
        flash(str(e), "error")
        return redirect(url_for("appointments_page"))
    if appt_date is None:
        flash("Appointment date is required.", "error")
        return redirect(url_for("appointments_page"))
    slot_label = f["slot_label"]
    resource_type = f.get("resource_type")
    if resource_type not in RESOURCE_TYPES:
        flash("Resource type must be one of: " + ", ".join(RESOURCE_TYPES) + ".", "error")
        return redirect(url_for("appointments_page", day=appt_date))
    appointment_type = f.get("appointment_type")
    if appointment_type not in APPOINTMENT_TYPES:
        flash("Appointment type must be one of: " + ", ".join(APPOINTMENT_TYPES) + ".", "error")
        return redirect(url_for("appointments_page", day=appt_date))
    resource_id = f.get("resource_id") or None
    if resource_type == "grooming":
        # Grooming has no per-resource distinction — every grooming booking
        # shares one column on the grid, keyed as (slot_label, "grooming",
        # NULL) by day_grid()/slot_conflict(). A tampered request smuggling
        # a non-null resource_id here would create a row neither of those
        # ever looks at — invisible on the grid — so this is the only slot
        # type where the value has to be forced rather than merely validated.
        resource_id = None
    elif not resource_id or not any(v["id"] == resource_id for v in vet_users(db)):
        flash("Pick a valid, active vet for this appointment.", "error")
        return redirect(url_for("appointments_page", day=appt_date))
    if not any(s["label"] == slot_label for s in logic.generate_slots(db)):
        flash("That's not a valid time slot — the schedule may have changed. Reload and try again.", "error")
        return redirect(url_for("appointments_page", day=appt_date))

    if logic.slot_conflict(db, appt_date, slot_label, resource_type, resource_id):
        flash("That slot is already booked for this vet/groomer.", "error")
        return redirect(url_for("appointments_page", day=appt_date))

    # The check above is a friendly fast-path, not the real guarantee — two
    # concurrent bookings for the same slot could both pass it before either
    # inserts. The database's uq_appointments_slot unique index (see
    # schema_postgres.sql) is what actually prevents the double-booking;
    # this catches the resulting IntegrityError for whichever request loses
    # that race and turns it into the same friendly message instead of a
    # raw 500.
    try:
        cur = db.execute(
            "INSERT INTO appointments (appt_date, slot_label, resource_type, resource_id, pet_name, owner_name, "
            "appointment_type, reason, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?) RETURNING id",
            (appt_date, slot_label, resource_type, resource_id, f["pet_name"], f["owner_name"],
             appointment_type, f.get("reason"), session["user_id"], datetime.now().isoformat(timespec="seconds")),
        )
        appt_id = cur.fetchone()["id"]
        auth.log_change(db, "appointments", str(appt_id), "create")
        db.commit()
    except dbmod.IntegrityError:
        db.rollback()
        flash("That slot is already booked for this vet/groomer.", "error")
        return redirect(url_for("appointments_page", day=appt_date))
    flash("Appointment booked.", "success")
    return redirect(url_for("appointments_page", day=appt_date))


@app.route("/appointments/<int:appt_id>/cancel", methods=["POST"])
def appointment_cancel(appt_id):
    db = get_db()
    row = db.execute("SELECT appt_date FROM appointments WHERE id=?", (appt_id,)).fetchone()
    if not row:
        flash("Appointment not found.", "error")
        return redirect(url_for("appointments_page"))
    db.execute("DELETE FROM appointments WHERE id=?", (appt_id,))
    auth.log_change(db, "appointments", str(appt_id), "delete")
    db.commit()
    flash("Appointment cancelled.", "success")
    return redirect(url_for("appointments_page", day=str(row["appt_date"])))


# ---------------------------------------------------------------------------
# Reports: Monthly & Yearly P&L (Admin only)
# ---------------------------------------------------------------------------
@app.route("/reports")
@auth.permission_required("view_financial_reports")
def reports():
    db = get_db()
    pl = logic.monthly_pl(db)
    opex_rows = db.execute("SELECT month, rent, salaries, utilities, marketing, other FROM monthly_opex").fetchall()
    opex_by_month = {r["month"]: dict(r) for r in opex_rows}
    return render_template("reports.html", pl=pl, opex_by_month=opex_by_month)


@app.route("/reports/yearly")
@auth.permission_required("view_financial_reports")
def reports_yearly():
    db = get_db()
    all_pl = logic.yearly_pl(db)
    page = get_page()
    total = len(all_pl)
    offset = page_offset(page)
    pl = all_pl[offset:offset + PER_PAGE]
    return render_template("reports_yearly.html", pl=pl,
                            page=page, total_pages=page_count(total), total_count=total)


@app.route("/reports/rebuild", methods=["POST"])
@auth.permission_required("view_financial_reports")
def reports_rebuild_summary():
    db = get_db()
    logic.recompute_full_summary(db)
    db.commit()
    flash("Report data rebuilt from current billing, sales, and cost data.", "success")
    return redirect(request.form.get("return_to") or url_for("reports"))


# ---------------------------------------------------------------------------
# Insights (BI dashboard) & Retention (cohort analysis) — Admin only
# ---------------------------------------------------------------------------
@app.route("/insights")
@auth.permission_required("view_insights_retention")
def insights():
    months_back = 12
    cutoff = logic.month_list(months_back)[0] + "-01"

    # These six queries don't depend on each other, and each is a read-only
    # aggregate over a different slice of the schema — running them on
    # separate connections in parallel cuts wall-clock time to roughly the
    # slowest single query instead of the sum of all of them. (Postgres
    # itself handles concurrent read connections fine; each thread here
    # just needs its own psycopg connection since a single connection
    # can't run more than one query at a time.) Borrowed from the pool
    # instead of opening a brand-new raw connection per thread per page
    # view — bounded by the same DB_POOL_MAX_SIZE as ordinary requests.
    def _run(fn):
        con = dbmod.getconn()
        try:
            return fn(con)
        finally:
            con.rollback()  # read-only; explicit rollback before returning to the pool
            dbmod.putconn(con)

    jobs = {
        "revenue": lambda c: logic.revenue_by_category(c, months_back=months_back),
        "vets": lambda c: logic.vet_performance(c, months_back=months_back),
        "clients": lambda c: logic.client_value(c, limit=20),
        "weekday_load": lambda c: logic.appointment_weekday_load(c, months_back=months_back),
        "occupancy": lambda c: logic.inpatient_boarding_occupancy(c, months_back=months_back),
        "payment_mix": lambda c: [dict(r) for r in c.execute(
            "SELECT method, COUNT(*) c, COALESCE(SUM(amount),0) total FROM payments "
            "WHERE date >= ? GROUP BY method ORDER BY total DESC",
            (cutoff,),
        ).fetchall()],
        "cash_register_health": lambda c: logic.cash_register_last_30_days(c),
    }
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futures = {name: ex.submit(_run, fn) for name, fn in jobs.items()}
        results = {name: f.result() for name, f in futures.items()}

    top_clients, avg_spend, active_client_count = results["clients"]
    return render_template(
        "insights.html", revenue=results["revenue"], vets=results["vets"],
        top_clients=top_clients, avg_spend=avg_spend, active_client_count=active_client_count,
        weekday_load=results["weekday_load"], occupancy=results["occupancy"],
        payment_mix=results["payment_mix"], cash_register_health=results["cash_register_health"],
        months_back=months_back,
    )


@app.route("/retention")
@auth.permission_required("view_insights_retention")
def retention():
    db = get_db()
    full = logic.cohort_retention_grid(db, max_offset=11)
    total = len(full["grid"])
    total_pages = page_count(total)
    page = min(get_page(), total_pages)
    offset = page_offset(page)
    page_grid = full["grid"][offset:offset + PER_PAGE]
    cohort = {"cohort_months": full["cohort_months"][offset:offset + PER_PAGE],
              "offsets": full["offsets"], "grid": page_grid}
    return render_template("retention.html", cohort=cohort,
                            page=page, total_pages=total_pages, total_count=total)


@app.route("/refunds")
@auth.permission_required("manage_refunds")
def refunds_page():
    db = get_db()
    page = get_page()
    date_filter = request.args.get("date", "").strip() or None
    count_where = " WHERE refund_date = ?" if date_filter else ""
    count_params = [date_filter] if date_filter else []
    total = db.execute(f"SELECT COUNT(*) c FROM refunds{count_where}", count_params).fetchone()["c"]
    refunds = logic.recent_refunds(db, limit=PER_PAGE, offset=page_offset(page), date_filter=date_filter)
    return render_template("refunds.html", refunds=refunds, today=date.today().isoformat(),
                            date_filter=date_filter,
                            page=page, total_pages=page_count(total), total_count=total)


@app.route("/refunds/retail", methods=["POST"])
@auth.permission_required("manage_refunds")
def refund_retail_save():
    db = get_db()
    f = request.form
    try:
        sale_id = int(f.get("sale_id", ""))
    except (TypeError, ValueError):
        flash("Look up a sale first — a retail refund must be linked to the sale it's refunding.", "error")
        return redirect(url_for("refunds_page"))
    sale_item_ids_raw = f.getlist("sale_item_id")
    quantities = f.getlist("quantity")
    restock = f.get("restock") == "on"
    reason = (f.get("reason") or "").strip()
    try:
        refund_date = clean_date(f.get("refund_date"), field="refund_date") or date.today().isoformat()
    except BadDate as e:
        flash(str(e), "error")
        return redirect(url_for("refunds_page"))

    if not sale_item_ids_raw:
        flash("No items selected — nothing to refund.", "error")
        return redirect(url_for("refunds_page"))
    try:
        sale_item_ids = [int(sid) for sid in sale_item_ids_raw]
    except ValueError:
        flash("Invalid item selection.", "error")
        return redirect(url_for("refunds_page"))

    # Lock every sale_items row being refunded, in a fixed order, before
    # computing how much of each is still refundable — same reasoning as
    # pos_checkout()'s stock-row locking: without this, two concurrent
    # refunds against the same sale could each read "2 remaining" and both
    # submit, over-refunding a sale that only had 2 to give back.
    for sid in sorted(set(sale_item_ids)):
        db.execute("SELECT id FROM sale_items WHERE id=? AND sale_id=? FOR UPDATE", (sid, sale_id))

    sale, refundable = logic.refundable_sale_items(db, sale_id)
    if not sale:
        flash("Sale not found.", "error")
        return redirect(url_for("refunds_page"))
    remaining_by_id = {l["sale_item_id"]: l for l in refundable}

    lines, total = [], 0
    for sid, qty_raw in zip(sale_item_ids, quantities):
        try:
            qty = parse_money(qty_raw, required=True)
        except BadNumber:
            flash("Refund quantities must be valid numbers.", "error")
            return redirect(url_for("refunds_page"))
        if qty <= 0:
            continue
        # Priced from what this sale actually charged per unit
        # (refundable_sale_items()'s discount-adjusted unit_price) — never
        # re-looked-up against today's Price List, which may have changed
        # since the sale.
        line = remaining_by_id.get(sid)
        if not line:
            flash("One of the selected items isn't part of that sale.", "error")
            return redirect(url_for("refunds_page"))
        if qty > line["remaining"]:
            flash(f"Can't refund {qty:g} {line['name']} — only {line['remaining']:g} left refundable from this sale.", "error")
            return redirect(url_for("refunds_page"))
        price = line["unit_price"]
        line_total = round(price * qty, 3)
        total += line_total
        lines.append((line["item_id"], sid, qty, price, line_total))

    if not lines:
        flash("Nothing to refund.", "error")
        return redirect(url_for("refunds_page"))

    now = datetime.now().isoformat(timespec="seconds")
    cur = db.execute(
        "INSERT INTO refunds (refund_type, refund_date, amount, restocked, sale_id, reason, refund_method, processed_by, created_at) "
        "VALUES ('retail',?,?,?,?,?,?,?,?) RETURNING id",
        (refund_date, round(total, 3), restock, sale_id, reason, f.get("refund_method"), session["user_id"], now),
    )
    refund_id = cur.fetchone()["id"]

    for iid, sid, qty, price, line_total in lines:
        db.execute(
            "INSERT INTO refund_items (refund_id, item_id, quantity, unit_price, line_total, sale_item_id) "
            "VALUES (?,?,?,?,?,?)",
            (refund_id, iid, qty, price, line_total, sid),
        )
        if restock:
            db.execute(
                "INSERT INTO inventory_transactions (item_id, change_qty, reason, ref_id, timestamp, user_id) "
                "VALUES (?,?,?,?,?,?)",
                (iid, qty, "refund", str(refund_id), now, session["user_id"]),
            )

    logic.recompute_month_summary(db, logic.month_key(refund_date))
    auth.log_change(db, "refunds", str(refund_id), "create")
    db.commit()
    flash(f"Refund of {total:,.3f} JOD recorded" + (" and stock restored." if restock else "."), "success")
    return redirect(url_for("refunds_page"))


@app.route("/refunds/service", methods=["POST"])
@auth.permission_required("manage_refunds")
def refund_service_save():
    db = get_db()
    f = request.form
    try:
        amount = parse_money(f.get("amount")) or 0
    except BadNumber:
        flash("Refund amount must be a valid number.", "error")
        return redirect(url_for("refunds_page"))
    reason = (f.get("reason") or "").strip()
    try:
        refund_date = clean_date(f.get("refund_date"), field="refund_date") or date.today().isoformat()
    except BadDate as e:
        flash(str(e), "error")
        return redirect(url_for("refunds_page"))
    visit_id = (f.get("visit_id") or "").strip() or None
    case_id_raw = (f.get("inpatient_case_id") or "").strip()

    if amount <= 0:
        flash("Refund amount must be greater than 0.", "error")
        return redirect(url_for("refunds_page"))

    if visit_id and not db.execute("SELECT 1 FROM visits WHERE id=?", (visit_id,)).fetchone():
        flash(f"Visit {visit_id} not found.", "error")
        return redirect(url_for("refunds_page"))

    case_id = None
    if case_id_raw:
        if not case_id_raw.isdigit() or not db.execute(
            "SELECT 1 FROM inpatient_cases WHERE id=?", (int(case_id_raw),)
        ).fetchone():
            flash(f"Inpatient case {case_id_raw} not found.", "error")
            return redirect(url_for("refunds_page"))
        case_id = int(case_id_raw)

    now = datetime.now().isoformat(timespec="seconds")
    cur = db.execute(
        "INSERT INTO refunds (refund_type, refund_date, amount, visit_id, inpatient_case_id, reason, refund_method, processed_by, created_at) "
        "VALUES ('service',?,?,?,?,?,?,?,?) RETURNING id",
        (refund_date, round(amount, 3), visit_id, case_id, reason, f.get("refund_method"), session["user_id"], now),
    )
    refund_id = cur.fetchone()["id"]
    logic.recompute_month_summary(db, logic.month_key(refund_date))
    auth.log_change(db, "refunds", str(refund_id), "create")
    db.commit()
    flash(f"Service refund of {amount:,.3f} JOD recorded.", "success")
    return redirect(url_for("refunds_page"))


@app.route("/reports/opex", methods=["POST"])
@auth.permission_required("view_financial_reports")
def reports_opex_save():
    db = get_db()
    f = request.form
    month = f.get("month", "").strip()
    if not month:
        flash("Pick a month first.", "error")
        return redirect(url_for("reports"))
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        flash("That's not a valid month.", "error")
        return redirect(url_for("reports"))
    try:
        rent = parse_money(f.get("rent")) or 0
        salaries = parse_money(f.get("salaries")) or 0
        utilities = parse_money(f.get("utilities")) or 0
        marketing = parse_money(f.get("marketing")) or 0
        other = parse_money(f.get("other")) or 0
    except BadNumber:
        flash("Operating costs must be valid numbers.", "error")
        return redirect(url_for("reports"))
    db.execute(
        """INSERT INTO monthly_opex (month, rent, salaries, utilities, marketing, other) VALUES (?,?,?,?,?,?)
           ON CONFLICT(month) DO UPDATE SET rent=excluded.rent, salaries=excluded.salaries,
           utilities=excluded.utilities, marketing=excluded.marketing, other=excluded.other""",
        (month, rent, salaries, utilities, marketing, other),
    )
    auth.log_change(db, "monthly_opex", month, "update")
    db.commit()
    flash(f"Operating costs saved for {month}.", "success")
    return redirect(url_for("reports"))


# ---------------------------------------------------------------------------
# Settings (Admin only)
# ---------------------------------------------------------------------------
@app.route("/settings", methods=["GET", "POST"])
@auth.permission_required("manage_settings")
def settings_page():
    db = get_db()
    if request.method == "POST":
        # (field, min, max) — keeps schedule generation and alert windows sane.
        NUMERIC_RANGES = {
            "audit_overdue_days": (1, 3650),
            "expiry_soon_days": (1, 3650),
            "appt_slot_minutes": (5, 240),
            "backup_retention": (1, 3650),
            "discount_cap_admin": (0, 100),
            "discount_cap_vet": (0, 100),
            "discount_cap_reception": (0, 100),
        }
        for key, (lo, hi) in NUMERIC_RANGES.items():
            val = request.form.get(key)
            if val is None or val.strip() == "":
                continue
            try:
                n = int(val)
            except ValueError:
                flash(f"{key.replace('_', ' ').title()} must be a whole number.", "error")
                return redirect(url_for("settings_page"))
            if n < lo or n > hi:
                flash(f"{key.replace('_', ' ').title()} must be between {lo} and {hi}.", "error")
                return redirect(url_for("settings_page"))
        start = request.form.get("appt_start_time")
        end = request.form.get("appt_end_time")
        if start and end and start >= end:
            flash("Day Ends At must be after Day Starts At.", "error")
            return redirect(url_for("settings_page"))
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
                    "backup_dir", "backup_time", "backup_retention"]:
            val = request.form.get(key)
            if val is not None:
                old = logic.get_setting(db, key)
                db.execute(
                    "INSERT INTO settings (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, val),
                )
                if old != val:
                    auth.log_change(db, "settings", key, "update", {key: (old, val)})
        # Discount caps live on roles.discount_cap now (not the settings
        # table) so they participate in the same role_permissions model as
        # everything else — these three fields just edit the built-in
        # Admin/Vet/Reception system roles' caps directly, same as before
        # from the admin's point of view.
        cap_changed = False
        for role_name, field in [("Admin", "discount_cap_admin"), ("Vet", "discount_cap_vet"),
                                  ("Reception", "discount_cap_reception")]:
            val = request.form.get(field)
            if val is None:
                continue
            role = db.execute("SELECT id, discount_cap FROM roles WHERE name=?", (role_name,)).fetchone()
            if not role or int(val) == role["discount_cap"]:
                continue
            db.execute("UPDATE roles SET discount_cap=? WHERE id=?", (int(val), role["id"]))
            auth.log_change(db, "roles", role["id"], "update", {"discount_cap": (role["discount_cap"], int(val))})
            cap_changed = True
        if cap_changed:
            auth.bump_permissions_version(db)
        db.commit()
        if request.form.get("backup_time"):
            import scheduler
            scheduler.reschedule(request.form.get("backup_time"))
        flash("Settings saved.", "success")
        newly_orphaned = len(logic.orphaned_appointments(db)) - orphaned_before
        if newly_orphaned > 0:
            flash(f"Heads up: changing the scheduling hours/slot length just made {newly_orphaned} upcoming "
                  f"appointment(s) stop matching a slot on the grid. They're still booked — check "
                  f"Appointments for the \"need attention\" list to reschedule them.", "error")
        return redirect(url_for("settings_page"))
    rows = db.execute("SELECT * FROM settings").fetchall()
    settings = {r["key"]: r["value"] for r in rows}
    role_caps = {r["name"]: r["discount_cap"] for r in db.execute("SELECT name, discount_cap FROM roles").fetchall()}
    settings["discount_cap_admin"] = str(role_caps.get("Admin", auth.DISCOUNT_CAPS["Admin"]))
    settings["discount_cap_vet"] = str(role_caps.get("Vet", auth.DISCOUNT_CAPS["Vet"]))
    settings["discount_cap_reception"] = str(role_caps.get("Reception", auth.DISCOUNT_CAPS["Reception"]))
    import backup as backup_mod
    return render_template(
        "settings.html", settings=settings, lan_address=lan_address(),
        recent_backups=backup_mod.recent_backups(db),
    )


@app.route("/settings/backup-now", methods=["POST"])
@auth.permission_required("manage_settings")
def settings_backup_now():
    db = get_db()
    import backup as backup_mod
    ok, message = backup_mod.run_backup(db)
    flash(message, "success" if ok else "error")
    return redirect(url_for("settings_page"))


if __name__ == "__main__":
    try:
        probe = dbmod.connect()
        probe.execute("SELECT 1 FROM settings LIMIT 1")
        probe.close()
    except Exception as e:
        raise SystemExit(
            f"Could not reach the Postgres database ({e}).\n"
            "Run: python3 setup.py first."
        )

    import scheduler
    scheduler.start(get_db=dbmod.connect, close_db=lambda c: c.close())

    def _graceful_shutdown(signum, frame):
        # Closes every pooled connection cleanly rather than letting them
        # get dropped mid-socket-close when the process exits.
        dbmod.close_pool()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _graceful_shutdown)
    signal.signal(signal.SIGINT, _graceful_shutdown)
    # Windows sends SIGBREAK (not SIGTERM) for Ctrl-Break / console-close —
    # SIGTERM delivery there is only reliable when running as a proper
    # Windows service, which this app doesn't. SIGINT (Ctrl-C) already
    # works the same on both platforms.
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _graceful_shutdown)

    # Bind address/port configurable instead of hardcoded — default
    # unchanged (0.0.0.0:5050). BEHIND_TLS_PROXY above is how this app
    # supports HTTPS: via a reverse proxy in front, not by binding
    # Waitress directly to a different scheme.
    bind_host = os.environ.get("VETCLINICSYSTEMJO_HOST", "0.0.0.0")
    scheme = "https" if BEHIND_TLS_PROXY else "http"

    if os.environ.get("VETCLINICSYSTEMJO_DEV") == "1":
        # Flask's dev server — convenient for local debugging only; not used
        # for normal clinic operation.
        app.run(debug=True, host=bind_host, port=BIND_PORT)
    else:
        from waitress import serve
        print("VetClinicSystem JO is running — reachable on the clinic network at "
              f"{scheme}://{lan_address()}:{BIND_PORT}")
        serve(app, host=bind_host, port=BIND_PORT, threads=8)
