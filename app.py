import ipaddress
import json
import os
import re
import signal
import sys
import time
import logging
import logging.handlers
import traceback
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
# On the versioned-release layout (VETCLINICSYSTEMJO_DATA_DIR set by the
# launcher script — see updater.py / setup.py --enable-updates), .env
# lives in the data dir, not next to this file, and code here runs from a
# release folder whose working directory at launch isn't guaranteed.
# Falls back to load_dotenv()'s normal upward search from cwd otherwise,
# unchanged from before this option existed.
_data_dir = os.environ.get("VETCLINICSYSTEMJO_DATA_DIR")
if _data_dir:
    load_dotenv(os.path.join(_data_dir, ".env"))
else:
    load_dotenv()

from flask import (
    Flask, render_template, request, redirect, url_for, flash, g, jsonify,
    session, send_from_directory, send_file, abort
)
from flask.json.provider import DefaultJSONProvider
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError
from werkzeug.exceptions import HTTPException

import logic
import auth
import db as dbmod
import barcode as barcode_mod
import attachments as attach_mod
import jobs
import pdf_export

# BASE_DIR, VERSION, DB_REQUEST_TIMEOUT_SECONDS, get_db() and lan_address()
# live in core.py so the route blueprints under routes/ can reach them
# without importing this module, which registers them (see core.py).
from core import BASE_DIR, VERSION, DB_REQUEST_TIMEOUT_SECONDS, get_db, lan_address
from core import csp_nonce
from core import (
    BadDate,
    BadNumber,
    BadPhone,
    CLEANUP_CAP,
    MAX_INT,
    MAX_MONEY,
    MAX_PAGE,
    MAX_QUANTITY,
    PAYMENT_METHODS,
    PER_PAGE,
    PHONE_COUNTRY_CODE,
    PHONE_LOCAL_LENGTH,
    _render_with_progress,
    clean,
    clean_date,
    clean_date_filter,
    cleanup_amount_error,
    date_filter_arg,
    discount_percent_error,
    get_page,
    has_negative,
    normalize_phone,
    page_count,
    page_offset,
    parse_int,
    parse_money,
    parse_quantity,
    required_field,
)
# Read by heartbeat.py for the payload's uptime figure. Set here rather than in
# heartbeat itself because that module is imported lazily inside a scheduler
# job, which would make "uptime" mean "time since the first heartbeat".
APP_STARTED_AT = datetime.now()


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
# The unset check alone doesn't catch someone hand-copying .env.example to
# .env instead of running setup.py (which is what actually replaces this
# placeholder with a real random key) — that would otherwise boot fine
# with a well-known, publicly-visible-in-source-control value signing
# every session cookie and CSRF token.
if not app.secret_key or app.secret_key == "change-me":
    raise SystemExit(
        "SECRET_KEY is not set (or still the placeholder value). Copy "
        ".env.example to .env (setup.py does this for you, with a real "
        "random key) before starting the app."
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
SESSION_LIFETIME_HOURS = float(os.environ.get("SESSION_LIFETIME_HOURS", "12"))
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=SESSION_LIFETIME_HOURS)
# Flask-WTF defaults WTF_CSRF_TIME_LIMIT to 3600 seconds, and this app never
# set it -- so the CSRF token expired after ONE hour inside a session that
# stayed valid for TWELVE. A visit form, an inpatient bill or a POS cart left
# open across a consultation then failed on submit, threw away everything
# typed, and sent the person to the login page for what was a stale form
# token, not an expired session. Tied to the same value so the two cannot
# drift apart again; raising SESSION_LIFETIME_HOURS now raises both.
app.config["WTF_CSRF_TIME_LIMIT"] = int(SESSION_LIFETIME_HOURS * 3600)

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


@app.before_request
def _reject_null_bytes():
    """A literal null byte in a path segment (e.g. /owners/OW001%00) or a
    query value reaches psycopg as a bound parameter and Postgres rejects
    it outright — surfacing as a raw, unhandled exception rather than a
    clean 400, since nothing upstream of the database layer ever checked
    for it. Every text/varchar column this app queries is affected the
    same way, so this is checked once, globally, rather than patched at
    each individual route."""
    if ("\x00" in request.path
            or any("\x00" in v for v in request.args.values())
            or any("\x00" in v for v in request.form.values())):
        return ("Bad Request", 400)
    # JSON-body routes (bulk-edit endpoints etc.) bypass request.form
    # entirely, so a null byte there reached Postgres unfiltered — the
    # form-value check above never runs for them. See ERROR_500_AUDIT.md
    # E-14.
    if request.method == "POST" and request.is_json and _has_null(request.get_json(silent=True)):
        return ("Bad Request", 400)
    return None


def _has_null(value):
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_has_null(k) or _has_null(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_has_null(v) for v in value)
    return False


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
    # Baseline CSP. script-src carries a per-request nonce rather than
    # 'unsafe-inline' — the sitewide template rewrite this used to say was
    # "not worth it" was done on 2026-09-10 (review finding S6): every on*=
    # attribute became a listener in static/behaviors.js, and every inline
    # <script> carries nonce="{{ csp_nonce }}". A nonce authorises <script>
    # blocks only, never inline handlers, and a browser that sees one ignores
    # 'unsafe-inline' altogether — which is why the attributes had to go first
    # rather than alongside.
    #
    # style-src still allows 'unsafe-inline' because of the inline style=
    # attributes that remain (review finding M8); tightening it is that
    # finding's job, not this one's.
    #
    # connect-src and frame-ancestors are spelled out rather than left to
    # inherit. The effective policy did not change: connect-src falls back to
    # default-src 'self', and X-Frame-Options: DENY above already blocks
    # framing. IQ has always listed both explicitly and JO has not — an
    # undocumented textual divergence with no behavioural difference, now
    # closed in the direction of saying what is meant.
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; "
        f"script-src 'self' 'nonce-{csp_nonce()}'; "
        "connect-src 'self'; frame-ancestors 'none'"
    )
    return resp


# ---------------------------------------------------------------------------
# Crash logging — every unhandled exception gets a short reference ID shown
# on the error page (safe to text/screenshot) and the full exception detail
# written here (not safe to show every role — see handle_unexpected_error).
# A dedicated file+logger, independent of the DB, so a crash caused by the
# database itself being unreachable still gets captured.
# ---------------------------------------------------------------------------
ERROR_LOG_PATH = (os.path.join(_data_dir, "logs", "errors.log") if _data_dir
                  else os.path.join(BASE_DIR, "logs", "errors.log"))
os.makedirs(os.path.dirname(ERROR_LOG_PATH), exist_ok=True)

error_logger = logging.getLogger("vetzone.errors")
error_logger.setLevel(logging.ERROR)
if not error_logger.handlers:
    _err_handler = logging.handlers.RotatingFileHandler(
        ERROR_LOG_PATH, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    _err_handler.setFormatter(logging.Formatter("%(message)s"))
    error_logger.addHandler(_err_handler)




def mark_transaction_failed():
    """Call from any error handler that runs *after* an exception has been
    caught and turned into a rendered response — @app.errorhandler(Exception)
    swallows the exception before it can reach close_db()'s teardown
    argument, so without this, teardown sees exc=None and commits whatever
    the request had already written, half-finished transaction included.
    See ORPHANED_RECORDS_AUDIT.md F-01."""
    g.db_failed = True


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
    failed = g.pop("db_failed", False)
    if db is not None:
        try:
            try:
                if exc is None and not failed:
                    db.commit()
                else:
                    db.rollback()
            except Exception:
                # teardown_appcontext runs after the response has already
                # been built, outside the request-handling flow that
                # @app.errorhandler(Exception) covers — an exception raised
                # here (e.g. the connection died mid-request, so this
                # commit/rollback itself fails) would otherwise propagate
                # straight past Flask to Waitress, replacing the app's own
                # already-built response with a raw, unbranded fault page.
                # Logged and swallowed instead.
                error_logger.error("close_db(): commit/rollback failed\n" + traceback.format_exc())
        finally:
            # Always return the connection to the pool, even if the
            # commit/rollback above raised (e.g. the connection dropped
            # mid-request) — the pool discards a connection it can't
            # reuse and opens a replacement, so this can never leak a
            # connection reference the way an un-guarded db.close() call
            # that never ran would have.
            dbmod.putconn(db)


@app.template_filter("money")
def money_filter(v):
    return logic.fmt_money(v)


def cached_dashboard_snapshot(db):
    """dashboard_snapshot() scans several tables. It's needed on every page
    (for the nav alert badge) and again on the dashboard route itself —
    cache it per-request so it only runs once."""
    if "dash_snap" not in g:
        g.dash_snap = logic.dashboard_snapshot(db)
    return g.dash_snap


@app.route("/jobs/status")
def jobs_status():
    """
    Polling endpoint for the report-page loading shells (Insights,
    Retention, Consignment Overview). Gated only by being logged in (like
    every other route, via require_login()) rather than by the specific
    report's own permission — job_id is an unguessable random token (see
    jobs.py's uuid4), so being able to supply one already implies having
    just been handed it by the page that started that job. Separate from
    /settings/job-status, which is permission-gated and has its own
    result-shaping for the Updates section — not reused here to avoid
    coupling two unrelated consumers.
    """
    job_id = request.args.get("job_id", "")
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
    if state["status"] == "error":
        payload["message"] = state.get("error")
    return jsonify(payload)


def pagination_url(page, page_param="page"):
    """Builds a link to another page of the current view, preserving every
    other query-string filter (search terms, sort, date, etc)."""
    args = request.args.to_dict()
    args[page_param] = page
    return url_for(request.endpoint, **args)


def form_value(form, name, default=""):
    """Looks up a field's just-submitted value from `form` (the raw
    request.form MultiDict, passed to render_template only when re-showing
    a form after a validation failure) so a rejected submit can redisplay
    exactly what the person typed instead of a blank/stale field. `form` is
    None on a normal GET (and on any render that isn't redisplaying a
    rejected POST), in which case `default` (usually an existing record's
    DB value, for an edit form) is used instead. Deliberately returns the
    raw submitted string as-is — no int/float/Decimal parsing — so this is
    safe to use for every field type without reintroducing a type-coercion
    bug on a value that failed validation specifically because it wasn't a
    valid number/date in the first place (and without ever turning a
    Decimal-typed money field into a float along the way). Checked with a
    truthiness test rather than `is None` because a template that never
    received a `form=` kwarg at all (the normal GET-request case) gets
    Jinja2's Undefined sentinel here, not Python None — and Undefined has
    no .get() method."""
    if not form:
        return default
    return form.get(name, default)


app.jinja_env.globals["pagination_url"] = pagination_url
app.jinja_env.globals["has_permission"] = auth.has_permission
app.jinja_env.globals["bind_port"] = BIND_PORT
app.jinja_env.globals["fv"] = form_value
app.jinja_env.globals["CLEANUP_CAP"] = CLEANUP_CAP


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------
OPEN_ENDPOINTS = {"login", "static", "health", "logout", "favicon_ico"}


@app.route("/favicon.ico")
def favicon_ico():
    # Safari (and some other browsers) probe this exact root-level path
    # directly, independent of the <link rel="icon"> tag in base.html --
    # without this route there is nothing at /favicon.ico at all (only at
    # /static/favicon.svg), so the probe 404s and Safari can fall back to
    # whatever it last had cached for this origin.
    #
    # Unlike IQ, this app ships a single SVG icon and no .ico, and has no
    # palette to choose between -- so this serves that SVG with its real
    # mimetype rather than pretending to be an ICO. Every browser that
    # probes this path also understands SVG icons, and an SVG served
    # honestly beats a 404.
    return send_from_directory(app.static_folder, "favicon.svg", mimetype="image/svg+xml")


def _warn_if_submission_will_be_lost():
    """Say so when a signed-out request was carrying data.

    require_login() redirects to /login with ?next=<path>, and login() then
    redirects to that path with a GET -- so the body of a POST is gone. The
    person sees an empty form and no indication that anything was lost, which
    on a front desk means a whole visit or bill quietly typed twice. This does
    not preserve the submission (see FULL_APP_REVIEW U2 for the stash option);
    it makes the loss visible, which is the part that actually hurt.
    """
    if request.method != "GET":
        flash("You were signed out before that could be saved, so nothing was stored. "
              "Please sign in and enter it again.", "error")


@app.before_request
def require_login():
    if request.endpoint in OPEN_ENDPOINTS or request.endpoint is None:
        return
    if not session.get("user_id"):
        _warn_if_submission_will_be_lost()
        return redirect(url_for("login", next=request.path))
    db = get_db()
    user = auth.current_user(db)
    if not user:
        session.clear()
        return redirect(url_for("login"))
    # A password change/reset stamps users.password_changed_at with a new
    # value (see change_password()/admin_user_reset_password()) — a
    # session whose login predates that no longer matches what's stored
    # here, so a stolen cookie stops working the moment the password it
    # was issued under is replaced, instead of staying valid for the rest
    # of PERMANENT_SESSION_LIFETIME. An empty stored value (pre-migration
    # row, or an old session from before this check existed) is treated as
    # "nothing to compare against yet" rather than an automatic mismatch.
    if user["password_changed_at"] and session.get("password_changed_at") != user["password_changed_at"]:
        session.clear()
        flash("Your password was changed — please log in again.", "error")
        return redirect(url_for("login"))
    auth.refresh_session_permissions(db, user)
    if user["must_change_password"] and request.endpoint != "change_password":
        return redirect(url_for("change_password"))


@app.context_processor
def inject_csp_nonce():
    """Deliberately separate from inject_globals().

    That one talks to the database and carries a fallback path for when the
    database is unreachable. A nonce dropped on that fallback path would block
    every script on the 500 page — the page you least want to break, and the
    one least likely to be looked at before release. This processor cannot
    fail for that reason because it touches nothing but `g`.
    """
    return dict(csp_nonce=csp_nonce())


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
                   current_role=session.get("role"), current_username=session.get("username"),
                   session_user_id=session.get("user_id"))
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
            alert_count=0, session_user_id=session.get("user_id"),
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
    mark_transaction_failed()
    flash("One of the number fields on that form wasn't valid. Please check the amounts and try again.", "error")
    return _fallback_redirect()


@app.errorhandler(BadPhone)
def handle_bad_phone(e):
    """Safety net for BadPhone — same idea as handle_bad_number() above."""
    mark_transaction_failed()
    flash("That phone number doesn't look valid. Please check it and try again.", "error")
    return _fallback_redirect()


@app.errorhandler(BadDate)
def handle_bad_date(e):
    """Safety net for BadDate — same idea as handle_bad_number() above.
    See ORPHANED_RECORDS_AUDIT.md F-04."""
    mark_transaction_failed()
    flash(str(e), "error")
    return _fallback_redirect()


@app.errorhandler(HTTPException)
def handle_http_exception(e):
    """Registered *after* the specific 403/404/413 handlers above, which
    Flask still prefers (exact status-code match beats a class-based one)
    — this only catches HTTPExceptions with no more specific handler:
    plain 400s (BadRequestKeyError from a missing request.form[...] key —
    see E-04), CSRFError (see E-17), and anything else in that family.
    Marks the transaction failed for non-GET requests so close_db()
    doesn't commit whatever a route had already written before aborting —
    see ORPHANED_RECORDS_AUDIT.md F-01. GET requests are excluded since
    they're not expected to have pending writes to roll back."""
    if request.method != "GET":
        mark_transaction_failed()
    if isinstance(e, CSRFError):
        # A CSRF failure is not the same thing as an expired session, and
        # saying it was sent people to a login screen they did not need --
        # after discarding what they had typed. Now that the token lifetime
        # matches the session lifetime this should be rare, but the two can
        # still come apart (a server restart rotates SECRET_KEY on some
        # deployments, invalidating every outstanding token while the browser
        # still holds a valid-looking cookie). Tell the truth about which one
        # happened, and only force a re-login when the session really is gone.
        if session.get("user_id"):
            flash("This page had been open too long to submit safely, so nothing was saved. "
                  "Please check what you entered and submit it again.", "error")
            return _fallback_redirect()
        flash("You were signed out while this page was open. Please sign in again — "
              "you may need to re-enter what you were working on.", "error")
        return redirect(url_for("login"))
    if e.code == 400:
        flash("That form was missing something the server needed. "
              "Please reload the page and try again.", "error")
        return _fallback_redirect()
    return e


@app.errorhandler(dbmod.NumericValueOutOfRange)
def handle_numeric_out_of_range(e):
    """An absurdly large integer reached a numeric database column — a
    crafted `<int:...>` URL segment, a huge quantity/amount, an id nobody
    would legitimately have — instead of a bad-but-plausible value
    BadNumber's validation would have already caught client-side. Same
    friendly-degrade pattern as BadNumber/BadPhone above."""
    flash("That number is too large to be a valid value here.", "error")
    return _fallback_redirect()


@app.errorhandler(dbmod.PoolTimeout)
def handle_pool_timeout(e):
    """The connection pool caps at a fixed size and require_login() calls
    get_db() on every single request — exhaustion doesn't degrade one
    page, it 500s everything at once. See ERROR_500_AUDIT.md E-03."""
    error_logger.error(f"DB pool exhausted on {request.method} {request.path}")
    if request.accept_mimetypes.best == "application/json" or request.path.startswith("/api/"):
        return jsonify({"error": "The system is busy right now — try again in a moment."}), 503
    return render_template("error_busy.html"), 503


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
    mark_transaction_failed()
    if isinstance(e, HTTPException):
        # No longer reachable now that @app.errorhandler(HTTPException) is
        # registered separately (Flask always prefers the more specific
        # handler) — left as defense-in-depth in case that registration is
        # ever removed.
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
        # Clears any pre-auth session state (e.g. a CSRF token issued to
        # the anonymous login page) rather than letting it survive into
        # the authenticated session — a fresh login starts a fresh session.
        session.clear()
        session["user_id"] = row["id"]
        session["username"] = row["full_name"]
        session["password_changed_at"] = row["password_changed_at"]
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
        elif auth.password_error(new, user["username"]):
            flash(auth.password_error(new, user["username"]), "error")
        elif new != confirm:
            flash("New password and confirmation don't match.", "error")
        else:
            changed_at = datetime.now().isoformat(timespec="seconds")
            db.execute("UPDATE users SET password_hash=?, must_change_password=false, password_changed_at=? WHERE id=?",
                       (auth.hash_password(new), changed_at, user["id"]))
            auth.log_change(db, "users", user["id"], "update", {"password": ("(hidden)", "(self-service change)")})
            db.commit()
            # Keeps this session logged in through its own change — only
            # OTHER sessions for this user (e.g. a stolen cookie elsewhere)
            # get invalidated by require_login()'s mismatch check.
            session["password_changed_at"] = changed_at
            flash("Password updated.", "success")
            return redirect(url_for("dashboard"))
    return render_template("change_password.html", forced=forced)


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
    # A blank Date Billed silently drops that bill from every P&L figure
    # forever (see logic._revenue_and_cogs_by_month()'s `continue`) — this
    # is the visible half of the fix in visit_billing_save(), which now
    # defaults date_billed instead of allowing it blank going forward; this
    # catches anything that slipped through before that fix, or via direct
    # SQL. See ORPHANED_RECORDS_AUDIT.md F-06.
    unbilled_count = 0
    if auth.has_permission("view_financial_reports"):
        unbilled_count = db.execute(
            "SELECT COUNT(*) c FROM billing WHERE date_billed IS NULL AND total > 0"
        ).fetchone()["c"]
    backup_alert = None
    migration_failures = None
    self_check = None
    self_check_modal = False
    if auth.has_permission("manage_settings"):
        import backup as backup_mod
        backup_alert = logic.backup_alert_message(backup_mod.last_backup(db))
        # Set by setup.apply_incremental_migrations() when a schema statement
        # fails on this launch — a per-statement failure no longer blocks
        # every later one (see setup.py), but it's still worth an admin's
        # attention. See ORPHANED_RECORDS_AUDIT.md F-22.
        migration_failures = logic.get_setting(db, "migration_failures")
        # Layer 1 of operational monitoring. Reads the last *recorded* result
        # rather than running a fresh check: run_self_check() probes the disk
        # and write-tests the backup folder, neither of which has any business
        # happening on every dashboard load. scheduler.py runs it daily (20
        # minutes after the backup) and once at startup.
        if logic.get_setting(db, "selfcheck_enabled", "1") != "0":
            import selfcheck
            row = selfcheck.latest(db)
            if row and row["status"] != "ok":
                try:
                    findings = json.loads(row["findings"] or "[]")
                except (TypeError, ValueError):
                    findings = []
                self_check = {"status": row["status"], "ran_at": row["ran_at"],
                              "findings": findings}
                # The modal, not the banner, is the point: a dismissible
                # banner is what is already being scrolled past. Three
                # consecutive failing days, holders of manage_settings only.
                self_check_modal = (row["status"] == "fail"
                                    and selfcheck.consecutive_fail_days(db) >= 3)
        # backup_alert_message() predates the self-check and every case it
        # reports (never run / failed / stranded / stale) is now covered by a
        # backup_* finding, in more detail. With both on screen the admin got
        # the same news twice in two different shapes -- once in the health
        # banner and once as a toast, since toast.js converts a .flash into
        # one. Reported 2026-08-31 off a real Test C dashboard.
        #
        # Suppressed only when the banner is actually carrying a backup
        # finding. If the self-check is switched off, has never run, or is
        # reporting something unrelated (a low disk, a rolled-back update),
        # this older alert is the only backup warning there is and must
        # survive -- the two also disagree by design, because the self-check's
        # staleness threshold is configurable (selfcheck_backup_max_age_days)
        # while this one is fixed at 2 days.
        if self_check and backup_alert and any(
                str(f.get("code", "")).startswith("backup_")
                for f in self_check["findings"]):
            backup_alert = None
    return render_template("dashboard.html", snap=snap, lan_address=lan_address(), missed=missed,
                            is_overseer=is_overseer, opex_due=opex_due, backup_alert=backup_alert,
                            unbilled_count=unbilled_count, migration_failures=migration_failures,
                            self_check=self_check, self_check_modal=self_check_modal,
                            missed_page=missed_page, missed_total_pages=page_count(missed_total),
                            missed_total=missed_total)


# ---------------------------------------------------------------------------
# Reports: Monthly & Yearly P&L (Admin only)
# ---------------------------------------------------------------------------
def _reports_context(db):
    pl = logic.monthly_pl(db)
    opex_rows = db.execute("SELECT month, rent, salaries, utilities, marketing, other FROM monthly_opex").fetchall()
    opex_by_month = {r["month"]: dict(r) for r in opex_rows}
    return dict(pl=pl, opex_by_month=opex_by_month)


@app.route("/reports")
@auth.permission_required("view_financial_reports")
def reports():
    db = get_db()
    return render_template("reports.html", **_reports_context(db))


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

    def compute(update):
        # Runs in a background thread — no Flask request/g context exists
        # here, so each query borrows its own connection from the shared
        # pool rather than reusing anything tied to the request that
        # kicked this off.
        def _run(fn):
            con = dbmod.getconn()
            try:
                return fn(con)
            finally:
                con.rollback()  # read-only; explicit rollback before returning to the pool
                dbmod.putconn(con)

        job_defs = [
            ("revenue", lambda c: logic.revenue_by_category(c, months_back=months_back)),
            ("vets", lambda c: logic.vet_performance(c, months_back=months_back)),
            ("clients", lambda c: logic.client_value(c, limit=20)),
            ("weekday_load", lambda c: logic.appointment_weekday_load(c, months_back=months_back)),
            ("occupancy", lambda c: logic.inpatient_boarding_occupancy(c, months_back=months_back)),
            ("payment_mix", lambda c: [dict(r) for r in c.execute(
                "SELECT method, COUNT(*) c, COALESCE(SUM(amount),0) total FROM payments "
                "WHERE date >= ? GROUP BY method ORDER BY total DESC",
                (cutoff,),
            ).fetchall()]),
            ("cash_register_health", lambda c: logic.cash_register_last_30_days(c)),
        ]
        results = {}
        # Capped rather than len(job_defs) — this report alone shouldn't be
        # able to claim most of the DB connection pool at once and starve
        # every other request. See ERROR_500_AUDIT.md E-03.
        with ThreadPoolExecutor(max_workers=3) as ex:
            futures = {ex.submit(_run, fn): name for name, fn in job_defs}
            done = 0
            # as_completed gives real progress: update() fires exactly when
            # each section's own query actually finishes, not on a timer
            # standing in for it.
            for fut in as_completed(futures):
                name = futures[fut]
                results[name] = fut.result()
                done += 1
                update(done)

        top_clients, avg_spend, active_client_count = results["clients"]
        return {
            "revenue": results["revenue"], "vets": results["vets"],
            "top_clients": top_clients, "avg_spend": avg_spend,
            "active_client_count": active_client_count,
            "weekday_load": results["weekday_load"], "occupancy": results["occupancy"],
            "payment_mix": results["payment_mix"], "months_back": months_back,
            "cash_register_health": results["cash_register_health"],
        }

    return _render_with_progress(
        "insights.html",
        ["Revenue by category", "Vet performance", "Client value",
         "Weekday appointment load", "Inpatient/boarding occupancy", "Payment mix", "Cash Register health"],
        compute,
        page_title="Loading Insights",
        page_note="Running six report queries in parallel.",
    )


@app.route("/retention")
@auth.permission_required("view_insights_retention")
def retention():
    page = get_page()

    def compute(update):
        # Runs in a background thread, so its own connection (not g.db).
        con = dbmod.connect()
        try:
            full = logic.cohort_retention_grid(con, max_offset=11)
        finally:
            con.close()
        total = len(full["grid"])
        total_pages = page_count(total)
        eff_page = min(page, total_pages)
        offset = page_offset(eff_page)
        page_grid = full["grid"][offset:offset + PER_PAGE]
        cohort = {"cohort_months": full["cohort_months"][offset:offset + PER_PAGE],
                  "offsets": full["offsets"], "grid": page_grid}
        return {"cohort": cohort, "page": eff_page, "total_pages": total_pages, "total_count": total}

    return _render_with_progress(
        "retention.html",
        ["Computing cohort retention grid"],
        compute,
        page_title="Loading Retention",
        page_note="Computing cohort retention across every month with visit history.",
    )


@app.route("/reports/opex", methods=["POST"])
@auth.permission_required("view_financial_reports")
def reports_opex_save():
    db = get_db()
    f = request.form

    def redisplay():
        return render_template("reports.html", **_reports_context(db), form=f)

    month = f.get("month", "").strip()
    if not month:
        flash("Pick a month first.", "error")
        return redisplay()
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        flash("That's not a valid month.", "error")
        return redisplay()
    try:
        rent = parse_money(f.get("rent")) or 0
        salaries = parse_money(f.get("salaries")) or 0
        utilities = parse_money(f.get("utilities")) or 0
        marketing = parse_money(f.get("marketing")) or 0
        other = parse_money(f.get("other")) or 0
    except BadNumber:
        flash("Operating costs must be valid numbers.", "error")
        return redisplay()
    # A negative operating cost does not reduce spending, it reads as income:
    # yearly_pl() computes net_profit = gross_profit - total_opex, so a
    # negative column makes total_opex smaller and the reported profit LARGER.
    # A single mistyped "-100,000" rent moved the annual net profit figure by
    # +200,000 in testing.
    if has_negative(rent, salaries, utilities, marketing, other):
        flash("Operating costs can't be negative.", "error")
        return redisplay()
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


@app.route("/health")
def health():
    """Used by updater.py to confirm a new release actually boots and can
    reach the database — not just that the process started. No auth
    required (harmless — reveals nothing beyond the version string; the
    updater probes this on a throwaway localhost port before the release
    it's checking is ever promoted)."""
    try:
        get_db().execute("SELECT 1")
        return {"status": "ok", "version": VERSION}, 200
    except Exception:
        # This endpoint is in OPEN_ENDPOINTS -- no login required -- so
        # whatever it returns is readable by anyone who can reach the app.
        # It used to return str(e), and psycopg's connection errors carry the
        # database host, port and user inline:
        #   connection to server at "127.0.0.1", port 5432 failed:
        #   FATAL: password authentication failed for user "vetclinic"
        # That path is reachable whenever the pool has no live connection --
        # the app starting before Postgres is the obvious way. The reference
        # id ties this response to the full traceback in logs/errors.log,
        # which is already access-controlled.
        #
        # updater.py's _probe_health() only reads `status`, and the Settings
        # page's restart poll only checks that the request succeeds, so
        # nothing consumes the old free-text detail.
        error_id = uuid.uuid4().hex[:8].upper()
        error_logger.error(f"[{error_id}] /health check failed\n" + traceback.format_exc())
        return {
            "status": "error",
            "detail": f"The application could not reach its database. "
                      f"Reference {error_id} — see logs/errors.log on the server.",
        }, 503


# ---------------------------------------------------------------------------
# Route blueprints
# ---------------------------------------------------------------------------
# Areas of the app that have moved out of this file. Each module under routes/
# owns one area and exposes `bp`; they import shared request-layer pieces from
# core.py, never from here, because this is the module that registers them.
#
# Registering a blueprint prefixes its endpoint names: settings_page becomes
# settings.settings_page. url_for() raises BuildError at render time on a name
# that no longer exists, so a missed rename fails at the first page load rather
# than at the first click.
from routes import settings as settings_routes

app.register_blueprint(settings_routes.bp)
from routes import clinical as clinical_routes

app.register_blueprint(clinical_routes.bp)
from routes import sales as sales_routes

app.register_blueprint(sales_routes.bp)
from routes import inventory as inventory_routes

app.register_blueprint(inventory_routes.bp)
from routes import consignment as consignment_routes

app.register_blueprint(consignment_routes.bp)
from routes import admin as admin_routes

app.register_blueprint(admin_routes.bp)


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

    try:
        import scheduler
        scheduler.start(get_db=dbmod.connect, close_db=lambda c: c.close())
    except Exception:
        # A scheduler failure should never take the whole app down — the
        # front desk still needs to open. See ERROR_500_AUDIT.md E-01.
        error_logger.error("Nightly backup scheduler failed to start:\n" + traceback.format_exc())
        print("  !! Nightly backups are NOT scheduled — see logs/errors.log. The app will still run.")

    try:
        import backup as boot_backup_mod
        boot_conn = dbmod.connect()
        try:
            reaped = boot_backup_mod.reap_stale_running(boot_conn)
            if reaped:
                print(f"  Reaped {reaped} stale 'running' backup log row(s) from an earlier, killed run.")
        finally:
            boot_conn.close()
        # Makes "no restore has happened" provable rather than assumed —
        # see ORPHANED_RECORDS_AUDIT.md F-20.
        boot_backup_mod.ensure_no_restore_marker()
    except Exception:
        error_logger.error("Boot-time backup/restore-marker housekeeping failed:\n" + traceback.format_exc())

    def _graceful_shutdown(signum, frame):
        """
        Runs on SIGTERM/SIGINT (Ctrl-C)/SIGBREAK — sent by the OS on
        shutdown/restart/logout, or by a person closing the launcher
        window. Every write this app makes is already committed
        per-request (see close_db/teardown_appcontext above), so there's
        no in-flight "unsaved" transaction sitting on the server side to
        lose here. What this actually guards against is Postgres (running
        in Docker or as a local service) getting killed abruptly in the
        same shutdown sequence with nothing recent to fall back on — so:
        take one more backup as a last safety net, then exit cleanly
        instead of being hard-killed mid-request.
        """
        print("\nVetClinicSystem JO is shutting down \u2014 taking a final backup first...")
        try:
            db = dbmod.connect()
            try:
                import backup as backup_mod
                ok, message = backup_mod.run_backup(db, triggered_by="shutdown")
                print(message if ok else f"Final backup failed: {message}")
            finally:
                db.close()
        except Exception as e:
            print(f"Could not take a final backup during shutdown: {e}")
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
