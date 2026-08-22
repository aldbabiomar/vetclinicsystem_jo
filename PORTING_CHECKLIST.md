# Sync checklist: Jordan Referral Center → VetClinicSystem_IQ parity

Generated 2026-08-22 by diffing `schema_postgres.sql` (table-by-table) and
`app.py`/`logic.py`/`auth.py`/`db.py` against VetClinicSystem_IQ, plus reading
`CHANGELOG.md` and `CHANGELOG_SECURITY_FIXES.md` top to bottom. Verdicts are
based on either (a) direct code inspection of this repo, noted inline, or
(b) the changelog's own description where inspection wasn't done yet — those
are marked **[unverified]** and should be confirmed at port time, not assumed.

Legend: **PORT** = apply as-is · **ADAPT** = apply, but rewrite for Jordan's
money model or missing infra first · **SKIP** = Iraq-only or branding-only ·
**BLOCKED** = depends on a Phase A item landing first.

Currency-model correction vs. earlier assumption: neither app uses `Decimal`.
Both use plain `float` (VetClinicSystem_IQ's own security changelog flagged
float→Decimal as "deliberately not done"). The only real currency-only
divergence is the 250-IQD note-rounding logic in `money.py` and its price
warnings — everything else below is a normal feature/fix gap, not a currency
gap.

---

## Phase A — Foundational infrastructure (land these first; most of Phase B/C depends on them)

Confirmed by direct schema/code diff, not changelog description alone.

1. **RBAC migration** — Jordan: `users.role TEXT CHECK (...'Admin','Vet','Reception')`,
   `auth.roles_required(*allowed_roles)` checking literal role names.
   VetClinicSystem_IQ: `roles`/`permissions`/`role_permissions` tables,
   `users.role_id` FK, per-role `discount_cap` + per-user `custom_discount_cap`
   override, `is_vet_role` flag (replaces hardcoded "Vet" name checks in vet
   pickers). **This blocks nearly every permission-gated fix/feature below** —
   do it first, even though it's the most invasive single change.
2. **Connection pooling** (`db.py`, `app.py`) — Jordan has no pool; every
   request opens a raw `psycopg.Connection`. VetClinicSystem_IQ added
   `psycopg_pool.ConnectionPool` (High #6). Needed before the Insights
   parallel-connection fix (item 9 below) makes sense to port.
3. **Visit billing line-item snapshotting** (`visit_billing_lines` table) —
   Jordan still stores `billing.codes` as a comma-separated string, re-priced
   live from Price List on every read (confirmed: no `visit_billing_lines` in
   Jordan's schema). VetClinicSystem_IQ replaced this with a real line-items
   table snapshotted at save time (`logic.price_codes_or_none()`,
   `_revenue_and_cogs_by_month()` reading snapshots). Needed before the
   Billing Redesign UI (item C-1.x) and the revenue/COGS-by-month report fix
   (item B-6) can be ported meaningfully.
4. **Distributor Ledger** — new feature, self-contained (`distributor_bills`,
   `distributor_bill_payments`, `distributor_ledger()`, 6 new routes, PDF
   export). No currency-model dependency; port as designed.
5. **Consignment** — new feature (4 new tables, 16 routes, 7 templates). The
   original build explicitly used plain `float` + `round(x, 2)`, "no
   dependency on Decimal/NUMERIC migration" — directly portable to Jordan
   with no money-model rewrite needed. Depends on Phase A.1 (RBAC) for its
   4 new permissions.
6. **Cash Register** — new feature unifying POS/visit/inpatient/boarding
   payments + refunds into one end-of-day reconciliation page. Depends on
   Phase A.1 (new `manage_cash_register` permission) and requires Refunds to
   record payment method (check whether Jordan's `refunds` table already has
   a method column — if not, that's part of this port too).
7. **In-app updater** — Jordan has no `autostart.py`, `reconcile_attachments.py`,
   `VERSION` file, or versioned-release layout. This is a separate product
   decision (does Jordan even want GitHub-release-based auto-update?), not a
   pure bugfix port — flag to the user before investing in it rather than
   assuming it's wanted.

---

## Phase B — Security/correctness fixes (independently portable, in changelog order)

| # | Fix (from `CHANGELOG_SECURITY_FIXES.md`) | Verdict | Note |
|---|---|---|---|
| B-1 | High #17 — audit log atomic with its mutation (`log_change` no longer self-commits; reordered 56 call sites) | **PORT** | Confirmed Jordan's `auth.log_change()` still calls `db.commit()` itself (`auth.py:198`) — same architectural gap exists. Every route calling it needs the same reorder. |
| B-2 | Critical #1 — POS checkout oversell race (`SELECT ... FOR UPDATE`, sorted lock order) | **PORT** | Confirmed: `grep -n "FOR UPDATE"` on Jordan's `app.py`/`logic.py` returns nothing. Race is live in Jordan's POS today. |
| B-3 | Critical #3 — restore endpoint path validation | **PORT [unverified]** | Jordan's `backup.py` is 143 lines vs VetClinicSystem_IQ's 467 — check whether `resolve_restorable_backup()`-equivalent exists before assuming the hole is open, but the size gap strongly suggests it isn't there yet. |
| B-4 | High #9 — attachment upload/delete file↔DB ordering | **PORT [unverified]** | Jordan's `attachments.py` is ~3KB vs a meaningfully larger file upstream; check current ordering before porting. |
| B-5 | High #16 + Medium — missing FKs (`payments.*`, `attachments.*`, `inpatient_cases.visit_id`, `price_list.linked_item_id`) + `audit_session_lines` unique constraint | **PORT** | Jordan has no data yet in most installs — can add these FKs inline in `CREATE TABLE`, no migration/backfill scaffolding needed (same simplification VetClinicSystem_IQ itself did once it confirmed no prod data). |
| B-6 | Medium — Python-side pagination → SQL pagination (Follow-ups, Wellness, Grooming, Audit History) | **PORT [unverified]** | Jordan has `followups_list.html`/`grooming_list.html` — confirm current implementation is Python-side before porting the `..._page()` split pattern. |
| B-7 | High #7 + Medium — `0.0.0.0` bind hardening, session cookie policy, per-IP login rate limit | **PORT** | Generic Flask/Waitress hardening, opt-in via env vars, zero currency dependency. Safe to port as-is. |
| B-8 | High #12/#13 — billing snapshot + invalid-code rejection | **BLOCKED** on A.3 | Same underlying table. |
| B-9 | High #8 — float→Decimal/NUMERIC | **SKIP (for now)** | VetClinicSystem_IQ itself deliberately never did this ("largest and riskiest item... not implemented this session"). Don't chase parity on a fix the source app also skipped. |

---

## Phase C — Feature/bugfix items from `CHANGELOG.md` (v1.0.0 → v1.4.6)

| Version | Item | Verdict | Note |
|---|---|---|---|
| 1.0.0 | In-app update checker | **SKIP/decide separately** | See Phase A.7. |
| 1.0.0 | Cash Received / Change Due on POS checkout | **PORT** | Pure UI/logic, currency-agnostic. |
| 1.0.0 | "Not a multiple of 250 IQD" price warning | **SKIP** | Iraq-only denomination concept. |
| 1.0.0 | Round bill totals/balances/refunds to nearest 250 IQD | **SKIP** | Iraq-only (`money.py`). Do not introduce JOD-note rounding. |
| 1.0.0 | Reports read the same rounded total shown on receipt | **ADAPT** | The *principle* (reports must agree with receipts, not recompute) is worth keeping — just without 250-rounding. |
| 1.0.0 | Boolean columns instead of 0/1 ints | **PORT** | Confirmed Jordan still uses `INTEGER NOT NULL DEFAULT 1` for `active` (see `users` table) — same cleanup applies. |
| 1.0.0 | Retail refunds linked to original sale, over-refund blocked | **PORT [unverified]** | Check Jordan's `refund_items`/`sale_items` linkage first. |
| 1.0.0 | Boarding totals computed live (price/day × nights so far) | **PORT [unverified]** | Check Jordan's `boarding_sessions` billing logic. |
| 1.0.0 | Optimistic-locking warning on concurrent edits (visit/boarding/inpatient) | **PORT [unverified]** | |
| 1.0.0 | Min password length 6→8 | **PORT** | Trivial. |
| 1.0.0 | Negative-value guards (price/weight/unit cost), fractional BCS rejection, `distributor_delete()` explains linkage | **PORT** | Generic input validation, no currency dependency. |
| 1.0.0 | `schema_postgres.sql` fails on genuinely empty DB | **PORT [unverified]** | Test against a fresh empty Jordan DB. |
| 1.0.0 | `parse_money()` accepted NaN/Infinity | **PORT** | Confirmed Jordan's `parse_money()` (`app.py:116`) does `float(raw)` with no NaN/Infinity guard — same bug is live in Jordan today. |
| 1.1.0 | In-app updates on by default / versioned layout | **SKIP** | Depends on Phase A.7 decision. |
| 1.1.1 / 1.1.2 | Settings spacing fix, native confirm→styled dialog | **PORT** | Trivial CSS/JS. |
| 1.2.0 | Consignment bulk-edit UI, loading screen | **BLOCKED** on A.5 | |
| 1.2.0 | Consignment-lock-on-any-past-sale bug fix | **BLOCKED** on A.5 | |
| 1.2.1 | Consignment balance floor by `consignment_since` | **BLOCKED** on A.5 | |
| 1.3.0 | Bulk Barcode Print | **PORT** | No currency/RBAC dependency beyond an existing permission. |
| 1.3.0 | Sales History end-of-day tally | **SKIP** | Superseded by Cash Register (A.6) one version later upstream — port Cash Register instead of this intermediate step. |
| 1.3.0 | Pagination on Consignment pages | **BLOCKED** on A.5 | |
| 1.3.0 | Barcode label box overflow fix | **PORT** | Confirmed Jordan has `barcode_label.html`/`barcode-render.js`-equivalent via `barcode.py`; check current CSS width. |
| 1.3.0 | Refund routes: 250-rounded amount saved but un-rounded shown | **SKIP** | Iraq-only rounding bug. |
| 1.3.0 | Payment-audit logged against parent id not payment id | **PORT [unverified]** | |
| 1.3.0 | 7 audit-logging gaps closed (attachments, password change, audit-session start, boarding incidents, appointment book/cancel) | **PORT** | Straightforward — sweep Jordan's equivalent routes for the same missing `log_change()` calls. |
| 1.4.0 | Cash Register feature | **BLOCKED** on A.6 | |
| 1.4.0 | "Bank Transfer"/"Transfer" label unification | **PORT [unverified]** | Check Jordan's payment-method values first. |
| 1.4.0 | 4 delete routes logging "delete" even when nothing deleted | **PORT [unverified]** | |
| 1.4.1 | Sticky table headers | **PORT** | Pure CSS. |
| 1.4.1 | Payment-method dropdown instead of free text (distributor/consignment payments) | **BLOCKED** on A.4 (distributor ledger doesn't exist yet in Jordan) |
| 1.4.1 | Grooming badge wrap fix, Cash Register note margin fix | **PORT (Grooming part only)** | Cash Register part blocked on A.6. |
| 1.4.2 | Malformed date-filter crash guard (Visits/Refunds/Cash Register) | **PORT [unverified]** | Cash Register part blocked on A.6; Visits/Refunds part portable now. |
| 1.4.2 | 6 PDF export crash-on-missing-record fixes + billing PDF number formatting | **PORT [unverified]** | Check Jordan's `pdf_export.py` for the same unguarded lookups. |
| 1.4.2 | Boarding stay locked after pickup (dates/price/total) | **PORT [unverified]** | |
| 1.4.2 | Restore re-applies schema sync | **PORT [unverified]** | Related to B-3; do together. |
| 1.4.2 | Updater release-pointer atomic write | **SKIP** | Depends on A.7. |
| 1.4.2 | Follow-ups/Wellness sort order (newest first) | **PORT [unverified]** | |
| 1.4.2 | Newly-enabled grooming request gets "Waiting" status | **PORT [unverified]** | |
| 1.4.2 | Follow-Ups dropdown missing "N/A" | **PORT [unverified]** | |
| 1.4.2 | Dashboard "Missed Items" header spacing | **PORT** | Trivial CSS. |
| 1.4.2 | Boarding list N+1 query batching | **PORT [unverified]** | |
| 1.4.3 | Appointments "need attention" fallback list + vet-deactivation warning | **PORT [unverified]** | Genuinely useful correctness feature, no blockers. |
| 1.4.3 | Inpatient "Balance Due" filter | **PORT [unverified]** | |
| 1.4.4 | Inventory Status double-counting same-day sales (audit-timestamp vs audit-date bug) | **PORT — high priority** | This one is described as "silently wrong since the feature shipped" with cascading effects (false LOW STOCK, wrong Ordering Sheet, false shortfall warnings). Check Jordan's audit-confirmation logic for the same date-vs-timestamp comparison bug regardless of consignment status. |
| 1.4.4 | Boarding pickup not updating monthly P&L cache | **PORT [unverified]** | |
| 1.4.4 | Audit-confirm re-running inventory status per item instead of once | **PORT [unverified]** | Performance fix, portable regardless of consignment. |
| 1.4.4 | Distributor bill payment overpay + double-submit race | **BLOCKED** on A.4 | |
| 1.4.4 | Consignment settlement double-submit race | **BLOCKED** on A.5 | |
| 1.4.4 | Price List row linked to already-linked inventory item | **PORT [unverified]** | |
| 1.4.4 | Phone number validation (short input, mis-normalized foreign number) | **PORT [unverified]** | Check Jordan's phone validator — this one may already differ since Jordan phone formats (Jordan +962) vs Iraq (+964) aren't interchangeable; port the *validation logic pattern*, not the literal country code. |
| 1.4.5 | Refunding a discounted POS sale refunded pre-discount price | **PORT — high priority** | Real money-correctness bug, currency-model independent. Check Jordan's checkout/refund code for the same "discount only applied to total, not snapshotted per-item" pattern. |
| 1.4.5 | Discount not re-checked against items added after discount applied | **PORT [unverified]** | |
| 1.4.5 | Visit billing/discount first-save race | **PORT [unverified]** | |
| 1.4.5 | 3 lookup endpoints missing permission checks | **BLOCKED** on A.1 | Needs the permission model to exist first, but flag now: audit Jordan's equivalent lookup endpoints for the same missing check even under the old role system. |
| 1.4.5 | Backup/restore/update mutual exclusion | **PORT [unverified]**, restore/backup part only — update part depends on A.7 | |
| 1.4.5 | 5 routes crash on nonexistent record id | **PORT [unverified]** | |
| 1.4.5 | Operating Costs month not validated server-side | **PORT [unverified]** | |
| 1.4.6 | Dashboard panels gated on literal role name instead of permission | **BLOCKED** on A.1 | This bug *is* the RBAC gap — will be moot once A.1 lands, since Jordan doesn't have permissions to check yet. |
| 1.4.6 | Full History omits boarding stays | **PORT [unverified]** | |
| 1.4.6 | Grooming booking doesn't blank resource ID (double-booking bug); vet/time-slot validation | **PORT — security/correctness priority** | |
| 1.4.6 | Boarding negative price/total guard | **PORT [unverified]** | |
| 1.4.6 | Boarding payment cap + lock + no delete/correct path | **PORT [unverified]** | |
| 1.4.6 | Barcode duplicate-race handling (2 more routes) | **PORT [unverified]** | Depends on whether B-2's `FOR UPDATE` pattern was already established elsewhere in Jordan to extend from. |
| 1.4.6 | Login timing side-channel (constant-time regardless of username existing) | **PORT — security priority** | Straightforward, no dependencies. |

---

## SKIP entirely (Iraq-only or branding-only, confirmed not applicable)

- `money.py` — `round_to_denomination()`, `SMALLEST_NOTE = 250`, `is_denomination_valid()`
- Every "multiple of 250 IQD" warning/validation
- ChamPet palette, VetClinicSystem_IQ branding, logo/favicon assets, rename-related commits (`chore: complete Phase 4`, `feat: ChamPet color palette...`)
- `GITHUB_REPO` pointer / release automation tied to the VetClinicSystem_IQ repo specifically

---

## Suggested execution order

1. Phase A.1 (RBAC) — unblocks the largest number of downstream items.
2. Phase B items with no blockers (B-1, B-2, B-7, B-5, B-9-skip) — pure security/correctness, no feature dependency.
3. Phase A.2 (pooling) + Phase A.3 (billing snapshot) — infra plumbing.
4. High-priority correctness bugs called out above (1.4.4 inventory double-count, 1.4.5 discount-refund leak, 1.4.6 grooming double-booking, login timing) — these read as "silently wrong money/security" bugs regardless of which infra phase they land in, so pull them forward if you want quick wins before the bigger RBAC lift.
5. Phase A.4/A.5/A.6 (Distributor Ledger, Consignment, Cash Register) as standalone feature projects, each with its own Phase B/C-blocked items following immediately after.
6. Remaining `[unverified]` rows — confirm against Jordan's current code at port time, one changelog entry at a time, committing each as its own git commit now that Jordan has real history (see initial commit `e0077b7`).
