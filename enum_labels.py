"""Display labels for values that are STORED in the database in English.

Wrapping a template literal does nothing for these: the text on the page came
out of a row, not out of the markup. `app.py`'s `|tr` filter looks the stored
value up in the translation catalogue, and this module is what makes the
values *extractable* — `pybabel` can only see strings that appear in a `_()`
call somewhere in the source, and a value that only ever exists as a database
row appears nowhere.

Nothing here is imported for its value; the calls exist so extraction finds
them. `lazy_gettext` rather than `gettext` because this runs at import time,
outside any request, where there is no locale to resolve yet.

**Never change a value on the left.** These strings are the stored constants
that routes validate against and that CHECK constraints enforce — translating
the *stored* value would break both. Only the display goes through `|tr`.

**The literals here are duplicates, and that is the point.** The real
constants live in `routes/`, `core.py`, `logic.py` and the templates;
`_(CASE_STATUSES)` on a variable extracts nothing, so extraction needs the
strings spelled out. Duplication that nothing checks is how the first version
of this file came to declare a grooming status of "In Progress" that no code
has ever stored, while the real "Waiting" went untranslated.
`tests/test_enum_labels.py` compares every list below against its source of
truth, so a value that drifts fails a test instead of silently not
translating.
"""
# Aliased to `_` rather than `_l` because pybabel's default keyword list
# contains `_` and not `_l` — under the other name every string in this
# file is invisible to extraction, which is exactly what happened first
# time and showed up as a catalogue that did not grow.
from flask_babel import lazy_gettext as _

# --- mirrors of Python constants (test_enum_labels checks each against its
# --- module: routes.clinical, routes.inventory, core, logic)

# routes.clinical.CASE_STATUSES — visits.case_status, a schema CHECK
CASE_STATUSES = [
    _("Needs Filling"), _("Ongoing"), _("Admitted to Inpatient"),
    _("Deceased/Euthanized"), _("Lost to Follow Up"), _("Resolved"), _("Referred"),
]
# routes.clinical.FOLLOWUP_REASONS — visits.followup_reason
FOLLOWUP_REASONS = [
    _("Surgery Follow Up"), _("Medical Follow Up"), _("Vaccine"),
    _("Deworming"), _("Spot On"), _("Other"),
]
# routes.clinical.WELLNESS_TYPES — visits.wellness_type
WELLNESS_TYPES = [
    _("Annual Vaccine"), _("First Vaccine"), _("Rabies Vaccine"),
    _("Deworming"), _("Spot On/Pill"),
]
# logic.GROOMING_SERVICES — the grooming service checkboxes, stored as text
GROOMING_SERVICES = [
    _("Bath"), _("Haircut"), _("De-shedding"), _("Nail Trim"), _("Ear Cleaning"),
    _("Ear Mites Cleaning"), _("Paw Clipping"), _("Nail Caps"),
    _("Anal Gland Emptying"), _("Zoning"),
]
# core.PAYMENT_METHODS — payments.method, sales.payment_method, refunds,
# settlements and distributor bill payments all share this vocabulary
PAYMENT_METHODS = [_("Cash"), _("Card"), _("Transfer")]
# routes.inventory.PRICE_CATEGORIES — price_list.category, a schema CHECK
PRICE_CATEGORIES = [_("Service"), _("Medicine"), _("Retail")]
# routes.inventory.INVENTORY_CATEGORIES — inventory_list.category, a CHECK
INVENTORY_CATEGORIES = [_("Medical"), _("Retail")]
# logic.REVENUE_CATEGORIES — the insights/P&L breakdown
REVENUE_CATEGORIES = [_("Service"), _("Medicine"), _("Retail"), _("Boarding")]

# --- mirrors of literal lists that live only in a template
# --- (test_enum_labels parses the templates for these)

# visits.visit_type
VISIT_TYPES = [_("Outpatient"), _("Inpatient")]
# visits.followup_status — also the /follow-ups filter
FOLLOWUP_STATUSES = [_("Pending"), _("Completed"), _("Cancelled"), _("N/A")]
# visits.followup_method
FOLLOWUP_METHODS = [_("Physical Visit"), _("Phone Call")]
# visits.grooming_status — NOT Pending/In Progress; see the module docstring
GROOMING_STATUSES = [_("Waiting"), _("Ongoing"), _("Finished")]
# patients.species / .repro_status / .housing
SPECIES = [_("Dog"), _("Cat"), _("Bird"), _("Rabbit"), _("Other")]
REPRO_STATUSES = [_("Intact"), _("Neutered"), _("Spayed")]
HOUSING = [_("Indoor"), _("Outdoor"), _("Stray")]
# patients.sex — stored as the single letters, which is why the msgids are
# one character long. Translated for display only.
SEXES = [_("M"), _("F")]

# --- values a route computes or a schema CHECK fixes, rendered but never
# --- offered in a <select>, so there is no list to compare against

# logic.compute_bill_totals() — visits.payment_status
PAYMENT_STATUSES = [_("Unpaid"), _("Partially Paid"), _("Fully Paid"), _("N/A")]
# distributor_bills.status
BILL_STATUSES = [_("Unpaid"), _("Partially Paid"), _("Paid")]
# inventory_list.ownership_type, a schema CHECK
OWNERSHIP_TYPES = [_("Owned"), _("Consignment")]
# consignment_shrinkage.reason / .liable_party, both schema CHECKs
SHRINKAGE_REASONS = [_("Damaged"), _("Expired"), _("Other")]
LIABLE_PARTIES = [_("Distributor"), _("Clinic")]
# audit_sessions.status, a schema CHECK
AUDIT_STATUSES = [_("Draft"), _("Confirmed")]
# cash_register_audits.status, a schema CHECK
CASH_AUDIT_STATUSES = [_("Deficit"), _("Surplus"), _("Perfect")]
# appointments.appointment_type, a schema CHECK
APPOINTMENT_TYPES = [_("Medical"), _("Grooming")]
# refunds.refund_type, a schema CHECK — stored lower-case
REFUND_TYPES = [_("retail"), _("service")]
# audit_log.action, rendered as a badge on the change log
AUDIT_ACTIONS = [_("create"), _("update"), _("delete")]
# logic.WEEKDAY_LABELS — the Insights weekday-load table
WEEKDAY_LABELS = [
    _("Sunday"), _("Monday"), _("Tuesday"), _("Wednesday"), _("Thursday"),
    _("Friday"), _("Saturday"),
]
# backup_log.triggered_by / update_log, rendered through |title before |tr
BACKUP_TRIGGERS = [_("Manual"), _("Nightly"), _("Shutdown")]
# auth.py's seeded roles. A clinic can rename these and write its own
# description; when it does there is no catalogue entry and its own wording
# passes through |tr unchanged, which is what should happen.
SEEDED_ROLE_NAMES = [_("Admin"), _("Vet"), _("Reception")]
SEEDED_ROLE_DESCRIPTIONS = [
    _("Full access to every area of the app, always. There must be at least one "
      "active Admin."),
    _("Clinical staff — patient care, visits, and inpatient cases."),
    _("Front desk — scheduling, checkout, and client-facing tasks."),
]
# logic.cash_register_ledger() — the ledger's event_type column, built in SQL
CASH_LEDGER_EVENTS = [
    _("POS Sale"), _("Visit Payment"), _("Inpatient Payment"), _("Boarding Payment"),
    _("Retail Refund"), _("Service Refund"), _("Register Payout"),
    # the CASE fallback for a payment row tied to none of the three
    _("Payment"),
]
# auth.PERMISSIONS labels and auth.PERMISSION_CATEGORIES — the roles matrix
PERMISSION_CATEGORIES = [
    _("Patients & Visits"), _("Inpatient"), _("Inventory"), _("Sales & Billing"),
    _("Admin"), _("Consignment"),
]
PERMISSION_LABELS = [
    _("Manage Owners"), _("Manage Patients"), _("Manage Visits"),
    _("Manage Follow-Ups"), _("Manage Wellness Plans"), _("Manage Grooming"),
    _("Manage Boarding"), _("Manage Appointments"), _("Manage Inpatient Cases"),
    _("View Inventory Status"), _("Manage Ordering Sheet"), _("Manage Audit History"),
    _("Manage Inventory Catalog"), _("Manage Distributors"), _("Process POS Sales"),
    _("View Sales History"), _("Manage Price List"), _("Manage Refunds"),
    _("Manage Cash Register"), _("View Financial Reports"),
    _("View Insights & Retention"), _("Manage Users & Roles"), _("Manage Settings"),
    _("Manage Backups, Updates & Startup"), _("View Logins & Change Log"),
    _("View Consignment"), _("Manage Consignment Items"),
    _("Log Receiving, Returns & Shrinkage"), _("Manage Settlements"),
]
# logic.ordering_sheet() priority, and the appointments grid's non-vet column
ORDER_PRIORITIES = [_("CRITICAL"), _("URGENT"), _("SOON"), _("OK"), _("No data")]
APPOINTMENT_COLUMNS = [_("Grooming")]
# logic.inventory_status() stock_status, and the ordering sheet's usage trend
STOCK_STATUSES = [_("LOW STOCK"), _("No audits yet"), _("OK")]
USAGE_TRENDS = [_("Not enough data"), _("Increasing"), _("Decreasing"), _("Steady")]
TREND_NOTES = [
    _("Not enough audit history yet (need 2+ confirmed audits)"),
    _("Usage rising - consider more coverage days"),
    _("Usage falling - consider fewer coverage days"),
    _("Usage steady - keep current target"),
]
# logic.inventory_status() — computed per row, rendered as a badge
AUDIT_FRESHNESS = [_("Never audited"), _("OVERDUE"), _("OK")]
EXPIRY_STATUSES = [_("EXPIRED"), _("EXPIRING SOON"), _("OK")]
# retention contact_method / boarding wellness_contact_method
CONTACT_METHODS = [_("Phone Call"), _("Text Message"), _("Message")]
