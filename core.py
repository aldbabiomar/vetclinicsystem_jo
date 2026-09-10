"""
The few pieces of VetClinicSystem JO that both `app.py` and the route blueprints
under `routes/` need.

This module exists to break what would otherwise be a circular import: app.py
creates the Flask app and registers the blueprints, so a blueprint cannot
import from app.py. Everything here is deliberately small and dependency-free
in that direction — it imports Flask and `db`, and nothing from this
application's own request layer.

Nothing here changed behaviour when it moved out of app.py; these are the same
definitions, in a place both sides can reach.
"""
import math
import os
import re
import socket
from datetime import datetime
from decimal import Decimal, InvalidOperation

from flask import flash, g, render_template, request, url_for

import db as dbmod
import jobs
import logic

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# On the versioned-release layout (VETCLINICSYSTEMJO_DATA_DIR set by the
# launcher script — see updater.py / setup.py --enable-updates), the .env, the
# logs and the uploads live in the persistent data dir rather than beside this
# file, whose folder an in-app update replaces and later prunes.
DATA_DIR = os.environ.get("VETCLINICSYSTEMJO_DATA_DIR")

_version_path = os.path.join(BASE_DIR, "VERSION")
VERSION = open(_version_path).read().strip() if os.path.exists(_version_path) else "unknown"

# Separate from (and shorter than) DB_POOL_TIMEOUT_SECONDS, which the pool
# itself still uses for background/maintenance callers (dbmod.connect()).
# During a full DB outage every request that reaches get_db() would otherwise
# block for the pool's full default wait before failing — tying up one of
# Waitress's worker threads that whole time and making the app look hung rather
# than degraded.
DB_REQUEST_TIMEOUT_SECONDS = float(os.environ.get("DB_REQUEST_TIMEOUT_SECONDS", "4"))


def get_db():
    """The request-scoped connection. Borrowed from the pool on first use and
    returned by app.py's close_db() teardown."""
    if "db" not in g:
        g.db = dbmod.getconn(timeout=DB_REQUEST_TIMEOUT_SECONDS)
    return g.db


def lan_address():
    """This machine's address on the clinic LAN, for the Settings page's
    "reach it from another device at ..." line. Falls back to loopback rather
    than raising when there is no route out."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ---------------------------------------------------------------------------
# Shared request-layer helpers
# ---------------------------------------------------------------------------
# Form parsing and validation, pagination, and the background-job render shell.
# These were in app.py, where every blueprint would have had to import from the
# module that registers it. They are pure helpers over `request` and the parsed
# values -- no route, no database of their own.
#
# parse_money(), MAX_MONEY and the PHONE_* constants differ between the two
# apps on purpose (IQ is whole-IQD float with 250-note rounding, JO is exact
# three-decimal Decimal; the phone formats differ too). Each app's own version
# moved here; they must not be merged. See COMPARISON.md §1.1.
class BadNumber(ValueError):
    """Raised by parse_money() when a submitted field isn't blank but also
    isn't a valid number — lets the route catch it once and show a friendly
    error instead of a raw ValueError (Python) or invalid-input-syntax
    error (Postgres) turning into an uncaught 500."""


# The widest value any NUMERIC(12,3) column in this schema can hold. Checked
# here, once, rather than at each call site — an amount past this is always a
# typo (a phone number into a price field is the common one), and letting it
# reach Postgres turns that typo into a 500 mid-form instead of a flash. See
# ERROR_500_AUDIT.md E-06.
MAX_MONEY = Decimal("999999999.999")
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
    if abs(val) > MAX_MONEY:
        raise BadNumber(f"{raw} is too large — check for a typo.")
    return val


MAX_INT = 2_147_483_647  # widest value any INTEGER column in this schema can hold


def parse_int(raw, required=False):
    """Same shape as parse_money(), for INTEGER columns (e.g. lead_time_days).
    Blank collapses to None; non-numeric input raises BadNumber instead of
    reaching the DB and surfacing as a raw Postgres cast error. Bounded at
    MAX_INT so an oversized value degrades to a flash instead of a raw
    Postgres overflow error. See ERROR_500_AUDIT.md E-06."""
    if raw is None or str(raw).strip() == "":
        if required:
            raise BadNumber("required")
        return None
    try:
        val = int(raw)
    except ValueError:
        raise BadNumber(raw)
    if abs(val) > MAX_INT:
        raise BadNumber(f"{raw} is too large — check for a typo.")
    return val


class BadDate(ValueError):
    """Raised by clean_date() when a submitted date field isn't blank but
    also isn't a valid ISO date — lets the route catch it once and show a
    friendly error instead of a raw ValueError/Postgres 'invalid input
    syntax for type date' turning into an uncaught 500. More importantly:
    this is what stops an empty string ('') from ever reaching a date
    column. '' is not NULL, so a downstream query that assumes a date
    column is only ever 'a real date or NULL' (e.g. `WHERE date IS NOT
    NULL` followed by `date::date`) breaks the moment it meets one. That is
    not hypothetical: a blank date reaching a nullable date column is what
    made reports skip bills entirely and comparisons behave as though the
    row had no date at all."""


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


# ---------------------------------------------------------------------------
# Pagination — 50 rows/page across every list view in the system
# ---------------------------------------------------------------------------
PER_PAGE = 50


MAX_PAGE = 100_000  # generous for any realistic list size; keeps page_offset() well inside Postgres's integer range


def get_page():
    try:
        p = int(request.args.get("page", 1))
    except (TypeError, ValueError):
        p = 1
    return min(max(1, p), MAX_PAGE)


def page_count(total, per_page=PER_PAGE):
    return max(1, (total + per_page - 1) // per_page)


def page_offset(page, per_page=PER_PAGE):
    return (page - 1) * per_page


# ---------------------------------------------------------------------------
# Slow report pages (Insights, Retention, Consignment Overview): background
# job + loading shell
# ---------------------------------------------------------------------------
def _render_with_progress(template_name, step_labels, compute_fn, page_title, page_note=None):
    """
    Shared pattern for report pages slow enough to need a progress bar
    (Insights, Retention, Consignment Overview). On first visit, starts a
    background job running compute_fn(update) (which must return the dict
    of template variables the real page needs) and renders a lightweight
    loading shell instead; once the client's poll reports the job done,
    the shell redirects back to this same URL with ?job_id=..., and THIS
    SAME route then picks up the finished job's already-computed result
    and renders the real page.

    The slow part only ever runs once, in the background thread; the
    actual page render always happens inside a normal request, so
    session/g/url_for/current-user context all work exactly as they
    always do (rendering from a background thread would have none of
    that, which is why the heavy lifting and the rendering are kept in
    two separate steps like this rather than trying to render from the
    thread directly).
    """
    job_id = request.args.get("job_id")
    if job_id:
        result = jobs.take_result(job_id)
        if result is not None:
            return render_template(template_name, **result)
        # Not found / not finished yet / already consumed (a stale or
        # reloaded link) — fall through and start a fresh job below
        # rather than erroring, since any of those are recoverable just
        # by trying again.

    new_job_id = jobs.start(step_labels, compute_fn)
    # Preserve whatever other query params got here (e.g. ?page=2 on
    # Retention) rather than dropping them — built as a real url_for() call
    # so the client just navigates to a finished URL rather than having to
    # reconstruct it with string concatenation.
    other_args = {k: v for k, v in request.args.items() if k != "job_id"}
    reload_url = url_for(request.endpoint, job_id=new_job_id, **other_args)
    return render_template("_loading_shell.html", job_id=new_job_id, reload_url=reload_url,
                            page_title=page_title, page_note=page_note)


def required_field(f, key, label):
    """Returns the stripped value, or None (with a flash already set) if
    it's missing or blank. Covers both the KeyError case (f[key] on a
    missing key raises BadRequestKeyError) and the empty-string case,
    which NOT NULL alone does not. See ERROR_500_AUDIT.md E-04."""
    val = (f.get(key) or "").strip()
    if not val:
        flash(f"{label} is required.", "error")
        return None
    return val


# The payout methods a refund may record. Mirrors IQ, and matches exactly
# what refunds.html already offers in its dropdown -- the list was only ever
# in the template here, so the server accepted anything (including nothing).
PAYMENT_METHODS = ["Cash", "Card", "Transfer"]


def discount_percent_error(percent, cap):
    """Range-checks an already-parsed discount percent against the current
    user's role cap. Shared by the visit/inpatient/boarding/POS discount-save
    routes so the bound comparison lives in exactly one place — it was written
    out four times, which is four chances to change three of them."""
    if percent > cap or percent < 0:
        return f"Discount must be between 0% and {cap}% for your role."
    return None


def cleanup_amount_error(new_amount, existing_amount, balance):
    """Range-checks a Clean Up submission. Returns an error string, or None.

    Shared by the four payment surfaces, which were each carrying their own
    copy of these three checks. The two legitimate per-site differences are
    arguments rather than special cases:

      * POS passes existing_amount=0 — a brand-new sale has no prior Clean Up
        to accumulate against, unlike the other three, which can be paid off
        across several submissions.
      * Boarding passes the balance as it would stand AFTER this submission's
        discount, not before, so a discount-and-clean-up in one click cannot
        write off more than the discounted bill.

    JOD is exact three-decimal Decimal — no denomination rounding — so these
    are straight comparisons. IQ's copy compares floats against a 250-rounded
    cap; the two must not be merged (COMPARISON.md §1.1).
    """
    if new_amount < 0:
        return "Clean Up amount can't be negative."
    if existing_amount + new_amount > CLEANUP_CAP:
        return f"Clean Up can't exceed {CLEANUP_CAP} JOD total on this bill."
    if new_amount > balance:
        return "Clean Up can't exceed the remaining balance."
    return None


def parse_quantity(raw, required=False):
    """Same shape as parse_money(), for NUMERIC(10,3) quantity columns
    (POS cart, refund lines, inpatient billing) — bounded at that column
    type's own ceiling rather than MAX_MONEY's wider one. See
    ERROR_500_AUDIT.md E-06."""
    if raw is None or str(raw).strip() == "":
        if required:
            raise BadNumber("required")
        return None
    try:
        val = Decimal(str(raw).strip())
    except InvalidOperation:
        raise BadNumber(raw)
    if not val.is_finite():
        raise BadNumber(raw)
    if abs(val) > MAX_QUANTITY:
        raise BadNumber(f"{raw} is too large — check for a typo.")
    return val


def clean_date_filter(v):
    """For a read-side ?date= filter that's about to be compared against a
    real DATE column (or used as a LIKE prefix against a text timestamp) —
    clean()'s intent ("a bad filter should degrade to no hard error") isn't
    actually met by clean() alone, since a malformed-but-non-empty string
    still reaches the query. A DATE column then raises a raw Postgres cast
    error instead of degrading to anything. Returns the value only if it's
    a real YYYY-MM-DD date; a malformed one is silently dropped (treated
    the same as no filter at all) rather than either crashing or being
    passed through as a broken filter."""
    v = clean(v)
    if v is None:
        return None
    try:
        datetime.strptime(v, "%Y-%m-%d")
    except ValueError:
        return None
    return v


def date_filter_arg(name="date", message="That date wasn't valid — showing all dates instead."):
    """clean_date_filter() with a heads-up for the user.

    Dropping a malformed filter silently is the right default for a shared
    context builder that several routes re-render through (see
    _refunds_page_context) — a stray ?date= shouldn't add noise on top of a
    real validation error. But on the page the user actually asked for,
    silence is indistinguishable from "the filter worked and there's just a
    lot of data". IQ has always said so on these pages; this is what brings
    JO's list pages in line. Only speaks up when something was actually
    thrown away — an absent or empty ?date= is not an error."""
    raw = request.args.get(name)
    value = clean_date_filter(raw)
    if value is None and clean(raw) is not None:
        flash(message, "error")
    return value


# Flat ceiling on the cumulative "Clean Up" write-off allowed per bill —
# see CLEANUP_FEATURE_PLAN.md §3.3. Not per-role; a global constant.
CLEANUP_CAP = Decimal("1.000")


MAX_QUANTITY = Decimal("9999999.999")  # widest value any NUMERIC(10,3) column can hold
