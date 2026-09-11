"""Display labels for values that are STORED in the database in English.

Wrapping a template literal does nothing for these: the text on the page came
out of a row, not out of the markup. `app.py`'s `|tr` filter looks the stored
value up in the translation catalogue, and this module is what makes the
values *extractable* — `pybabel` can only see strings that appear in a
`_()` call somewhere in the source, and a value that only ever exists as a
database row appears nowhere.

Nothing here is imported for its value; the calls exist so extraction finds
them. `lazy_gettext` rather than `gettext` because this runs at import time,
outside any request, where there is no locale to resolve yet.

**Never change a value on the left.** These strings are the stored constants
that routes validate against and that CHECK constraints enforce — translating
the *stored* value would break both. Only the display goes through `|tr`.
"""
# Aliased to `_` rather than `_l` because pybabel's default keyword list
# contains `_` and not `_l` — under the other name every string in this
# file is invisible to extraction, which is exactly what happened first
# time and showed up as a catalogue that did not grow.
from flask_babel import lazy_gettext as _

# visits.case_status — schema_postgres.sql CHECK
CASE_STATUSES = [
    _("Needs Filling"), _("Ongoing"), _("Admitted to Inpatient"),
    _("Deceased/Euthanized"), _("Lost to Follow Up"), _("Resolved"), _("Referred"),
]

# visits.payment_status, as computed by logic.compute_bill_totals()
PAYMENT_STATUSES = [_("Unpaid"), _("Partially Paid"), _("Fully Paid"), _("N/A")]

# visits.visit_type
VISIT_TYPES = [_("Outpatient"), _("Inpatient")]

# visits.followup_* and the grooming/wellness worklists
FOLLOWUP_METHODS = [_("Physical Visit"), _("Phone Call")]
FOLLOWUP_STATUSES = [_("Pending"), _("Completed"), _("Cancelled"), _("N/A")]
FOLLOWUP_REASONS = [
    _("Surgery Follow Up"), _("Medical Follow Up"), _("Vaccine"),
    _("Deworming"), _("Spot On"), _("Other"),
]
WELLNESS_TYPES = [
    _("Annual Vaccine"), _("First Vaccine"), _("Rabies Vaccine"),
    _("Deworming"), _("Spot On/Pill"),
]
GROOMING_STATUSES = [_("Pending"), _("In Progress"), _("Finished")]

# patients — free text in practice, but these are what the form offers
SPECIES = [_("Dog"), _("Cat"), _("Bird"), _("Rabbit"), _("Turtle"), _("Other")]
SEXES = [_("Male"), _("Female")]
REPRO_STATUSES = [_("Intact"), _("Neutered"), _("Spayed")]
HOUSING = [_("Indoor"), _("Outdoor"), _("Indoor/Outdoor"), _("Stray")]

# money and stock
PAYMENT_METHODS = [_("Cash"), _("Card"), _("Transfer")]
BILLING_TYPES = [_("Automatic"), _("Manual")]
PRICE_CATEGORIES = [_("Service"), _("Medicine"), _("Retail")]
INVENTORY_CATEGORIES = [_("Medical"), _("Retail")]
OWNERSHIP_TYPES = [_("Owned"), _("Consignment")]
SHRINKAGE_REASONS = [_("Damaged"), _("Expired"), _("Other")]
LIABLE_PARTIES = [_("Distributor"), _("Clinic")]
AUDIT_STATUSES = [_("Draft"), _("Confirmed")]
CASH_AUDIT_STATUSES = [_("Deficit"), _("Surplus"), _("Perfect")]
APPOINTMENT_TYPES = [_("Medical"), _("Grooming")]
BILL_STATUSES = [_("Unpaid"), _("Partially Paid"), _("Paid")]
REFUND_TYPES = [_("retail"), _("service")]
