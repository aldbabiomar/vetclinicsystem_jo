# VetClinicSystem JO — Migration Script, PostgreSQL, & Dead Code Audit

**Scope:** Full repository sweep ahead of a fresh deployment.
**Database:** PostgreSQL only (this codebase has no SQLite runtime path — see Task 2 for what "SQLite" actually means here).
**Method:** Manual review of `setup.py`/`db.py`/`schema_postgres.sql` for Task 1, cross-checked against `git log -S` to establish whether each migration entry ever corresponded to a real historical schema state. Automated AST-based static analysis (custom script: every top-level function/class/constant, decorator-aware so Flask-dispatched functions aren't false-flagged) for Task 3, with every hit manually verified against the actual call graph — including template-side Jinja usage — before being reported. Nothing below is a raw tool dump.

A companion audit exists for the sibling app, [VetClinicSystem_IQ's `MIGRATION_AND_DEADCODE_AUDIT.md`](../../../../Downloads/VetClinicSystem_IQ/MIGRATION_AND_DEADCODE_AUDIT.md) — same prompt, same methodology, run separately since the two codebases have diverged (Decimal vs. float money, different migration history, different in-app-updater maturity). The two reports reach an **opposite conclusion on Task 1** for a reason explained below — that's not an inconsistency, it's a real difference between the two apps' deployment history.

---

## Architecture note (read this first)

Like IQ, this codebase has no `migrations/0001_xxx.sql`-folder pattern. There is exactly **one** SQL file, plus **one** in-code list that acts as migration history:

| File | Role |
|---|---|
| [schema_postgres.sql](schema_postgres.sql) | Full, current schema — every `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` needed to build the database from nothing. What a fresh install actually runs first. |
| [setup.py](setup.py) `INCREMENTAL_SCHEMA_STATEMENTS` (lines 137–145) | An append-only list of idempotent `ALTER TABLE`/`CREATE INDEX` statements — one per schema change shipped *after* `schema_postgres.sql` last had that change baked in natively. |

**The key finding of this audit:** unlike IQ — where the equivalent list is confirmed *live infrastructure* still needed by real, already-deployed clinic installs updating forward — every single entry currently in **JO's** list is provably dead weight, and always has been. See below for the evidence.

---

## Task 1 — Migration Script Audit

### Essential (required for a fresh deployment)

| Item | Why essential |
|---|---|
| [schema_postgres.sql](schema_postgres.sql) | The base schema. `setup.apply_schema()` runs this file verbatim on every install. |
| [setup.py](setup.py) — `apply_schema()`, `migrate_or_seed()`, `main()` | The install/bootstrap driver: schema creation → migrations → seed-if-empty. |
| [auth.py](auth.py) `seed_default_roles_and_permissions()` | Called from `apply_schema()` — creates Admin/Vet/Reception roles and grants permissions. Without it a fresh DB has tables but no usable login. |
| [import_seed.py](import_seed.py) | Populates a genuinely empty database from `seed_data.json` — invoked automatically by `migrate_or_seed()` when `owners` is empty. |
| [db.py](db.py) `run_script()`, `next_id()`, `seed_counter()` | Support code the schema/seed step depends on. |
| [updater.py](updater.py), [jobs.py](jobs.py), `/health` (app.py) | Not migration scripts, but the in-app self-update mechanism that will run `apply_schema()` again on every future release — essential to keep, since this is how *future* schema changes reach a deployed clinic. |

### `INCREMENTAL_SCHEMA_STATEMENTS` (setup.py:137–145) — **all 6 current entries are safe to delete**

I checked each statement against `schema_postgres.sql`'s `CREATE TABLE` blocks, then walked `git log -S` on both the column and the list itself to see whether there was ever a point in this repo's history where the column *didn't* already exist natively:

| Statement | Already in `schema_postgres.sql`'s `CREATE TABLE`? | First commit with the column | First commit with the ALTER entry |
|---|---|---|---|
| `ALTER TABLE visits ADD COLUMN IF NOT EXISTS weight_kg DOUBLE PRECISION` | Yes — [schema_postgres.sql:373](schema_postgres.sql) | `e0077b7` (initial commit) | `e0077b7` (initial commit) |
| `ALTER TABLE visits ADD COLUMN IF NOT EXISTS bcs INTEGER CHECK (...)` | Yes — [schema_postgres.sql:374](schema_postgres.sql) | `e0077b7` | `e0077b7` |
| `ALTER TABLE inpatient_cases ADD COLUMN IF NOT EXISTS weight_kg DOUBLE PRECISION` | Yes — [schema_postgres.sql:515](schema_postgres.sql) | `e0077b7` | `e0077b7` |
| `ALTER TABLE inpatient_cases ADD COLUMN IF NOT EXISTS bcs INTEGER CHECK (...)` | Yes — [schema_postgres.sql:516](schema_postgres.sql) | `e0077b7` | `e0077b7` |
| `ALTER TABLE price_list ADD COLUMN IF NOT EXISTS can_discount BOOLEAN NOT NULL DEFAULT FALSE` | Yes — [schema_postgres.sql:234](schema_postgres.sql) | `e0077b7` | `e0077b7` |
| `ALTER TABLE payments ADD COLUMN IF NOT EXISTS boarding_id INTEGER` + its `CREATE INDEX` | Yes — [schema_postgres.sql:544,557](schema_postgres.sql) | `e0077b7` | `e0077b7` |

**Every entry was added in the exact same commit as the column it "migrates."** There is no earlier version of `schema_postgres.sql` in this repo's history that lacks any of these columns — meaning these six statements have been no-ops for every Jordan install that has ever existed, back to the very first commit. This is a materially different situation from IQ's list (confirmed in IQ's own audit as still-load-bearing for real clinic installs in the field): Jordan's entries were copy-pasted along with the rest of `setup.py`'s structure at fork time, from an IQ commit that legitimately needed them for *IQ's* history — but that history doesn't apply to Jordan, which started with these columns already present.

**Recommendation:**
- **Delete the 6 current list entries** (setup.py:138–144) — pure dead weight, zero deployed installs (Jordan is pre-launch) depend on them, and none ever did.
- **Keep the mechanism** — `apply_incremental_migrations()`, the empty list it iterates, and its call from `apply_schema()`/`updater.py`. This is the correct, already-proven pattern for handling Jordan's *own* future schema changes once it has real deployed installs (exactly how IQ's list started).

### Removable / not part of the deployed application

| Item | Why removable from a deployment package |
|---|---|
| [generate_test_data.py](generate_test_data.py) | A manually-invoked, ~500-line stress-test data generator ("used to validate the Insights/Retention BI tabs against ~1.7M rows"). Its own docstring warns *"Never run this against a real clinic's data."* Not imported or called by `setup.py`, `app.py`, `import_seed.py`, or `updater.py` — confirmed via repo-wide grep; the only other mention is a one-line comment in `db.py` citing it as an example of a standalone-connection caller. Unlike IQ's equivalent (`dev_seed/`), this file is **not** git-ignored — it's tracked and would ship with the app as-is. Functionally identical to IQ's `dev_seed/`: safe to exclude from a deployment package, though harmless to leave in the repo (it does nothing unless someone runs it deliberately, and it refuses to run against a database with a confirmation prompt unless `--confirm-wipe` is passed). |

No other files matched "migration script" in this codebase — no legacy SQLite `schema.sql`, no old `migrations/` directory, no orphaned one-off migration scripts.

---

## Task 2 — PostgreSQL vs. SQLite Audit

**Headline result: zero Critical/Breaking findings.** No `IFNULL`, no `INSERT OR REPLACE`/`INSERT OR IGNORE`, no `GROUP_CONCAT`, no `LAST_INSERT_ROWID`/`.lastrowid`, no `AUTOINCREMENT`, no `PRAGMA`. IDs use Postgres-native `GENERATED BY DEFAULT AS IDENTITY` (not the legacy `SERIAL`), and boolean columns are genuine `BOOLEAN` (the codebase was already swept for 0/1-int-against-boolean comparisons — none found).

### Benign Comments (documentation only, zero functional impact)

| Location | Finding |
|---|---|
| [logic.py:3](logic.py) | Module docstring: *"Pure computation over SQLite tables; no Flask imports."* Factually wrong today — this module operates exclusively on Postgres (`NUMERIC`, `BOOLEAN`, `IDENTITY` columns throughout). Stale since the SQLite→Postgres port; the "no Flask imports" half is still true. |
| [schema_postgres.sql:1–3](schema_postgres.sql) | Header comment: *"Ported from the original SQLite schema.sql... AUTOINCREMENT -> IDENTITY, and REAL -> DOUBLE PRECISION."* Historically accurate, not misleading — optional to prune as dev-history noise, not required. |
| [db.py:42](db.py) | `class Connection(psycopg.Connection): """psycopg Connection that accepts SQLite-style '?' placeholders."""` — accurate, intentional (see next section), not stale. No action needed. |

### Universal (works identically / correctly in both, no issue)

| Location | Finding |
|---|---|
| `generate_test_data.py`'s `random()`, `floor()`, `array_length()`, `generate_series`, `LATERAL` joins | These *look* like they might be flagged by a naive "SQLite-vs-Postgres" grep for `random`, but they're pure Postgres-native SQL (`random()` returns `0.0–1.0` float in Postgres vs. SQLite's `RANDOM()` returning a 64-bit signed int — a real behavioral difference between the two engines' *same-named* function, but this code was written directly against Postgres's semantics, not ported from a SQLite equivalent). No issue. |
| `sale_date LIKE ?`, `date_billed::text LIKE ?`, etc. (logic.py, app.py, import_seed.py) | `LIKE` behaves slightly differently in SQLite (case-insensitive for ASCII by default) vs. Postgres (always case-sensitive; use `ILIKE` for insensitivity) — but every usage here matches against ISO-8601 date/timestamp *prefixes* (e.g. `'2026-08%'`), which contain no letters, so the case-sensitivity difference is irrelevant. No issue. |

### Unoptimized (works in Postgres, but a better Postgres-native replacement exists)

| Location | Finding | Recommendation |
|---|---|---|
| [db.py](db.py) `_translate()` / `Connection.execute()` — the whole `?`→`%s` placeholder-translation layer | Every single query in the codebase is written with SQLite-style `?` placeholders and translated via a regex substitution on *every* `.execute()` call. This is a deliberate, well-documented design choice (keeps the codebase visually consistent after the SQLite→Postgres port) — not a bug — but it does add a small, avoidable regex pass to every database round-trip, and it's the one place SQLite's calling convention still shapes how the code is written, 100% of queries, permanently. | Not urgent — this is architecture, not a defect, and the overhead is negligible at this app's scale (single-clinic LAN traffic). Only worth revisiting if a future full rewrite of the data-access layer is already on the table for other reasons; not something to change opportunistically. |
| ~30+ timestamp columns typed `TEXT` instead of `TIMESTAMP` (e.g. `created_at`, `timestamp`, `logged_at`, `sale_date` — see [schema_postgres.sql](schema_postgres.sql) lines 40, 62, 90, 99, 108, 166, 180, 248, 282, 305, 324, 342, 360, 449, 489, 498, 532, 562, 572, 594, 608, 622, 684, 720, 738, 784, and others), storing `datetime.now().isoformat()` as plain text | Idiomatic in SQLite (no native timestamp type), an anti-pattern in Postgres: no type-level validation, no native date/time arithmetic without casting, and less efficient indexing/sorting than a real `TIMESTAMP` column. It happens to *work correctly* only because ISO-8601's fixed-width format sorts lexicographically identically to chronological order — a fragile-but-currently-safe coincidence, not a guarantee (e.g. it breaks the moment any row's timestamp is missing zero-padding or a component is a different length). | This is pervasive and consistent, not scattered — a real architectural choice inherited from the SQLite origin, applied deliberately across the whole schema. **Not recommended as an urgent fix**: converting ~30 columns to native `TIMESTAMP` touches every read/write of each (parsing, comparisons, `.isoformat()` calls throughout `app.py`/`logic.py`), a large and risky undertaking for something that isn't currently causing a bug. Flagging for awareness/future consideration only, same spirit as the Decimal/NUMERIC conversion already done for money — if it's ever done, do it as its own dedicated pass, not folded into unrelated work. |

### Critical/Breaking

**None found.** No SQLite-only syntax that would actually error out or silently misbehave under Postgres exists anywhere in this codebase.

---

## Task 3 — Dead Code Analysis

### Methodology

Custom AST walk over all 13 first-party modules (`app.py`, `logic.py`, `auth.py`, `db.py`, `backup.py`, `updater.py`, `jobs.py`, `attachments.py`, `barcode.py`, `pdf_export.py`, `import_seed.py`, `generate_test_data.py`, `setup.py`): every top-level function/class/constant, with a text-search across every `.py` and `.html` file in the repo (excluding the definition site itself) counting real references. Functions carrying a decorator (`@app.route`, `@app.before_request`, `@app.template_filter`, etc.) were excluded automatically — those are invoked by Flask/Jinja's dispatcher, which a name-reference search can't see, and manually spot-checking confirmed the exclusion was correct (e.g. `money_filter` is registered via `@app.template_filter("money")` and called from templates as `|money`, never by its Python name).

Everything below survived that filter and was then individually re-verified by reading its full definition and searching for indirect uses (Jinja globals registration, dynamic dispatch, sibling helpers that might duplicate its logic inline instead of calling it).

### Confirmed dead code (safe to delete)

| # | Location | What it is | Evidence |
|---|---|---|---|
| 1 | [auth.py:76](auth.py) `ROLES = ["Admin", "Vet", "Reception"]` | Hardcoded legacy role-name list. | Zero references anywhere else. Its own comment says it's kept "only for places... that still refer to a role by its seeded name" — no such place exists; the live source of truth is the `roles` table. Same finding as IQ's audit. |
| 2 | [auth.py:59](auth.py) `PERMISSION_CATEGORIES = [...]` | A static list of permission category names, presumably meant to drive UI grouping alongside `PERMISSIONS`. | Zero references anywhere, including templates. The actual permission checkbox rendering doesn't group by category at all currently — this list was never wired into anything. |
| 3 | [auth.py:158](auth.py) `is_system_admin()` | `"""True if the current session belongs to a locked (is_system) role."""` — same docstring style and shape as `has_permission()` immediately above it, which *is* registered as a Jinja global (`app.jinja_env.globals["has_permission"] = auth.has_permission`, app.py:498). | Zero references anywhere, including no matching `app.jinja_env.globals["is_system_admin"] = ...` registration. Looks like it was written to mirror `has_permission`'s pattern but never actually wired in. |
| 4 | [logic.py:70–71](logic.py) `audit_session_status_label(status)` | One-line `{"Draft": "Saved", "Confirmed": "Confirmed"}.get(status, status)` mapping. | Zero call sites. The exact same "Draft"→"Saved" mapping is duplicated inline as a Jinja ternary in [templates/audit_sessions_list.html:18](templates/audit_sessions_list.html) instead of calling this helper — the template never adopted it. |
| 5 | [logic.py:486–487](logic.py) `visit_total_bill(db, visit_id)` | One-line wrapper: `return visit_billing_summary(db, visit_id)["total"]`. | Zero call sites; the one other place this exact expression is needed (`refresh_visit_billing_total`, logic.py:482, immediately above it) inlines it directly rather than calling this wrapper. |
| 6 | [logic.py:1255–1256](logic.py) `sellable_items(db)` | Returns all active Retail inventory items. | Zero call sites. [app.py:1840](app.py) runs the near-identical query inline (`SELECT id, name, cost_price FROM inventory_list WHERE active=true AND category='Retail' ORDER BY name`) instead of calling this helper — superseded-in-place. |
| 7 | [import_seed.py:45](import_seed.py) `INPATIENT_CASE_STATUSES_CLOSED = {...}` | A set of "closed" inpatient case statuses. | Zero references anywhere. Its sibling `CASE_STATUS_MAP` three lines above *is* used — this one was apparently never wired up. Same finding as IQ's audit. |
| 8 | [pdf_export.py:12](pdf_export.py) `PageBreak` (unused import from `reportlab.platypus`) | Imported alongside `SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle` but never invoked anywhere in the module. | Only textual occurrence is the import line itself. |

**Action:** delete all 8. None are imported or referenced elsewhere; removal is a pure subtraction with no follow-up changes needed.

### Flagged but NOT recommended for blind deletion — read before acting

| Location | What it is | Why this one needs a decision, not just a delete |
|---|---|---|
| [auth.py:112–120](auth.py) `no_vet_role_configured(db)` | `"""True if zero roles are marked 'can be assigned as a vet'... Callers use this to surface a loud warning right when an admin's role edit/delete would cause it."""` | The docstring describes real, specific intended behavior ("callers use this to surface a loud warning") that was never actually implemented — there's no caller anywhere. This reads like a genuine safety guard-rail that was designed (an admin could otherwise delete/edit every vet-eligible role and silently break every vet picker across Appointments/New Visit/Grooming/Inpatient) but never connected to the role-edit/delete route. **Recommend deciding whether to wire this up** (add the check to wherever roles are edited/deleted in `app.py`) **rather than deleting it outright** — deleting removes a half-built safety feature, not just dead weight. |
| [jobs.py:110–118](jobs.py) `take_result(job_id)` | Reads-and-removes a finished job's result so a page reload can't reuse stale data. | `jobs.py` was ported from IQ this session specifically as shared, symmetric infrastructure for the in-app updater — Jordan's only current consumer of `jobs.py` (`/settings/updates/apply`) reads results via `jobs.status()` directly and doesn't need this method yet. Recommend **keeping it** rather than deleting: it's part of a small, intentionally-generic module whose API is meant to match IQ's, and IQ does have (or may grow) consumers that need it (e.g. a future Insights/Retention progress bar, as IQ already has). Deleting it would silently fork the two apps' otherwise-identical `jobs.py` for a few lines of savings with no real benefit. |

---

## Summary / Action Checklist

- [ ] Delete 6 stale entries from `setup.py`'s `INCREMENTAL_SCHEMA_STATEMENTS` (lines 138–144) — confirmed no-ops since this repo's first commit; keep the mechanism itself for Jordan's own future schema changes
- [ ] Delete `auth.py:76` (`ROLES`)
- [ ] Delete `auth.py:59` (`PERMISSION_CATEGORIES`)
- [ ] Delete `auth.py:158` (`is_system_admin`) — or wire it up if you want the gate it implies
- [ ] Delete `logic.py:70–71` (`audit_session_status_label`)
- [ ] Delete `logic.py:486–487` (`visit_total_bill`)
- [ ] Delete `logic.py:1255–1256` (`sellable_items`)
- [ ] Delete `import_seed.py:45` (`INPATIENT_CASE_STATUSES_CLOSED`)
- [ ] Delete unused `PageBreak` import from `pdf_export.py:12`
- [ ] Decide: implement or delete `auth.py:112` (`no_vet_role_configured`) — a half-built safety feature, not ordinary dead code
- [ ] Leave `jobs.py`'s `take_result()` alone — shared infra, not dead weight
- [ ] Fix the stale "SQLite tables" docstring in `logic.py:3`
- [ ] Optional: exclude `generate_test_data.py` from anything packaged for a clinic deployment (harmless if left in — it refuses to run without explicit confirmation)
- [ ] No Critical/Breaking Postgres/SQLite issues found — no urgent action needed there
- [ ] Optional, not urgent: consider a future dedicated pass converting `TEXT`-typed timestamp columns to native `TIMESTAMP` (pervasive, ~30 columns, real but low-priority architectural debt)
