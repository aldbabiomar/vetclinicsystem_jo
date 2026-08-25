# VetClinicSystem JO — clinic management system

A full clinic management system for veterinary clinics: patient records,
visits, inpatient care, boarding, wellness & grooming tracking,
appointments, a point of sale, inventory & ordering, distributor and
consignment tracking, billing & refunds, a cash register, financial
reporting, and business-intelligence dashboards — running entirely on one
computer in the clinic, reachable from any device on the clinic's WiFi. It
also updates itself in-app, straight from GitHub Releases, with an
automatic pre-update backup and one-click rollback if anything goes wrong.

The same codebase deploys independently per clinic (each with its own
database and its own clinic name set in Settings).

Everything lives in a **PostgreSQL** database (running in Docker on the same
computer) so multiple staff can safely use the app at the same time — no
more "database is locked" errors, and the database itself no longer needs a
manual copy for a backup (see **Nightly Backups** below). No internet
connection or cloud account is required to run it day to day — Docker just
needs to be installed once, and internet is only needed for the optional
in-app update check.

## Quick start (macOS or Windows)

**One-time only:**
1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) (free) and open it once so it finishes starting up.
2. **macOS:** double-click `Start VetClinicSystem JO.command` (first time, macOS will refuse to open it — right-click → **Open** → **Open** again; you only need to do this once).
   **Windows:** double-click `Start VetClinicSystem JO.bat`.

That single script creates the Python environment, installs dependencies,
starts PostgreSQL, and sets up the database, seeding it with the clinic's
data on first run. Every run after that just starts the app and opens it
in your browser.

**Manual setup**, if you'd rather run it yourself:
```bash
cd vetclinicsystem_jo
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
python3 setup.py              # starts Postgres in Docker, builds the schema, loads data
python3 app.py
```

Open **http://127.0.0.1:5050** on the server machine.

## Using it from other devices on the clinic network

The app binds to the network, not just the one computer, so any phone,
tablet, or laptop on the same WiFi can reach it. Once running, the exact
address to use is shown on the **Dashboard** and **Settings** pages to admins
(something like `http://192.168.1.X:5050`) — type that into a browser on any
other device. The computer running `python3 app.py` is acting as the server,
so it needs to stay on and running during clinic hours for other devices to
reach it. macOS may prompt to allow incoming connections the first time —
click **Allow**.

## First login

- Username: `admin`  Password: `admin123`
- You'll be forced to set a new password immediately — do this first, before
  creating other staff accounts.
- Create accounts for your team under **Admin → Users**, assigning each
  person a role.

## Roles & permissions

Three roles come pre-configured — **Admin**, **Vet**, **Reception** — but
roles aren't fixed: **Admin → Roles & Permissions** can create any number of
custom roles (e.g. "Practice Manager", "Groomer"), each with its own name,
its own discount cap (0–100%), and its own checkbox-by-checkbox set of ~28
individual permissions spanning Patients & Visits, Inpatient, Inventory,
Sales & Billing, Consignment, and Admin. A role can also be flagged "can be
assigned as a vet," which is what makes it selectable in every vet picker
across Appointments, New Visit, Grooming, and Inpatient.

The three built-in roles seed with sensible defaults:

| | Admin | Vet | Reception |
|---|---|---|---|
| Everyday clinical/front-desk work | ✔ | ✔ | ✔ |
| Discount cap on any bill | 25% | 15% | 10% |
| Price List, Refunds, Cash Register (view/edit) | ✔ | — | — |
| Financial Reports, Insights & Retention | ✔ | — | — |
| Settings, self-update | ✔ | — | — |
| User & Role management | ✔ | — | — |
| Logins and Changes (audit trail) | ✔ | — | — |
| Consignment settlements | ✔ | — | — |
| Mark a vet/groomer unavailable | ✔ | — | ✔ |

Vets and Reception can still see prices while billing (a read-only picker
appears right on the Visit/Inpatient billing panel) even without Price List
access — only the Price List *editing* page itself is gated.

## What's in each part of the app

**Dashboard** — patients, active cases, follow-ups due, reminder calls,
wellness reminders due, grooming queue, low stock, audit/expiry alerts.
Admins (or any role with the right permissions) additionally see a
**missed-items panel** (any follow-up, wellness reminder, or
Lost-to-Follow-Up case that's gone 2+ weeks past its deadline without
action, with the responsible staff member named) and a reminder to enter
this month's operating costs before the month closes.

**Owners & Patients** — one owner can have multiple patients. Patients are
sortable by ID, animal name, species, or owner. Each patient has a full
**History** page (every visit, inpatient stay, and boarding stint, merged
and dated) and two PDF exports: the clinical file, and the billing history.

**Visits** — the single record for an encounter. New Visit branches into
"existing patient" (live search by name/ID/owner/phone) or "new patient"
(owner + patient intake in one form — automatically linked to an existing
owner instead of duplicated if the phone number's already on file). A visit
can carry, all at once: the clinical exam/treatment notes, a follow-up, a
wellness reminder, and a grooming request — since a pet's single visit is
often "checkup + vaccine + a bath" together, these all live on the same
visit rather than being split into separate records. Case status is one of:
Needs Filling, Ongoing, Admitted to Inpatient, Deceased/Euthanized, Lost to
Follow Up, Resolved, Referred. Visits are sortable by date (default), type,
status, or payment, and filterable to a specific date.

**Follow-ups / Wellness / Grooming** — three tabs, each reading straight off
the Visits table:
- *Follow-ups*: method (Physical Visit / Phone Call), reason, status.
- *Wellness*: reminders start 5 days before the next-dose date; flagged
  missed at 2+ weeks overdue if not marked contacted.
- *Grooming*: a working queue (Waiting → Ongoing → Finished) with admitted
  items and contacted status.

**Inpatient** — its own case record (separate from, but linked back to, the
originating visit): presenting complaint, exam findings, a daily update log
(with a "last 3 days" quick view), a contact log (same), a billing tab
(check off procedures used, enter quantity), attending/supervising vet,
admission/dismissal, its own discount + payments, file attachments
(X-rays, bloodwork), and a PDF export.

**Boarding** — a separate kennel/pet-boarding module: entry/exit dates,
an incident log for anything worth noting during the stay, its own billing
and payments, a dismiss workflow, and a PDF export. Boarding stays show up
on the patient's merged History alongside their visits and inpatient cases.

**Appointments** — a week of day-tabs; pick a day to see the booking grid
(one column per active Vet plus one Grooming column, rows generated from the
start time/end time/slot length you set in Settings, auto-lettered A, B, C…).
Double-booking the same vet (or Grooming) into the same slot is blocked
automatically. Admin or Reception can mark a specific vet or Grooming
unavailable for a specific day, which greys out just that column. Booking
doesn't require an existing patient record — it's a scheduling aid; the real
Visit gets created normally when the patient actually arrives.

**Point of Sale** — scan a barcode or search by name to ring up **Retail**
items only; stock deducts in real time, with a fixed per-cart-load token
that stops a double-click on "Complete Sale" from ever ringing up the same
cart twice. **Medicine** billed on a Visit is priced but does *not* touch
inventory (since it's billed by range, not exact dispensed amount) — only
Retail/POS sales move stock.

**Inventory** — several linked pieces:
- *Price List* (Service / Medicine / Retail) and *Inventory Catalog*
  (Medical / Retail) are separate: what you charge vs. what you stock.
- *Audit History* is a whole-catalog counting session: walk the shelf, fill
  in every item's count in one table, **Save** as a draft to finish later,
  or **Confirm** to lock it in permanently (irreversible, with a warning).
  Only Confirmed audits count toward *Inventory Status* and the *Ordering
  Sheet* — a Draft is just a mid-shift checkpoint. Leaving a column blank on
  a new audit means "keep whatever was set last time" for that item
  (threshold, critical flag, target coverage).
- Barcodes: either scan/type in a real one from the item's own packaging,
  or generate one for an item that doesn't have a manufacturer barcode —
  never both at once. Print a single label, or bulk-print labels for every
  item this app generated a barcode for.

**Distributors & Consignment** — track suppliers, log bills owed to them and
payments made, and export a distributor statement to PDF. Retail items can
be flagged **Consignment** (owned by the distributor until sold, not the
clinic) instead of clinic-owned stock — the Consignment area tracks
receiving, shrinkage, and returns for those items separately, shows a live
shelf-stock-and-amount-owed overview per distributor, and records
settlements when a distributor is paid out.

**Billing** — Automatic (priced line items, computed from what's actually
checked off) or Manual (one lump amount, exported on PDF as a single
"Veterinary Services" line) — your choice per visit or inpatient case.
Payment status (Unpaid / Partially Paid / Fully Paid) is computed from
actual payments recorded, not typed in by hand.

**Refunds** — separate retail (against a specific POS sale, restocking
optional) and service (against a visit or inpatient case's payments) refund
flows, each capped against what was actually paid so a refund can never
exceed real money taken in.

**Cash Register** — a daily ledger of everything that moved cash in or out
(sales, payments, payouts, refunds), a payout tool for cash leaving the
drawer for a reason, and an end-of-day audit that compares the ledger's
expected total against a physical note count.

**Monthly & Yearly P&L / Operating Costs** — revenue from both visit billing
and POS sales, COGS from measured stock usage, editable monthly operating
costs, month-over-month and year-over-year % change (green = up, red =
down).

**Insights** — a business-intelligence dashboard computed across up to 12
months of history in parallel: revenue by category, vet performance, top
client value, weekday appointment load, inpatient/boarding occupancy,
payment-method mix, and Cash Register health.

**Retention** — a cohort retention grid showing how many clients from each
month's first visit came back in each following month.

**Logins and Changes** (audit trail) — pick a date on the calendar to see
every login attempt (who, when, IP, device/browser) and every data change
(who, what record, old value → new value) on that day.

**Settings** — clinic name and location; numeric thresholds (audit-overdue
days, expiry-soon days, appointment slot length); nightly backup folder,
time, and retention (with an in-app folder browser to pick or create the
backup destination); starting the app automatically when this computer
starts; and, on an install that's opted into the versioned-release layout,
**in-app updates** — check for a new version, apply it (automatic
pre-update backup, progress shown step by step), or roll back to the
previous release with one click if something's wrong.

## Security & hardening

Runs safely as a normal LAN app out of the box, with several things opt-in
for a deployment that needs them: CSRF protection on every form, a
Content-Security-Policy header, per-IP and per-account login rate limiting
with an escalating lockout, session invalidation on password change,
configurable session lifetime, an optional network allowlist (restrict which
client IPs can reach the app at all), and optional reverse-proxy/TLS
awareness for a deployment that puts one in front. All of this is
environment-variable driven — see `.env.example` for the full list — and
none of it changes default behavior for a normal single-router clinic LAN
unless explicitly configured.

## A few judgment calls made during the build

- **Grooming lives on the Visit record itself**, not a separate table,
  since a single visit is often several things at once for the same
  patient.
- **File uploads (X-rays, bloodwork, etc.)** are keyed by patient ID +
  visit/case ID on disk (`uploads/<patient_id>/<record_id>/…`), not by
  date, since two records can share a calendar date for the same patient.
  Only the file path is stored in the database — the files themselves stay
  on disk, so the database doesn't balloon in size.
- A web app can't pop open your Mac's actual Finder — the folder-picker
  fields (e.g. choosing a backup destination) use an in-app file browser
  instead, with the same practical result.

## Nightly Backups

Set a backup folder on the **Settings** page (any path on this computer —
an internal folder, an external drive, a mapped network share, or a synced
folder like Google Drive/OneDrive all work). Every night at the time you
choose, the app runs a full database backup into that folder, deletes
backups older than the number you choose to keep, and shows the result on
Settings (and warns on the Dashboard if a backup fails or goes stale). You
can also click **Back Up Now** any time, or restore from a backup file
straight from the Settings page.

To restore a backup manually (only needed if you're recovering from a
serious problem outside the app):
```bash
pg_restore --clean --if-exists -d "$DATABASE_URL" path/to/vetclinicsystemjo_backup_XXXXXXXX_XXXXXX.dump
```

## Staying up to date

Once an install is set up for it (`python3 setup.py --enable-updates`), the
**Settings** page can check for, apply, and roll back updates without ever
touching a terminal. Applying an update automatically backs up the database
first, downloads and validates the new release, applies any additive
database changes, and switches over — with the previous release kept
around so a one-click rollback is always available if something looks
wrong afterward.

## Running on multiple computers / higher traffic

This runs comfortably for a single clinic's simultaneous staff on one
server machine. If you ever need to move the database to its own server,
just point `DATABASE_URL` in `.env` at that server instead of the local
Docker container — nothing else in the app needs to change.

## Running the tests

The money math — totals, discounts, write-offs, and the Decimal
discipline that keeps the JOD exact to the fils — is the part of this app
most worth checking on every change, and the part where a mistake is
least visible: a wrong colour is obvious, a wrong total is a bill someone
already paid. `tests/test_money.py` covers it.

The tests need no database, no Docker and no running app. From the repo
root, with the same virtual environment you set up in Quick start:

```
venv/bin/python -m pip install pytest
venv/bin/python -m pytest tests/ -q
```

(They import `app.py`, so they need the app's own dependencies — which is
why they run from that venv rather than a bare Python.)

Everything should pass in well under a second. If something fails,
**read what it says before changing it**: several of these tests exist
because the bug they describe already happened once. The tests around a
leftover balance are the clearest example — a threshold carried over
unchanged from the IQD original once marked bills with up to 500 fils
still owing as "Fully Paid", quietly hiding real uncollected money.

**A warning if you also work on VetClinicSystem IQ:** the two apps'
`test_money.py` files make deliberately *opposite* assertions, because
the IQD is rounded to a 250 note and has an anti-"looks free" floor.
Never copy one over the other. See `COMPARISON.md` §1.1 in the shared
workspace folder.

## Your data

Everything lives in PostgreSQL (inside the `vetclinicsystemjo_pgdata` Docker volume) —
see **Nightly Backups** above for how it's backed up automatically.
Uploaded X-rays/bloodwork still live in the `uploads/` folder alongside the
app — that folder isn't part of the database backup, so also back it up
separately (e.g. include it in whatever backs up the rest of this computer).
