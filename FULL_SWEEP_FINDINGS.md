# VetClinicSystem JO — Full Phased Sweep Findings

**Date:** 2026-08-22 · **Scope:** entire codebase (~9,700 LOC across 13 first-party modules, 134 routes), fresh investigation — not a reuse of the earlier IQ cross-check.
**Status:** investigation only. Nothing below has been fixed. Each item needs a decision; accepted items ship together in one release once you respond.

Already shipped this session (not repeated here): M1/M2/M5/M6/M7/M8, the `compute_bill_totals` currency-scale bug, and the `attachment_delete` permission gap.

---

## Phase 1 — Money & Business Logic

Swept every money-writing route not already fixed: boarding payments, distributor bills/payments, consignment receiving/shrinkage/returns, appointments, operating costs. **No new findings** — all of these already lock the relevant row before checking, and cap against a real balance/stock figure computed fresh at submit time (`distributor_payment_new`, `boarding_payment`, `record_consignment_shrinkage`, `record_consignment_return` are all correctly written — these were the reference patterns the earlier fixes copied). Appointments rely on a real DB unique index for the double-booking guarantee, with a friendly pre-check as a fast path — solid.

Nothing to accept/decline in this phase — it's clean.

---

## Phase 2 — Authentication & Sessions

These were flagged in the earlier IQ-applicability review as applying identically to JO but weren't part of the fix list you approved yet. Re-confirmed fresh, plus one new variant found.

### 2.1 — Sliding account lockout enables permanent lockout-DoS of any account
**Severity: Medium-High.** `auth.py`'s lockout window slides forward on every new failed attempt (computed from `MAX(timestamp)` of recent failures), so one bad-password request every ~14 minutes keeps any known username (`admin` is documented in the README) locked out indefinitely — denial of administration.
**Proposed fix:** compute the unlock time from the *first* failure in the window, not the most recent — a fixed 15-minute lockout per burst instead of one that resets on every additional guess.

### 2.2 — Password change/reset doesn't invalidate other live sessions
**Severity: Medium.** Neither self-service `change_password()` nor admin's `admin_user_reset_password()` invalidate sessions already logged in elsewhere. A stolen cookie (shared kiosk, malware) keeps working for up to 12 hours after the password is changed — including after an **admin explicitly resets a compromised account's password**, which defeats the point of that action.
**Proposed fix:** store a `password_changed_at` epoch on the user row; check it against the session's login time on each request, invalidating sessions that predate the most recent change/reset.

### 2.3 — No `session.clear()` at login
**Severity: Low.** Pre-auth session state survives into the authenticated session. Minor hygiene issue, not currently exploitable for anything specific here.
**Proposed fix:** `session.clear()` before setting `user_id` in `login()`.

### 2.4 — Forced-password-change gate traps a user who clicks Logout
**Severity: Low (UX bug, not a security hole).** `OPEN_ENDPOINTS` doesn't include `logout`, so a user who must change their password and clicks Logout gets redirected back to Change Password instead of actually logging out.
**Proposed fix:** add `"logout"` to `OPEN_ENDPOINTS`.

### 2.5 — Default credentials documented in README
**Severity: Medium, but by design.** `admin`/`admin123` is seeded and forced to change on first login (`must_change_password`) — this is an intentional first-run mechanism, not a bug, and the actual live instance already had its password changed during earlier testing this session. No code change proposed; flagged only so you're aware it's the same pattern IQ's audit called out.

### 2.6 — In-process login rate limiter (info only)
**Severity: Info.** Resets on restart, ineffective if the app ever runs multi-process. Fine for the current single-process Waitress deployment. No action proposed.

---

## Phase 3 — Files, Server & Supply Chain

### 3.1 — Backup dump files aren't restricted to owner-only permissions
**Severity: Low-Medium.** `backup.py`'s `_run_pg_dump()` writes the `.dump` file (via `pg_dump -f` or a plain `open(out_path, "wb")` in the Docker fallback) with no `chmod` afterward — it inherits whatever the process umask happens to be, potentially group- or world-readable. This matters more here than it might elsewhere: the Settings page's own documentation explicitly recommends pointing the backup folder at **a synced folder like Google Drive/OneDrive**, and every dump contains full patient/owner PHI (names, phones, addresses, medical history). A permissive dump sitting in a synced folder is a real, not theoretical, exposure on a shared or multi-user machine.
**Proposed fix:** `os.chmod(out_path, 0o600)` immediately after each dump completes, in both the native and Docker-exec code paths.

### 3.2 — Updater extracts release tarballs without member filtering
**Severity: Low-Medium, supply-chain dependent.** `updater.py`'s `tf.extractall(tmp)` trusts the tarball's contents (path traversal, odd permissions) with only a comment noting the source is trusted. Tarball contents are later imported and executed (`_check_imports`, schema sync), so a compromised release or repo would mean full code execution on the machine running it.
**Proposed fix:** `tf.extractall(tmp, filter="data")` (Python ≥3.12, which this app already targets) to reject unsafe archive members automatically.

### 3.3 — Folder-browser / arbitrary-filesystem-listing feature
**Does not apply.** No `/api/browse-folder` or folder-creation endpoint exists in JO at all (Settings' backup-folder field is a plain text input) — this whole class of exposure that IQ has simply isn't present here.

### 3.4 — In-app database restore
**Does not apply.** JO has no web-exposed restore feature (`pg_restore` is documented as a manual command-line step only) — no restore-related attack surface to review.

---

## Phase 4 — XSS, Client & Headers

Every template flagged for `innerHTML` usage (8 files: inpatient, visit, boarding, POS, refunds, audit session, inventory catalog) was individually checked. **All of them either escape user-controlled strings via the shared `escapeHtml()` helper, or build DOM nodes via `textContent`/`createElement` instead of `innerHTML` string-building.** No Jinja `|safe`, `Markup(`, or `render_template_string` anywhere in the codebase. This is the one area of the sweep that came back completely clean — no findings.

### 4.1 — No Content-Security-Policy header
**Severity: Low.** Confirmed no `Content-Security-Policy` header is set anywhere (`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` are already set — this is the one still missing). Given the templates are inline-script-heavy, a real CSP would need `nonce`-based script tags or `unsafe-inline`, which is a bigger lift than the other items here.
**Proposed fix (partial, "good enough" version):** add a baseline CSP (`default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'`) as a second layer of defense — won't fully lock down inline scripts without a larger template refactor, but blocks a large class of external-resource-loading attacks even if a script injection ever did land. Flagging this as a judgment call rather than a clear-cut fix — let me know if you want the fuller nonce-based version instead, which is more work but actually restrictive on inline scripts.

---

## Phase 5 — SQL & Data Integrity

Swept every `f"...{var}..."` SQL string in `app.py`/`logic.py` (11 sites) for whether the interpolated fragment is ever attacker-influenced. All are either code-owned literals (a hardcoded `(label, table_name)` list, never user input) or `WHERE`/`JOIN` fragments built from fixed condition strings with actual values still passed as bound parameters. **No SQL injection found**, matching IQ's own conclusion.

### 5.1 — Unvalidated date filter reaches a LIKE pattern
**Severity: Low.** `pos_history()`'s date filter builds `date_filter + "%"` into `LIKE ?` without first validating it's a real date (unlike `/refunds` and `/visits`, which validate first). Not an injection (still bound), but a malformed value silently matches nothing (or unexpectedly widely with embedded `%`/`_`) instead of showing a "not a valid date" message.
**Proposed fix:** run it through the same `clean_date`-based validation the other list pages already use.

### 5.2 — Placeholder-translation regex comment overstates its own safety
**Severity: Info.** `db.py`'s comment says the `?`→`%s` translator is quote-aware; the regex actually matches every bare `?` unconditionally. Harmless today (no SQL string in the codebase contains a literal `?`), but the comment would mislead someone adding a `LIKE '...?...'` pattern later.
**Proposed fix:** correct the comment to state plainly that it's a blind substitution, safe only because no current query needs a literal `?`.

---

## Phase 6 — Robustness (crash-prone inputs)

### 6.1 — Huge `page` query parameter → unhandled 500
**Severity: Low.** `get_page()` clamps `page >= 1` but never bounds the upper end; a huge value (e.g. `?page=99999999999999999999`) overflows Postgres's integer range when it reaches `OFFSET`, surfacing as a raw error page rather than a friendly message.
**Proposed fix:** clamp to a sane maximum (e.g. 100,000) in `get_page()`.

### 6.2 — Null byte in a text ID parameter → raw 500
**Severity: Low.** A literal `%00` in a path segment (e.g. `/owners/OW001%00`) isn't rejected before reaching Postgres, which raises on the embedded null byte — surfaces as an unhandled exception rather than a clean 400.
**Proposed fix:** reject any path/query parameter containing `\x00` early, in a shared spot (e.g. a `before_request` check), returning 400 instead of letting it reach the database layer.

---

## Phase 7 — Dead Code (re-verification)

Re-ran the same decorator-aware AST sweep from the earlier cleanup pass, after all of this session's edits. **Confirmed clean** — the only two remaining "zero external references" hits are the ones already deliberately kept last time (`auth.no_vet_role_configured`, a half-built safety feature worth wiring up rather than deleting; `jobs.take_result`, shared infra for a future consumer). No new dead code, no unused imports, introduced by any of today's fixes.

---

## Summary — what needs your decision

| # | Finding | Severity | Recommend |
|---|---|---|---|
| 2.1 | Sliding lockout → permanent DoS | Medium-High | Fix |
| 2.2 | Password change/reset doesn't kill other sessions | Medium | Fix |
| 2.3 | No `session.clear()` at login | Low | Fix (trivial, no downside) |
| 2.4 | Forced-password-change traps Logout | Low | Fix (trivial, no downside) |
| 2.5 | Default creds documented | Medium, by design | No action (working as intended) |
| 2.6 | In-process rate limiter | Info | No action |
| 3.1 | Backup dumps not chmod'd 0600 | Low-Medium | Fix |
| 3.2 | Updater extractall without member filtering | Low-Medium | Fix |
| 3.3 / 3.4 | Folder-browser / restore | N/A | Doesn't apply — no action |
| 4.1 | No CSP header | Low | Your call — baseline CSP vs. fuller nonce-based version vs. skip |
| 5.1 | `pos_history` date filter unvalidated | Low | Fix |
| 5.2 | Misleading regex comment | Info | Fix (one-line, no functional change) |
| 6.1 | Huge `page` param → 500 | Low | Fix |
| 6.2 | Null byte in ID → 500 | Low | Fix |
| 7 | Dead code | — | Already clean, no action |

Reply with which numbers to accept (or just say "accept all" / "accept all except X") and I'll apply them, verify live the same way as the last round, and ship as one release.
