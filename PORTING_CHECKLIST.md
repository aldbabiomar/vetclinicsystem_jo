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

1. **RBAC migration** ✅ DONE (`ce073e5`, simplified in `8fc1824`) — replaced
   `users.role TEXT CHECK (...'Admin','Vet','Reception')` /
   `auth.roles_required(*allowed_roles)` with VetClinicSystem_IQ's
   `roles`/`permissions`/`role_permissions` model: `users.role_id` FK,
   per-role `discount_cap` + per-user `custom_discount_cap` override,
   `is_vet_role` flag. All 22 admin-only routes now use
   `permission_required(...)`. Also fixed two bugs surfaced while doing
   this: `log_change()` no longer self-commits (all 49 call sites
   reordered — same fix as Phase B-1 below, pulled forward), and the
   Dashboard/nav's literal `role == 'Admin'` checks became real permission
   checks (same fix as the 1.4.6 item in Phase C below, pulled forward).
   No migration scripts were needed or added — this is a predeployment
   codebase, so the new columns are just declared directly in
   `schema_postgres.sql`. Verified end-to-end against a real local
   Postgres (fresh install, admin login, forced password change, Vet-role
   login with correctly restricted nav + a live 403 on an admin route,
   Settings discount-cap edit round-trip, audit log entries landing).
2. **Connection pooling** ✅ DONE (`8fc1824`) — `db.py`/`app.py` now use a
   bounded `psycopg_pool.ConnectionPool` instead of one raw connection per
   request (High #6). Insights' 6 parallel report queries also switched
   from opening raw connections to borrowing/returning pooled ones (the
   Medium item this used to block). Verified live: pool serves real
   requests including the Insights/Retention pages under the
   ThreadPoolExecutor pattern.
3. **Visit billing line-item snapshotting** ✅ DONE — `billing.codes` (comma
   string, live-repriced) replaced with `visit_billing_lines` (price_id/
   name/category/quantity/unit_price/unit_cost, snapshotted at Save time),
   plus a cached `billing.total` / `inpatient_cases.total` kept in sync by
   `refresh_visit_billing_total()`/`refresh_inpatient_total()`. Also added
   `inpatient_billing.unit_price/unit_cost` and `sale_items.unit_cost`
   snapshots (previously live-joined from Price List/Inventory Catalog,
   the same retroactive-price-edit gap VetClinicSystem_IQ's High #12/13
   fixed) — `_revenue_and_cogs_by_month()`, `revenue_by_category()`, and
   `vet_performance()` all rewired to read the snapshots. `visit_detail.html`
   got the search+add-to-cart billing UI (reusing `pos.html`'s pattern);
   `inpatient_detail.html`'s existing checkbox-list UI was left as-is since
   it already posts the same price_id/qty shape the backend expects — a
   matching search+cart upgrade there is a cosmetic follow-up, not a
   correctness gap. `visit_discount_save()`/`visit_billing_save()` also
   picked up the race-safe UPSERT VetClinicSystem_IQ's 1.4.5 fix uses
   (pulled forward, same as B-1 in Phase A.1).
   No currency-rounding porting needed here — Jordan's `compute_bill_totals`
   correctly stayed plain `round(x, 2)`; VetClinicSystem_IQ's own
   `IQD CURRENCY ROUNDING PLAN.md` confirms the 250-denomination rounding is
   isolated to `money.py` and never belonged in this shared billing logic.
   Verified end-to-end against a real Postgres: manual cart→save→snapshot→
   total-cache round trip, discount application, payments, inpatient
   billing add/delete/discount, POS checkout's new `sale_items.unit_cost`,
   and Insights/Reports against a full 1.7M-row synthetic dataset
   (`generate_test_data.py`, itself updated for the new columns). That
   stress test caught and fixed two real bugs before they shipped: a
   missing index on `visit_billing_lines.visit_id` (a correlated-subquery
   backfill was taking 20+ minutes without it — same index also speeds up
   every live visit-detail page load) and a `pdf_export.py` display bug
   where a multi-quantity Automatic billing line would have shown as its
   unit price instead of its line total on the exported PDF.
4. **Distributor Ledger** ✅ DONE — `distributor_bills`/`distributor_bill_payments`
   tables, `distributor_ledger()`/`distributor_outstanding_totals()`/
   `distributor_payables_summary()` in logic.py, 6 new routes (detail, bill
   new/delete, payment new/delete, PDF export), `distributor_detail.html`,
   and `distributors.html` gained the Outstanding column + Ledger link +
   payables summary block. Also fixed `distributor_delete()` while touching
   it: it used to unconditionally `DELETE FROM distributors`, which would
   crash with a raw FK violation the moment any inventory item OR bill was
   linked (the inventory-item case was already a live pre-existing bug,
   not something this phase introduced) — now checks both and gives a
   clean error naming what's still linked, matching the 1.0.0 changelog
   item for this same function.
   Verified end-to-end against a real Postgres: create distributor → log
   bill → record payment → overpayment correctly rejected → outstanding
   total correct on both the distributor row and the "Who You Owe Most"
   table → PDF export produces a valid PDF → delete guards correctly block
   while dependents exist and correctly allow once cleared, in order
   (payment → bill → distributor).
5. **Consignment** ✅ DONE — `inventory_list.ownership_type`/`consignment_since`,
   4 new tables (`consignment_receipts`/`shrinkage`/`returns`/`settlements`),
   13 routes (overview, items + bulk-edit, receiving + new, shrinkage + new,
   returns + new, sales-by-distributor, settlements + new + PDF export),
   7 templates, a Consignment nav group (gated on the 4 permissions already
   seeded in Phase A.1), and "consignment" badges on Inventory Status and
   the Audit Session view. Adapted two Iraq-only pieces while porting:
   `money.round_to_denomination()` on settlement `amount_paid` → plain
   `round(x, 2)`, and `restocked=true`/boolean columns → Jordan's `=1`/
   INTEGER convention throughout the new tables. The original build used
   plain float with no Decimal dependency, so no other money-model rewrite
   was needed. Skipped as lower-priority polish, not correctness: the
   collapsible nav-group JS/CSS VetIQ added around the same time (Jordan's
   nav groups stay simple/always-expanded, matching every other group),
   and the non-blocking "shortfall on audit confirm" nudge message.
   Verified end-to-end against a real Postgres: flag an item Consignment
   via bulk-edit, receive stock (confirmed it appears on Inventory
   Status), sell some via POS (confirmed `unit_cost` snapshot on
   `sale_items`), balance/shelf-value/shelf-units all computed correctly
   on the Overview page, shrinkage write-off and a return both correctly
   adjusted the balance and shelf count, a partial settlement correctly
   carried its residual into the next balance calculation, PDF export
   produced a valid file, the Sales-by-Distributor report matched the
   POS sale exactly, the item-lock guard held even against a raw POST
   bypassing the UI, and shrinkage/returns were correctly rejected for an
   item that's never been through a confirmed audit.
6. **Cash Register** ✅ DONE — new feature unifying POS sales, Visit/
   Inpatient/Boarding payments, and refunds (netted negative) into one
   end-of-day reconciliation page. `cash_register_payouts`/
   `cash_register_audits` tables, `refunds.refund_method` (confirmed
   Jordan's `refunds` table had no method column — added it, and wired a
   Method dropdown + column into both refund forms and the refunds
   list), `logic.cash_register_ledger/totals/payouts_for_day/
   latest_audit/last_30_days()`, 3 routes (page, payout, audit), a Cash
   Register nav link, and a "Cash Register Health" section on Insights
   (last 30 days' audit status, added as one more entry in the existing
   pooled-connection `jobs` dict from Phase A.2 rather than adopting
   VetIQ's separate `_render_with_progress`/`as_completed` job-runner,
   which Jordan has no other use for).
   Verified end-to-end against a real Postgres: a POS sale and a payout
   both correctly netted into the day's Cash total, a Perfect-status
   audit recorded and reflected on both the Cash Register page and
   Insights' health table, and a retail refund with a Cash method
   correctly subtracted from the same day's Cash total (50 sale − 10
   payout − 25 refund = 15, matching exactly).
7. **In-app updater** — Jordan has no `autostart.py`, `reconcile_attachments.py`,
   `VERSION` file, or versioned-release layout. This is a separate product
   decision (does Jordan even want GitHub-release-based auto-update?), not a
   pure bugfix port — flag to the user before investing in it rather than
   assuming it's wanted.

---

## Phase B — Security/correctness fixes (independently portable, in changelog order)

| # | Fix (from `CHANGELOG_SECURITY_FIXES.md`) | Verdict | Note |
|---|---|---|---|
| B-1 | High #17 — audit log atomic with its mutation (`log_change` no longer self-commits; reordered 56 call sites) | ✅ **DONE** (`ce073e5`) | Pulled forward during Phase A.1 since it required touching auth.py anyway. All 49 call sites in `app.py` reordered; one (`inpatient_billing_add`) was a genuine atomicity gap (logged strictly after its commit), not just out of order. |
| B-2 | Critical #1 — POS checkout oversell race (`SELECT ... FOR UPDATE`, sorted lock order) | ✅ **DONE** | Also picked up two related fixes bundled in the same upstream commit: duplicate cart lines for the same item are now merged before the stock check (previously two lines of 3 each could both pass against a stock of 5), and a never-audited item is now a hard block instead of an unlimited-oversell gap. The Cash Received/Change Due feature from the same upstream function was deliberately left out — separate scope (its own schema columns, still a Phase C item), and its 250-rounding logic wasn't wanted anyway. Verified live: the exact duplicate-line scenario (3+3 vs. stock of 5) is now rejected; a normal merged sale (2+2=4 vs. stock of 5) still succeeds. |
| B-3 | Critical #3 — restore endpoint path validation | **PORT [unverified]** | Jordan's `backup.py` is 143 lines vs VetClinicSystem_IQ's 467 — check whether `resolve_restorable_backup()`-equivalent exists before assuming the hole is open, but the size gap strongly suggests it isn't there yet. |
| B-4 | High #9 — attachment upload/delete file↔DB ordering | **PORT [unverified]** | Jordan's `attachments.py` is ~3KB vs a meaningfully larger file upstream; check current ordering before porting. |
| B-5 | High #16 + Medium — missing FKs (`payments.*`, `attachments.*`, `inpatient_cases.visit_id`, `price_list.linked_item_id`) + `audit_session_lines` unique constraint | ✅ **DONE** | Added inline in `CREATE TABLE` (predeployment, no migration/backfill needed). Two tables (`payments`, `price_list`) had to be moved later in `schema_postgres.sql` — they originally preceded the tables their new FKs reference (`boarding_sessions`/`inpatient_cases`, `inventory_list`), which would have failed on a fresh apply since `db.run_script()` executes statements in file order. Also updated `_save_audit_lines()` to `INSERT ... ON CONFLICT (session_id, item_id) DO UPDATE`, matching the new unique constraint (closes the same check-then-insert race the constraint exists to prevent). Verified live: all FKs and the unique constraint show up in `pg_constraint` after a fresh schema apply; saving the same audit session line twice updates in place instead of erroring; an attachment upload with the new FKs in place succeeds. |
| B-6 | Medium — Python-side pagination → SQL pagination (Follow-ups, Wellness, Grooming, Audit History) | **PORT [unverified]** | Jordan has `followups_list.html`/`grooming_list.html` — confirm current implementation is Python-side before porting the `..._page()` split pattern. |
| B-7 | High #7 + Medium — `0.0.0.0` bind hardening, session cookie policy, per-IP login rate limit | ✅ **DONE** | Jordan already had security headers + MAX_CONTENT_LENGTH from an earlier shared baseline — only `BEHIND_TLS_PROXY`/ProxyFix, explicit cookie policy, `PERMANENT_SESSION_LIFETIME`, the network allowlist, and per-IP login rate limiting were actually missing. Also made bind host/port configurable (`JRC_HOST`/`JRC_PORT`, previously hardcoded), which surfaced a real bug: two templates displayed a hardcoded `:5050` for the LAN address — fixed via a `bind_port` Jinja global. Verified live: session cookie now carries `HttpOnly`/`SameSite=Lax`/a 12h `Expires`, 22 rapid login attempts trip the per-IP rate limit on schedule, and `JRC_ALLOWED_NETWORKS` correctly 403s a non-matching request. |
| B-8 | High #12/#13 — billing snapshot + invalid-code rejection | ✅ **DONE** (billing-snapshot half) via Phase A.3 | The "invalid-code rejection" half is moot in Jordan: the search+cart UI only ever adds a real Price List match, so there's no free-typed code to reject in the first place. |
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
| 1.2.0 | Consignment bulk-edit UI, loading screen | ✅ **DONE** via A.5 | The Consignment Overview page's own loading-shell (VetIQ's `_render_with_progress`) wasn't ported — Jordan has no such background-job infrastructure elsewhere, and the page renders directly (fast enough at Jordan's scale: O(distributors), not O(catalog)). |
| 1.2.0 | Consignment-lock-on-any-past-sale bug fix | ✅ **DONE** via A.5 | Ported the already-fixed `consignment_item_locked()` directly — the bug (any historical sale permanently locking an item) never existed in what shipped to Jordan. |
| 1.2.1 | Consignment balance floor by `consignment_since` | ✅ **DONE** via A.5 | Ported directly — `consignment_balance()` already floors its scan at `inventory_list.consignment_since`. |
| 1.3.0 | Bulk Barcode Print | **PORT** | No currency/RBAC dependency beyond an existing permission. |
| 1.3.0 | Sales History end-of-day tally | **SKIP** | Superseded by Cash Register (A.6) one version later upstream — port Cash Register instead of this intermediate step. |
| 1.3.0 | Pagination on Consignment pages | ✅ **DONE** via A.5 | Every Consignment list route ported with SQL pagination already in place. |
| 1.3.0 | Barcode label box overflow fix | **PORT** | Confirmed Jordan has `barcode_label.html`/`barcode-render.js`-equivalent via `barcode.py`; check current CSS width. |
| 1.3.0 | Refund routes: 250-rounded amount saved but un-rounded shown | **SKIP** | Iraq-only rounding bug. |
| 1.3.0 | Payment-audit logged against parent id not payment id | **PORT [unverified]** | |
| 1.3.0 | 7 audit-logging gaps closed (attachments, password change, audit-session start, boarding incidents, appointment book/cancel) | **PORT** | Straightforward — sweep Jordan's equivalent routes for the same missing `log_change()` calls. |
| 1.4.0 | Cash Register feature | ✅ **DONE** via A.6 | |
| 1.4.0 | "Bank Transfer"/"Transfer" label unification | **PORT [unverified]** | Check Jordan's payment-method values first. |
| 1.4.0 | 4 delete routes logging "delete" even when nothing deleted | **PORT [unverified]** | |
| 1.4.1 | Sticky table headers | **PORT** | Pure CSS. |
| 1.4.1 | Payment-method dropdown instead of free text (distributor/consignment payments) | ✅ **DONE** (both halves, via A.4 + A.5) | Ported directly with the dropdown already in place on both — the free-text version this changelog entry replaced was never built in Jordan. |
| 1.4.1 | Grooming badge wrap fix, Cash Register note margin fix | **PORT (Grooming part only)** | Cash Register was built clean directly via A.6 — the margin bug this fixed never existed in what shipped to Jordan. |
| 1.4.2 | Malformed date-filter crash guard (Visits/Refunds/Cash Register) | ✅ **DONE** (Cash Register part, via A.6) / **[unverified]** (Visits/Refunds part) | `cash_register_page()` ported with the guard already in place. Visits/Refunds still need checking. |
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
| 1.4.4 | Distributor bill payment overpay + double-submit race | ✅ **DONE** via A.4 | Ported directly with `SELECT ... FOR UPDATE` locking + the balance check already in place — verified live that an overpayment attempt is rejected. |
| 1.4.4 | Consignment settlement double-submit race | ✅ **DONE** via A.5 | Ported directly with the `SELECT ... FOR UPDATE` mutex on the distributor row already in place. |
| 1.4.4 | Price List row linked to already-linked inventory item | **PORT [unverified]** | |
| 1.4.4 | Phone number validation (short input, mis-normalized foreign number) | **PORT [unverified]** | Check Jordan's phone validator — this one may already differ since Jordan phone formats (Jordan +962) vs Iraq (+964) aren't interchangeable; port the *validation logic pattern*, not the literal country code. |
| 1.4.5 | Refunding a discounted POS sale refunded pre-discount price | **PORT — high priority** | Real money-correctness bug, currency-model independent. Check Jordan's checkout/refund code for the same "discount only applied to total, not snapshotted per-item" pattern. |
| 1.4.5 | Discount not re-checked against items added after discount applied | **PORT [unverified]** | |
| 1.4.5 | Visit billing/discount first-save race | **PORT [unverified]** | |
| 1.4.5 | 3 lookup endpoints missing permission checks | **BLOCKED** on A.1 | Needs the permission model to exist first, but flag now: audit Jordan's equivalent lookup endpoints for the same missing check even under the old role system. |
| 1.4.5 | Backup/restore/update mutual exclusion | **PORT [unverified]**, restore/backup part only — update part depends on A.7 | |
| 1.4.5 | 5 routes crash on nonexistent record id | **PORT [unverified]** | |
| 1.4.5 | Operating Costs month not validated server-side | **PORT [unverified]** | |
| 1.4.6 | Dashboard panels gated on literal role name instead of permission | ✅ **DONE** (`ce073e5`) | Pulled forward during Phase A.1. Dashboard's Missed Items/opex/backup-alert panels and the sidebar's Sales & Billing/Admin nav groups now check real permissions (`is_overseer`, `has_permission(...)`) instead of `current_role == 'Admin'`. |
| 1.4.6 | Full History omits boarding stays | **PORT [unverified]** | |
| 1.4.6 | Grooming booking doesn't blank resource ID (double-booking bug); vet/time-slot validation | **PORT — security/correctness priority** | |
| 1.4.6 | Boarding negative price/total guard | **PORT [unverified]** | |
| 1.4.6 | Boarding payment cap + lock + no delete/correct path | **PORT [unverified]** | |
| 1.4.6 | Barcode duplicate-race handling (2 more routes) | **PORT [unverified]** | Depends on whether B-2's `FOR UPDATE` pattern was already established elsewhere in Jordan to extend from. |
| 1.4.6 | Login timing side-channel (constant-time regardless of username existing) | ✅ **DONE** via B-7 | Pulled forward while touching `login()` for the rate limiter anyway — `verify_password()` now runs unconditionally against a dummy hash when the username doesn't exist. |

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
