# Applicability of `AUDIT_FINDINGS.md` (VetClinicSystem_IQ) to VetClinicSystem_JO

**Purpose:** cross-check every finding in `/Users/omaraldbabi/Desktop/AUDIT_FINDINGS.md` (a security/bug audit run against VetClinicSystem_IQ v1.4.6) against JO's actual code, specifically through the lens of how JO's money and business logic differs (Decimal/NUMERIC vs. float, JOD's 3-decimal precision vs. IQD's whole-number precision, no note-denomination rounding concept).

**Nothing in this document has been fixed.** This is a read-only comparison, per instruction — every item below was checked by reading JO's actual source, not assumed from IQ's report.

**Headline result:** the vast majority of IQ's findings — including all the money-related ones — reproduce identically in JO, because the affected code (`app.py` route handlers) was written independently but follows the same validation patterns, not because it was copy-pasted. Two items are meaningfully **worse** in JO specifically because of how the port/adaptation was done (flagged prominently below, since these are new findings this review surfaced, not just "does IQ's finding apply"). A handful of IQ's findings **don't apply at all**, because the affected feature doesn't exist in JO.

---

## MONEY & BUSINESS LOGIC — the section you asked about specifically

### M1 (POS checkout accepts cash below total) — **APPLIES IDENTICALLY**
`app.py`'s `pos_checkout()`: `change_given = max(round(cash_received - total, 3), 0)` — no check that `cash_received >= total` anywhere before this. A cashier can record a sale as fully paid having taken less cash than the total, same as IQ. JOD's 3-decimal precision doesn't change this at all — it's a missing-validation bug, not a rounding artifact.

### M2 (Service refunds uncapped vs. amount paid) — **APPLIES IDENTICALLY**
`refund_service_save()` only checks `amount <= 0` and that the visit/case exists — no cap against what was actually paid, no reference to prior refunds. Confirmed the same asymmetry IQ found: the retail refund path (`refund_retail_save()`) correctly caps against `refundable_sale_items()`'s remaining-quantity math, but the service path has no equivalent cap.

### M3 (Banker's-rounding / denomination-rounding bug) — **DOES NOT APPLY** (different reason than "fixed")
JOD isn't rounded to a physical note denomination the way IQD is (JO has no `money.py`, no `round_to_denomination()` at all — confirmed via repo-wide grep). This whole class of bug is architecturally absent, not fixed. See the new finding below, though — a *different* currency-precision bug exists in the same function this finding points at.

### M4 (Sub-denomination totals bumped up) — **DOES NOT APPLY**
Same reason as M3 — no denomination rounding exists to bump anything up or down.

### 🆕 New finding surfaced by this review — not in IQ's list, JOD-specific

**`logic.py:433` `compute_bill_totals()`: `elif balance <= 0.5: status = "Fully Paid"` — the `0.5` threshold was carried over from IQD without rescaling for JOD's precision.**

In IQ, this same line exists (`money.py`'s `round_to_denomination()` rounds `balance` to a multiple of 250 IQD first, so `<= 0.5` there is really just "absorb floating-point noise around zero" — completely inconsequential, since the smallest real IQD note is 250). In JO, `balance` is a real `Decimal` computed to 3-decimal (fils) precision with **no** denomination rounding applied to it first. `<= 0.5` here means: **any bill left with up to half a Jordanian Dinar (500 fils, real money) genuinely still owed displays as "Fully Paid."**

This is used by *every* billing type — `compute_bill_totals()` is shared by visit billing, inpatient billing, and a third caller (confirmed via `logic.py:467,506,560`) — so it's systemic, not a one-off. This is the exact kind of currency-precision bug the Decimal/NUMERIC conversion work earlier this session was meant to catch, but it was inside a *comparison threshold* rather than an arithmetic operation, so it never raised a `TypeError` the way the float/Decimal mixing bugs did — nothing crashed, so nothing surfaced it until this review looked specifically for it.

**Not fixed here per your instruction** — flagging only. If you want it addressed later, the fix is straightforward (rescale the threshold to something JOD-appropriate, e.g. a few fils, or make it exactly `== 0` now that everything is exact `Decimal` arithmetic with no floating-point noise to absorb in the first place).

### M5 (Visit/inpatient payments unlimited overpayment) — **APPLIES IDENTICALLY**
`visit_payment_add()` and `inpatient_payment_add()` both only check `amount <= 0`. `boarding_payment()` correctly locks the row and caps at `balance` — same asymmetric pattern IQ found, reproduced exactly (down to boarding being the one safe path in both apps).

### M6 (Consignment settlement can overpay) — **APPLIES IDENTICALLY**
`consignment_settlement_new()` only rejects `amount_paid < 0`; nothing caps at `balance["amount_owed"]`. Residual can go negative exactly as in IQ.

### M7 (Cash-register payouts unbounded) — **APPLIES IDENTICALLY**
`cash_register_payout_new()` only requires `amount > 0` + a reason string — no cap against the drawer's actual cash total.

### M8 (TOCTOU between discount validation and billing save) — **APPLIES IDENTICALLY**
Confirmed no `FOR UPDATE` lock on the `billing` row between `visit_discount_save()`'s non-discountable check and `visit_billing_save()`'s write, or vice versa — same millisecond race window. Worth noting: JO's `visit_billing_save()` already re-validates non-discountable items on every save specifically to close the *sequential* version of this gap (its own comment says so explicitly) — but that's a different fix from closing the *concurrent* race IQ's M8 describes, which remains open in both apps.

### M9 (Retail refund rounding can exceed refunded value) — **DOES NOT APPLY**
Same reason as M3/M4 — no `round_to_denomination`, so there's no upward-rounding-past-the-refunded-lines behavior to have. JO's retail refund total is the exact sum of the refunded lines' `Decimal` amounts.

### M10 (Money columns use `double precision`) — **DOES NOT APPLY — already fixed, deliberately, this session**
This is the one IQ finding JO has a real, verified answer for: every money-bearing column in `schema_postgres.sql` is `NUMERIC(12,3)`/`NUMERIC(10,3)`/`NUMERIC(5,2)`, not `double precision` — the Decimal/NUMERIC conversion done earlier this session exists specifically because JOD (unlike IQD) is a genuine 3-decimal-place currency where float drift is real and observable. Confirmed via `schema_postgres.sql` and `parse_money()`'s `Decimal` return type.

---

## Other sections — checked, briefer since you asked specifically about money/business logic

### Authentication & Sessions

| ID | Applies to JO? |
|---|---|
| A1 (default `admin`/`admin123`, documented) | **Yes** — `import_seed.py:87`, `README.md:52` |
| A2 (sliding lockout → permanent DoS) | **Yes** — identical `LOCKOUT_THRESHOLD=5`/`LOCKOUT_WINDOW_MINUTES=15` in `auth.py`, same sliding-window computation |
| A3 (password change doesn't invalidate other sessions) | **Yes** — no `password_changed_at` epoch anywhere in `app.py`/`auth.py` |
| A4 (no `session.clear()` at login) | **Yes** — `app.py`'s `login()` sets `session["user_id"]` directly with no clear first |
| A5 (forced-password-change traps Logout) | **Yes** — `OPEN_ENDPOINTS = {"login", "static", "health"}` doesn't include `logout`; the `must_change_password` gate redirects every other endpoint |
| A6 (in-process rate limiter) | **Yes** — same `_LOGIN_ATTEMPTS_BY_IP` in-memory dict pattern |

### Files, Server & Supply Chain

| ID | Applies to JO? |
|---|---|
| F1 (`/api/browse-folder` lists whole FS) | **No — feature doesn't exist.** JO's Settings backup-folder field is a plain text input; confirmed zero routes matching `browse-folder`/`new-folder` anywhere in `app.py` or `templates/settings.html`. |
| F2 (folder-creation anywhere) | **No** — same reason as F1. |
| F3 (updater extracts tarballs without member filtering) | **Yes** — `updater.py:156`, `tf.extractall(tmp)` with the same trust-the-source comment. Makes sense: `updater.py` was ported verbatim from IQ this session. |
| 🆕 F4 (attachment deletion has no ownership check) — **worse in JO** | IQ's `/attachments/<id>/delete` requires `manage_visits` OR `manage_inpatient` (`app.py:2109`). **JO's equivalent route has no `@auth.permission_required(...)` decorator at all** — confirmed by reading the route definition directly. Any logged-in user, regardless of role, can delete any patient's attachment by sequential ID. Same audit-logged-at-least mitigation as IQ, but the permission gap itself is strictly wider in JO. |
| F5 (backup dump staging/permissions) | Not checked in depth — JO's `backup.py` doesn't use the same `/tmp/<basename>` staging path IQ's does (different `_run_pg_dump()` implementation, writes directly to the configured backup folder via `pg_dump -f`), so the specific symlink-race description doesn't transfer as-written; would need its own look if this matters to you. |

### XSS, Client & Headers

| ID | Applies to JO? |
|---|---|
| X1 (folder-browser XSS via unescaped filenames) | **No** — depends on F1/F2's folder-browser feature, which doesn't exist in JO. |
| X2 (Werkzeug debugger RCE when dev env var set) | **Yes** — same `app.run(debug=True, host=bind_host, ...)` pattern gated by `VETCLINICSYSTEMJO_DEV` |
| X3 (full traceback shown to every role) | **Yes** — same `handle_unexpected_error()` design (deliberately un-gated by role, same `redact_sensitive()` mitigation for PII in the message — this exists in both apps, not JO-specific) |
| X4 (no CSP) | **Yes** — confirmed no `Content-Security-Policy` header set anywhere in `app.py` |
| X5 (vendored htmx outdated) | **No** — JO doesn't vendor htmx at all (no `static/vendor/htmx*` found) |

### SQL & Data Integrity

| ID | Applies to JO? |
|---|---|
| S1 (unwhitelisted ORDER BY direction) | **No** — checked the equivalent sortable-list routes (e.g. `patients_list()`); JO's `direction_sql = "DESC" if direction == "desc" else "ASC"` pattern is already whitelisted everywhere checked. IQ's specific flagged location (`logic.py:1693,1717`, inpatient sorting) has no JO equivalent — the admission-date queries here use a literal `DESC`. |
| S2 (unvalidated date filter into LIKE) | **Yes** — `pos_history()` builds `date_filter + "%"` into `LIKE ?` without running it through `clean_date`/`parse_date` first, identical to IQ. |
| S3 (placeholder regex comment overclaims quote-awareness) | **Yes** — `db.py`'s `_PLACEHOLDER_RE`/`_translate()` is line-for-line the same code and comment as IQ's. |

### Robustness

| ID | Applies to JO? |
|---|---|
| R1 (huge `page` param → 500) | **Yes** — `get_page()` clamps `p >= 1` only, no upper bound; `page_offset()` multiplies unboundedly. |
| R2 (null byte in ID → raw 500) | Very likely yes (same psycopg/Postgres driver-level behavior on a null byte in a query parameter, same lack of a global handler for it) — not live-tested against JO in this pass, but nothing in the request-handling path differs from IQ in a way that would change this. |

---

## Summary

**Everything in the MONEY & BUSINESS LOGIC section that's a missing-validation bug (M1, M2, M5, M6, M7, M8) applies to JO exactly as described in IQ's audit** — these are gaps in `app.py`'s own route logic, independent of currency precision, and JO's routes have the identical gaps. **Everything that's specifically about IQD's note-denomination rounding (M3, M4, M9) doesn't apply**, because that mechanism doesn't exist in JO at all. **M10 is the one already fixed**, deliberately, for exactly the reason JOD needed it.

The one thing this review found that IQ's audit *couldn't* have found (since it doesn't exist there) is the `balance <= 0.5` threshold in `compute_bill_totals()` — a currency-scale-dependent magic number that was correct for IQD's context and silently became a real, systemic "forgive up to half a Dinar of debt" bug once the surrounding arithmetic switched to JOD's precision. That's the most important finding in this document, precisely because it's JO-specific and money-related — worth prioritizing over the identical-to-IQ items if you do decide to act on any of this later.
