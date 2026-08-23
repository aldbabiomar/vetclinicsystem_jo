# Changelog

All notable changes to VetClinicSystem JO are documented in this file, in
[Keep a Changelog](https://keepachangelog.com) style.

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
