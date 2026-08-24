# Changelog

All notable changes to VetClinicSystem JO are documented in this file, in
[Keep a Changelog](https://keepachangelog.com) style.

## [1.6.4] - 2026-08-24

### Fixed
- The light/dark toggle in the sidebar is now the same finger-sized target
  as everything else on a phone.

## [1.6.3] - 2026-08-24

### Fixed
- **On phones and tablets, wide lists dragged the whole page sideways**
  (Price List, Monthly P&L, Visits, Sales History, Patients, Inpatient).
  Those tables now scroll on their own inside their card, so the page and
  the menu bar stay put. On a full-size screen nothing changes — the
  column headings still stay put as you scroll.
- **Tablets in portrait had the same sideways-scrolling problem** even on
  the layout as a whole. Fixed.
- **Typing in a form on an iPhone or iPad zoomed the page in** and left it
  zoomed. Form fields are now sized so that stops happening.
- **Buttons and menu items were too small to tap comfortably** on a phone —
  the sidebar links, the menu button, and ordinary buttons are all now a
  full finger-sized target. Unchanged on desktop.

## [1.6.2] - 2026-08-24

### Changed
- **The amber/warning colour is back to its original gold** on borders,
  icons and legend markers. Only the *text* that sits on an amber
  background (status chips, warning banners) uses a darker shade, because
  that is the part that has to stay readable — and those backgrounds are
  now paler too, so the chips look lighter overall.

## [1.6.1] - 2026-08-24

### Changed
- Green, amber and red accents are slightly deeper so status chips and
  buttons using them are properly readable. Same hues, just darker; the
  main crimson, the sidebar and the overall look are unchanged.

## [1.6.0] - 2026-08-24

### Added
- **Dark mode.** A sun/moon button next to the clinic name in the sidebar
  switches between light and dark, and remembers your choice on that
  computer. Date pickers, dropdowns and scrollbars follow the theme too.
  Light mode is unchanged.

## [1.5.0] - 2026-08-24

### Added
- **Column headings now stay put while you scroll long lists** — Owners,
  Patients, Visits, Follow-Ups, Wellness, Grooming, Boarding, Inpatient,
  Price List, Inventory Catalog, Audit Sessions, Sales History, Refunds,
  Yearly P&L and the Dashboard's missed-items table. Previously only the
  Consignment screens did this.

### Fixed
- On a phone, those sticky headings would have sat hidden behind the top
  bar. They now sit just below it.

## [1.4.4] - 2026-08-24

### Fixed
- Hardened how the database password is handed to the backup tools on
  Docker-based installs, so it is passed privately rather than in a way
  other users of the same computer could read.

## [1.4.3] - 2026-08-24

### Fixed
- **Backups could appear to hang forever.** The backup was actually
  waiting for a database password to be typed into the black Terminal
  window behind the app — easy to miss entirely, and the password it
  wanted was the database's, not your login. Backups and restores now
  use the password the app already has, and if a password is ever wrong
  they stop straight away with a clear message instead of waiting.
  This affected the nightly automatic backup and the backup taken
  before an update too, not just the Back Up Now button.
- **Back Up Now no longer pretends to start when no backup folder is
  set.** The button stays disabled until you've chosen and saved a
  folder, and tells you so, instead of running a progress bar and then
  reporting a failure. That case also no longer clutters Recent Backups
  with failed entries.

## [1.4.2] - 2026-08-24

### Fixed
- The Browse-for-a-folder window's buttons were scattered across two
  uneven rows. The folder-name box and all the buttons now sit neatly on
  one line, lined up with the folder list above them — and on a phone or
  narrow window they stack, with Cancel and Select centred underneath.
- Pop-up windows could be wider than the screen on a phone, pushing their
  buttons out of reach. They now always fit.

## [1.4.1] - 2026-08-24

### Fixed
- Buttons that warn before something destructive (Restore Now, and others
  like it) turned a slightly-off colour when hovered — one that didn't
  belong to VetClinicSystem JO's own palette. They now darken correctly. The same
  fix cleans up a handful of other places where a stray colour had been
  hardcoded instead of following the app's palette, including the highlight when you hover a row in a table.

## [1.4.0] - 2026-08-24

### Added
- **A VetClinicSystem JO icon on the Desktop.** Double-click it to start
  the app — and if the app is already running, it just brings it up in
  your browser instead of starting a second copy. It keeps working after
  updates, shutdowns and restarts, so it's there as a reliable way in if
  "start automatically when this computer starts" ever doesn't fire. It's
  created for you during setup; if it ever gets deleted, running setup
  again puts it back.
- **Collapsible sidebar sections.** Inventory, Consignment, Sales &
  Billing and Admin can now be folded away by clicking their heading, so
  a long sidebar can be trimmed to just the parts you use. Each person's
  choice is remembered on their own computer.

## [1.3.1] - 2026-08-24

### Fixed
- "Start automatically when this computer starts" could point at the
  wrong copy of the app on installs with automatic updates enabled — it
  would keep launching whatever version was installed at the time you
  turned the toggle on, ignoring later updates until the app was started
  by hand at least once. It now always finds and uses the correct,
  currently-active version on every restart.

## [1.3.0] - 2026-08-24

### Added
- **Clean Up** — a small, capped amount you can apply to a visit,
  inpatient, or boarding bill (or a POS sale) to round off or write down
  the total, instead of forcing every bill to land on an exact figure.
  Shows on the bill, the printed receipt/PDF, and is accounted for
  correctly if the sale is later refunded.
- **Back Up Now** shows a live progress bar and no longer freezes the
  page while the backup runs.

### Fixed
- Several forms could still be submitted with a required field left
  blank (owner/patient name, price list/inventory item name, appointment
  details) without a clear error — now rejected with a specific message
  instead of a confusing failure later on.
- A visit's Body Condition Score, and quantities/amounts on Point of
  Sale, refunds, and inpatient billing, are now checked against sane
  bounds before saving, instead of accepting anything typed in.
- Marking a visit "Admitted to Inpatient" now reliably creates the
  matching inpatient case (and the reverse — you can't move a visit off
  that status while its inpatient case is still open).
- A handful of pages could occasionally crash with a server error
  instead of showing a normal message — creating/editing price list or
  inventory items with a distributor or linked item that no longer
  exists, deleting a role or disabling a user whose upcoming appointments
  would be left stranded, and a few others. These now either fail
  cleanly with an explanation or, where it makes sense, warn you and let
  you continue.
- Deactivated inventory items no longer disappear from an audit session
  that already referenced them.
- A discount can no longer be applied to a visit before its bill has
  been saved.
- Deleting an inpatient billing line, or applying a discount, can no
  longer push a bill below what's already been paid on it.
- A refund is now required to reference exactly one visit or one
  inpatient case — never both, never neither.
- New Draft audit sessions can be discarded, and can no longer be
  confirmed with nothing actually counted.
- Attached files, payments, and refunds are now guaranteed at the
  database level to reference exactly one thing, closing a class of
  bug where a bad or incomplete request could leave one referencing
  nothing (or everything at once).
- If the app is closed or crashes mid-restore, Settings now shows a
  clear warning instead of silently leaving the database in an unknown
  state.
- Backup/restore/update no longer silently overlap with each other if
  triggered close together; a stale "running" backup left over from a
  crash is now automatically cleared on the next startup instead of
  blocking new ones forever.
- A single bad row created by very old data no longer prevents *every*
  routine database update from applying on startup — only that one
  update is skipped (and flagged on the Dashboard for an admin to look
  at), the rest still apply normally.
- Editing or deleting a custom role that's the only one marked "can be
  assigned as a vet" now warns you if it leaves any staff member's
  upcoming appointments stranded, same as removing a person from vet
  duty individually already did.

## [1.2.3] - 2026-08-24

### Fixed
- **A mistake in a form (an invalid phone number, a bad date) used to
  wipe out everything else you'd typed and bounce you back to a blank
  page.** Forms now show exactly what you entered, with the problem
  field flagged, so you only need to fix the one thing — across Log
  Visit, Owners, Distributors, Boarding, Appointments, Inpatient Cases,
  billing/discount/payment, Price List, Inventory Catalog, Audit
  History, and Reports.
- Phone numbers are now checked as you type, before you submit, instead
  of only after a failed save.
- Fixed a bug where logging a new visit for a brand-new owner could
  leave a duplicate patient behind if the visit's own date/weight/BCS
  failed validation after the owner and pet had already been saved.

## [1.2.2] - 2026-08-23

### Fixed
- **The Dashboard's missed-items panel didn't sort by deadline** — items
  showed in whatever order the underlying queries happened to return them,
  rather than newest-missed-first. Follow-ups, wellness reminders, and
  Lost-to-Follow-Up cases now all sort consistently, newest deadline first.

## [1.2.1] - 2026-08-23

### Fixed
- **Auto-generated inventory barcodes were 12 digits, not real EAN-13's
  13** — one digit short in the random body, so every generated barcode
  failed strict EAN-13 validation. Now generates the correct length;
  already-generated barcodes are unaffected and keep printing normally.

## [1.2.0] - 2026-08-23

### Added
- **Toasts and styled confirm dialogs**, replacing native browser
  `alert()`/`confirm()` throughout the app, plus a background-job
  progress UI for Insights, Retention, and Consignment Overview.
- **Custom role creation and editing** — Users & Roles now has a full
  Roles & Permissions tab: create a role, choose exactly which
  permissions and discount cap it gets, and edit or delete it later.
  The built-in Admin role stays locked. Also added a per-user discount
  override at account creation (inherit from role, or set a custom
  limit for that person).
- **In-app database restore** — Settings → Restore From Backup can now
  restore any backup this app created, with the same progress UI as
  Backup Now. A backup file can only be restored if it's inside the
  configured backup folder and actually appears in this app's own
  backup history — never an arbitrary path.
- **Backup-folder browser** — Settings' Backup Folder field now has a
  Browse… button to pick (or create) a folder on this computer,
  instead of typing a path by hand.
- **Start automatically on login** — a Settings toggle to have
  VetClinicSystem JO launch automatically when this computer starts
  (macOS and Windows).
- **Manually enter a barcode** — Inventory Catalog items can now use a
  real barcode scanned or typed in from the product's own packaging,
  as an alternative to a VetClinicSystem-generated one. Bulk Barcode
  Print continues to cover only the barcodes this app created.
- **`reconcile_attachments.py`** — a new maintenance script to safely
  relink attachment files that a database restore left without a
  matching record. Run `python3 reconcile_attachments.py` (dry run) or
  `--apply` from the app folder after a restore.

### Changed
- Barcode labels (single and bulk print) now detect the right barcode
  format automatically instead of assuming every code is EAN-13 —
  needed for manually entered codes, which aren't always EAN-13.
- Removed the three separate Admin/Vet/Reception discount-limit fields
  from Settings; each role's discount cap is now set on the role
  itself, in Users & Roles.

## [1.1.0] - 2026-08-23

### Fixed
- **~90 of ~130 routes had no per-permission check at all** — only login was required, not the specific permission (Manage Owners, Manage Visits, Process POS Sales, Manage Boarding, etc.) that the Roles & Permissions page presents as togglable. Every one of those routes now carries the matching permission check, verified against no regression for the Admin/Vet/Reception roles' existing default access.
- **POS checkout could oversell stock under genuine concurrent load** — the row lock that serializes concurrent checkouts was sound, but the stock-since-last-audit calculation compared whole-second-precision timestamps with a strict `>`; a sale landing in the same wall-clock second as the audit it was being checked against got silently excluded from the running total, letting stock go negative while Inventory Status still reported a plausible (wrong) number. Every timestamp feeding that comparison now carries microsecond precision.
- **Re-entering an existing owner's name and phone while adding a pet created a duplicate owner record** instead of linking to the existing one, both from the "new patient" visit form and from double-submitting the New Owner form. Owner phone numbers are now enforced unique at the database level; a submission that collides with an existing owner links to them instead of erroring or duplicating.
- **Double-clicking "Complete Sale" on an unchanged cart created two separate, fully valid sales** — double-charging the customer and double-deducting stock, with no confirmation prompt either side. Checkout now carries a one-time token per POS page load; a repeat submission is recognized and sent to the original sale instead of creating another one.
- **A database outage could occasionally show a raw, unbranded error page** instead of the app's own error page, in the narrow window right as the connection dropped — traced to the per-request cleanup step trying to commit/roll back an already-dead connection outside the app's normal error handling. Now guarded, and each request also fails faster during an outage instead of hanging for the full connection-pool timeout.

## [1.0.3] - 2026-08-23

### Fixed
- An account lockout could be kept renewed indefinitely by firing a fresh
  burst of wrong-password guesses right as the previous lockout expired.
  Lockouts now escalate (15/30/60/120 min, capped at 4 hours) across
  repeated episodes and reset on a successful login.
- Changing or resetting a password now signs out that user's other active
  sessions immediately, instead of leaving them valid for up to 12 hours.
- Logging out while forced to change your password now actually logs you
  out, instead of bouncing back to the Change Password page.
- Backup dump files are now restricted to owner-only permissions — they
  contain full patient/owner information.
- A very large `?page=` value, a null byte in a URL or form field, and a
  malformed date filter on Visits/POS History/Refunds no longer produce a
  raw error page.

### Added
- A baseline Content-Security-Policy header.

## [1.0.2] - 2026-08-22

### Fixed
- POS checkout no longer accepts cash tendered below the sale total.
- Service refunds are capped at what was actually paid on the linked visit
  or inpatient case, minus refunds already recorded against it.
- Visit and inpatient payments are capped at the remaining balance.
- Consignment settlements can no longer overpay past the amount owed.
- Cash-register payouts are capped at the drawer's expected cash for the day.
- Closed a race between applying a discount and adding a non-discountable
  item to a visit or inpatient bill; inpatient billing also gained the
  same protection visit billing already had for this.
- A bill could show "Fully Paid" with up to half a Dinar genuinely still
  owed — a rounding-tolerance threshold left over from an earlier
  currency model. Bills now only show "Fully Paid" when nothing is left.
- Attachment deletion now requires the same permission it does everywhere
  else in the app (previously any logged-in user could delete any
  patient's attachment).

## [1.0.1] - 2026-08-22

### Fixed
- `SECRET_KEY` is now rejected if it's still the `.env.example` placeholder
  value, not just if it's unset — hand-copying that file instead of running
  `setup.py` used to boot fine with a well-known, publicly-visible secret
  signing every session cookie and CSRF token.

### Changed
- Removed 8 confirmed-dead functions/constants/imports (unused role/permission
  helpers, a superseded billing/POS helper, an unwired audit-status label,
  a stray import) and cleared 6 no-op entries from the schema migration list
  — full details in `MIGRATION_AND_DEADCODE_AUDIT.md`. No behavior change.

## [1.0.0] - 2026-08-22

### Added
- In-app update mechanism: an admin can check for, install, and roll back
  tagged releases from the Settings page (see `updater.py` and the
  Updates section of Settings) without touching the command line. Updates
  are downloaded from GitHub Releases, backed up against first, installed
  into their own isolated environment, health-checked on a throwaway
  port, and only then switched to — a failed update never takes the
  clinic offline. This is the first version tracked through that
  mechanism, so it establishes the starting point rather than describing
  new clinic-facing behavior.
