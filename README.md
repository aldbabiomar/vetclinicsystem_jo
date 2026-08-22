# Jordan Referral Center — clinic management system

A full clinic management system for Jordan Referral Center (Amman, Jordan) — patients, visits,
inpatient care, wellness & grooming tracking, appointments, a point of sale,
inventory & ordering, billing, and P&L reporting — running entirely on one
computer in the clinic, reachable from any device on the clinic's WiFi.

Everything lives in a **PostgreSQL** database (running in Docker on the same
computer) so multiple staff can safely use the app at the same time — no
more "database is locked" errors, and the database itself no longer needs a
manual copy for a backup (see **Nightly Backups** below). No internet
connection or cloud account is required to run it day to day; Docker just
needs to be installed once.

## Quick start (macOS or Windows)

**One-time only:**
1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) (free) and open it once so it finishes starting up.
2. **macOS:** double-click `Start Jordan Referral Center.command` (first time, macOS will refuse to open it — right-click → **Open** → **Open** again; you only need to do this once).
   **Windows:** double-click `Start Jordan Referral Center.bat`.

That single script creates the Python environment, installs dependencies,
starts PostgreSQL, and sets up the database, seeding it with the clinic's
data on first run. Every run after that just starts the app and opens it
in your browser.

**Manual setup**, if you'd rather run it yourself:
```bash
cd 'Jordan Referral Center'
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
  person a role: **Admin**, **Vet**, or **Reception**.

## Roles

| | Admin | Vet | Reception |
|---|---|---|---|
| Everyday clinical/front-desk work | ✔ | ✔ | ✔ |
| Discount cap on any bill | 25% | 15% | 10% |
| Price List (view/edit) | ✔ | — | — |
| Monthly & Yearly P&L | ✔ | — | — |
| Settings | ✔ | — | — |
| User management | ✔ | — | — |
| Logins and Changes (audit trail) | ✔ | — | — |
| Mark a vet/groomer unavailable | ✔ | — | ✔ |

Vets and Reception can still see prices while billing (a read-only picker
appears right on the Visit/Inpatient billing panel) — only the Price List
*editing* page itself is admin-only.

## What's in each part of the app

**Dashboard** — patients, active cases, follow-ups due, reminder calls,
wellness reminders due, grooming queue, low stock, audit/expiry alerts.
Admins additionally see a **missed-items panel** (any follow-up, wellness
reminder, or Lost-to-Follow-Up case that's gone 2+ weeks past its deadline
without action, with the responsible staff member named) and a reminder to
enter this month's operating costs before the month closes.

**Owners & Patients** — one owner can have multiple patients. Patients are
sortable by ID, animal name, species, or owner. Each patient has a full
**History** page (every visit and inpatient stay, merged and dated) and two
PDF exports: the clinical file, and the billing history.

**Visits** — the single record for an encounter. New Visit branches into
"existing patient" (live search by name/ID/owner/phone) or "new patient"
(owner + patient intake in one form). A visit can carry, all at once: the
clinical exam/treatment notes, a follow-up, a wellness reminder, and a
grooming request — since a pet's single visit is often "checkup + vaccine +
a bath" together, these all live on the same visit rather than being split
into separate records. Case status is one of: Needs Filling, Ongoing,
Admitted to Inpatient, Deceased/Euthanized, Lost to Follow Up, Resolved,
Referred. Visits are sortable by date (default), type, status, or payment,
and filterable to a specific date.

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
admission/dismissal, and its own discount + payments.

**Appointments** — a week of day-tabs; pick a day to see the booking grid
(one column per active Vet plus one Grooming column, rows generated from the
start time/end time/slot length you set in Settings, auto-lettered A, B, C…).
Double-booking the same vet (or Grooming) into the same slot is blocked
automatically. Admin or Reception can mark a specific vet or Grooming
unavailable for a specific day, which greys out just that column. Booking
doesn't require an existing patient record — it's a scheduling aid; the real
Visit gets created normally when the patient actually arrives.

**Point of Sale** — scan a barcode or search by name to ring up **Retail**
items only; stock deducts in real time. Inventory Catalog has a "Create
barcode" button for any item without a manufacturer barcode, plus a
printable label. **Medicine** billed on a Visit is priced but does *not*
touch inventory (since it's billed by range, not exact dispensed amount, per
your instruction) — only Retail/POS sales move stock.

**Inventory** — Audit History is a whole-catalog session: walk the shelf,
fill in every item's count in one table, click **Save** to keep it as a
draft you can finish later, or **Confirm** to lock it in permanently (a
warning pops up first, since Confirm can't be undone). Only Confirmed audits
count toward Inventory Status and the Ordering Sheet — a Draft is just a
mid-shift checkpoint. Leaving a column blank on a new audit means "keep
whatever was set last time" for that item (threshold, critical flag, target
coverage). Price List has 3 categories (Service, Medicine, Retail);
Inventory Catalog has 2 (Medical, Retail).

**Billing** — Automatic (priced line items, computed as before) or Manual
(one lump amount, exported on PDF as a single "Veterinary Services" line) —
your choice per visit or inpatient case. Payment status (Unpaid / Partially
Paid / Fully Paid) is computed from actual payments recorded, not typed in
by hand.

**Monthly & Yearly P&L** (Admin only) — revenue from both visit billing and
POS sales, COGS from measured stock usage, editable monthly operating costs,
month-over-month and year-over-year % change (green = up, red = down).

**Logins and Changes** (Admin only) — pick a date on the calendar to see
every login attempt (who, when, IP, device/browser) and every data change
(who, what record, old value → new value) on that day.

## A few judgment calls made during the build

- **Grooming lives on the Visit record itself**, not a separate table — per
  your instruction, since a single visit is often several things at once for
  the same patient.
- **Distributors started empty** — the source spreadsheet's one row was
  explicitly marked "delete and replace with your real distributors."
- **Price List categories were auto-remapped** from the original spreadsheet's
  15 categories down to the new 3 (Service/Medicine/Retail) — worth a quick
  review in Price List, since only clinical judgement can say for certain
  which items should really be Medicine vs. Service.
- **File uploads (X-rays, bloodwork, etc.)** are keyed by patient ID + visit
  ID on disk (`uploads/<patient_id>/<visit_id>/…`), not by date, since two
  records can share a calendar date for the same patient. Only the file path
  is stored in the database — the files themselves stay on disk, so the
  database doesn't balloon in size.
- A web app can't pop open your Mac's actual Finder — the "additional tests"
  icon opens an in-app file list instead, with the same practical result.

## Nightly Backups

Set a backup folder on the **Settings** page (any path on this computer —
an internal folder, an external drive, a mapped network share, or a synced
folder like Google Drive/OneDrive all work). Every night at the time you
choose, the app runs a full database backup into that folder, deletes
backups older than the number you choose to keep, and shows the result on
Settings (and warns on the Dashboard if a backup fails or goes stale). You
can also click **Back Up Now** any time.

To restore a backup (only needed if you're recovering from a serious
problem):
```bash
pg_restore --clean --if-exists -d "$DATABASE_URL" path/to/jrc_backup_XXXXXXXX_XXXXXX.dump
```

## Running on multiple computers / higher traffic

This runs comfortably for a single clinic's simultaneous staff on one
server machine. If you ever need to move the database to its own server,
just point `DATABASE_URL` in `.env` at that server instead of the local
Docker container — nothing else in the app needs to change.

## Your data

Everything lives in PostgreSQL (inside the `jrc_pgdata` Docker volume) —
see **Nightly Backups** above for how it's backed up automatically.
Uploaded X-rays/bloodwork still live in the `uploads/` folder alongside the
app — that folder isn't part of the database backup, so also back it up
separately (e.g. include it in whatever backs up the rest of this computer).
