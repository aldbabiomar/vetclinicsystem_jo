"""
Patient care: owners and their animals, visits and their billing, inpatient cases, boarding stays, grooming, wellness reminders, follow-ups, appointments, and the clinical files attached to a visit or a case.

Split out of app.py. Shared request-layer pieces come from core.py rather than
app.py -- app.py registers this blueprint, so importing from it here would be
circular.

Endpoint names carry the `clinical.` prefix Flask gives every blueprint route:
`url_for("clinical.api_patients_search")`, not `url_for("clinical.api_patients_search")`.
"""

from datetime import date
from datetime import datetime
from datetime import timedelta
import attachments as attach_mod
import auth
import db as dbmod
import logic
import os
import pdf_export
import re

from flask import (
    Blueprint, abort, flash, jsonify, redirect, render_template, request, send_file, send_from_directory, session, url_for
)

from core import BadDate, BadNumber, BadPhone, CLEANUP_CAP, PER_PAGE, clean_date, cleanup_amount_error, date_filter_arg, discount_percent_error, get_db, get_page, has_negative, normalize_phone, page_count, page_offset, parse_int, parse_money, parse_quantity, required_field

bp = Blueprint("clinical", __name__)


BCS_MIN, BCS_MAX = 1, 9


def parse_bcs(raw):
    """Mirrors the CHECK (bcs BETWEEN 1 AND 9) constraint on visits/
    inpatient_cases so an out-of-range value is a friendly flash rather
    than a raw CheckViolation. See ERROR_500_AUDIT.md E-05."""
    val = parse_int(raw)
    if val is not None and not (BCS_MIN <= val <= BCS_MAX):
        raise BadNumber(f"Body Condition Score must be between {BCS_MIN} and {BCS_MAX}.")
    return val


def stale_edit_error(old_updated_at, submitted_updated_at, what):
    """Optimistic-locking guard for "edit whole record" routes — previously
    last-write-wins with no warning: two staff editing the same record at
    once meant the second save silently erased the first's changes.
    Compares the updated_at the edit form was loaded with against the
    record's current value; a mismatch means someone else saved in
    between. Returns an error string, or None if it's safe to save.
    old_updated_at is None for a row this mechanism has never touched
    (created before this existed, or its very first edit), in which case
    there's nothing to compare against and saving proceeds."""
    if old_updated_at and submitted_updated_at != old_updated_at:
        return (f"This {what} was changed by someone else while you had it open — "
                f"reload the page to see the latest version before saving your changes.")
    return None


class BadMicrochip(ValueError):
    """Raised by normalize_microchip() when a submitted microchip number
    isn't blank but doesn't resemble a chip number at all — lets the route
    redisplay the form with a friendly message rather than saving a typo
    that a scanner lookup will never match."""


# 9-15 rather than a flat 15: ISO 11784/11785 (FDX-B) chips are 15 digits and
# are what a clinic implants today, but animals already carrying an older
# 9-digit (AVID Euro) or 10-digit (AVID / trovan, which can be alphanumeric)
# chip still walk in, and a strict 15-digit rule would make those simply
# unrecordable. The range is wide enough to accept every real chip and narrow
# enough to catch the failure this exists for: a truncated paste or a few
# digits typed by hand.
MICROCHIP_MIN_LENGTH = 9
MICROCHIP_MAX_LENGTH = 15


def normalize_microchip(raw):
    """
    Normalizes a microchip number to bare uppercase alphanumerics. Returns
    None for a blank/optional field, or raises BadMicrochip.

    Normalizing on the way in is what makes the field searchable at all.
    Staff read a 15-digit number off a scanner and type it however it is
    grouped on the screen -- "985 141 000 123456", "985-141-000123456" --
    and each of those stored verbatim is a different string, so searching
    one would not find the others, and the unique index would not see two
    spellings of the same chip as a duplicate. Store one canonical form and
    both problems disappear.
    """
    if raw is None or not str(raw).strip():
        return None
    cleaned = logic.strip_microchip_separators(raw)
    if not re.fullmatch(rf"[A-Z0-9]{{{MICROCHIP_MIN_LENGTH},{MICROCHIP_MAX_LENGTH}}}", cleaned):
        raise BadMicrochip(raw)
    return cleaned


def patient_with_microchip(db, microchip, exclude_patient_id=None):
    """The patient already carrying this chip, or None.

    Checked before writing so staff get "that chip is on Luna's record"
    instead of an IntegrityError from idx_patients_microchip_unique -- but
    the index is what actually enforces it, and the callers still catch the
    violation for the concurrent case this lookup cannot see. Defence in
    depth, the same shape as owners.phone (ERROR_500_AUDIT.md E-12).
    """
    if not microchip:
        return None
    if exclude_patient_id:
        return db.execute(
            "SELECT id, animal_name FROM patients WHERE microchip=? AND id<>?",
            (microchip, exclude_patient_id),
        ).fetchone()
    return db.execute(
        "SELECT id, animal_name FROM patients WHERE microchip=?", (microchip,)
    ).fetchone()


def microchip_taken_message(microchip, row):
    """One wording for both write paths, naming the animal that already holds
    the chip -- "it's taken" alone leaves staff no way to tell a genuine
    duplicate from a mistyped digit."""
    return (f"Microchip {microchip} is already on file for "
            f"{row['animal_name'] or 'another patient'} ({row['id']}).")


def vet_users(db):
    return db.execute("SELECT id, full_name FROM users WHERE role_id IN (SELECT id FROM roles WHERE is_vet_role=true) AND active=true ORDER BY full_name").fetchall()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@bp.route("/api/patients/search")
@auth.permission_required("manage_visits", "manage_boarding", "manage_inpatient")
def api_patients_search():
    db = get_db()
    term = request.args.get("q", "").strip()
    if len(term) < 2:
        return jsonify([])
    rows = logic.search_patients(db, term)
    return jsonify([{"id": r["id"], "animal_name": r["animal_name"], "species": r["species"],
                      "owner_name": r["owner_name"], "owner_phone": r["owner_phone"],
                      "microchip": r["microchip"]} for r in rows])


# ---------------------------------------------------------------------------
# Owners
# ---------------------------------------------------------------------------
@bp.route("/owners")
@auth.permission_required("manage_owners")
def owners_list():
    db = get_db()
    search = request.args.get("q", "").strip()
    page = get_page()
    if search:
        total = db.execute("SELECT COUNT(*) c FROM owners WHERE name ILIKE ? OR phone ILIKE ?",
                            (logic.like_pattern(search), logic.like_pattern(search))).fetchone()["c"]
        rows = db.execute(
            "SELECT * FROM owners WHERE name ILIKE ? OR phone ILIKE ? ORDER BY name LIMIT ? OFFSET ?",
            (logic.like_pattern(search), logic.like_pattern(search), PER_PAGE, page_offset(page)),
        ).fetchall()
    else:
        total = db.execute("SELECT COUNT(*) c FROM owners").fetchone()["c"]
        rows = db.execute("SELECT * FROM owners ORDER BY name LIMIT ? OFFSET ?",
                          (PER_PAGE, page_offset(page))).fetchall()
    counts = {r["owner_id"]: r["c"] for r in db.execute("SELECT owner_id, COUNT(*) c FROM patients GROUP BY owner_id").fetchall()}
    return render_template("owners_list.html", owners=rows, search=search, counts=counts,
                            page=page, total_pages=page_count(total), total_count=total)


@bp.route("/owners/new", methods=["GET", "POST"])
@auth.permission_required("manage_owners")
def owner_new():
    db = get_db()
    if request.method == "POST":
        f = request.form
        try:
            phone = normalize_phone(f.get("phone"))
        except BadPhone:
            flash("That phone number doesn't look valid — check the digits and try again.", "error")
            return render_template("owner_form.html", owner=None, form=f)
        # Friendly fast-path — not the real guarantee (see the IntegrityError
        # catch below for that): an owner with this phone already on file
        # almost always means "add another pet to them", not "make a new
        # owner", so send staff straight there instead of a duplicate row.
        if phone:
            existing = db.execute("SELECT id FROM owners WHERE phone=?", (phone,)).fetchone()
            if existing:
                flash(f"Owner {existing['id']} already has this phone number on file — "
                      f"add the pet to them instead of creating a new owner.", "error")
                return redirect(url_for("clinical.owner_detail", owner_id=existing["id"]))
        name = required_field(f, "name", "Owner name")
        if name is None:
            return render_template("owner_form.html", owner=None, form=f)
        oid = dbmod.next_id(db, "OW")
        try:
            db.execute("INSERT INTO owners (id,name,phone,address,notes) VALUES (?,?,?,?,?)",
                      (oid, name, phone, f.get("address"), f.get("notes")))
            auth.log_change(db, "owners", oid, "create")
            db.commit()
        except dbmod.IntegrityError:
            # The pre-check above is best-effort, not atomic — two
            # near-simultaneous submits for the same new phone number can
            # both pass it before either commits. idx_owners_phone_unique
            # (schema_postgres.sql) is what actually prevents the
            # duplicate; this catches the resulting IntegrityError for
            # whichever request loses that race.
            db.rollback()
            existing = db.execute("SELECT id FROM owners WHERE phone=?", (phone,)).fetchone()
            if existing:
                flash(f"Owner {existing['id']} already has this phone number on file — "
                      f"add the pet to them instead of creating a new owner.", "error")
                return redirect(url_for("clinical.owner_detail", owner_id=existing["id"]))
            flash("That phone number is already on file for another owner.", "error")
            return render_template("owner_form.html", owner=None, form=f)
        flash(f"Owner {oid} added.", "success")
        return redirect(url_for("clinical.owner_detail", owner_id=oid))
    return render_template("owner_form.html", owner=None)


@bp.route("/owners/<owner_id>")
@auth.permission_required("manage_owners")
def owner_detail(owner_id):
    db = get_db()
    owner = db.execute("SELECT * FROM owners WHERE id=?", (owner_id,)).fetchone()
    if not owner:
        flash("Owner not found.", "error")
        return redirect(url_for("clinical.owners_list"))
    patients = db.execute("SELECT * FROM patients WHERE owner_id=? ORDER BY animal_name", (owner_id,)).fetchall()
    return render_template("owner_detail.html", owner=owner, patients=patients)


@bp.route("/owners/<owner_id>/edit", methods=["GET", "POST"])
@auth.permission_required("manage_owners")
def owner_edit(owner_id):
    db = get_db()
    owner = db.execute("SELECT * FROM owners WHERE id=?", (owner_id,)).fetchone()
    if not owner:
        flash("Owner not found.", "error")
        return redirect(url_for("clinical.owners_list"))
    if request.method == "POST":
        f = request.form
        try:
            phone = normalize_phone(f.get("phone"))
        except BadPhone:
            flash("That phone number doesn't look valid — check the digits and try again.", "error")
            return render_template("owner_form.html", owner=owner, form=f)
        name = required_field(f, "name", "Owner name")
        if name is None:
            return render_template("owner_form.html", owner=owner, form=f)
        new_vals = {"name": name, "phone": phone, "address": f.get("address"), "notes": f.get("notes")}
        changes = auth.diff_dict(owner, new_vals)
        db.execute("UPDATE owners SET name=?, phone=?, address=?, notes=? WHERE id=?",
                  (new_vals["name"], new_vals["phone"], new_vals["address"], new_vals["notes"], owner_id))
        auth.log_change(db, "owners", owner_id, "update", changes)
        db.commit()
        flash("Owner updated.", "success")
        return redirect(url_for("clinical.owner_detail", owner_id=owner_id))
    return render_template("owner_form.html", owner=owner)


# ---------------------------------------------------------------------------
# Patients (sortable)
# ---------------------------------------------------------------------------
PATIENT_SORT_COLUMNS = {
    "id": "p.id", "animal_name": "p.animal_name", "species": "p.species", "owner": "o.name",
}


@bp.route("/patients")
@auth.permission_required("manage_patients")
def patients_list():
    db = get_db()
    search = request.args.get("q", "").strip()
    sort = request.args.get("sort", "id")
    direction = request.args.get("dir", "desc" if sort == "id" else "asc")
    sort_col = PATIENT_SORT_COLUMNS.get(sort, "p.id")
    direction_sql = "DESC" if direction == "desc" else "ASC"
    page = get_page()

    if search:
        # search_patients() is already capped to the top 25 best matches —
        # a single page's worth, so no further pagination needed here.
        rows = logic.search_patients(db, search)
        total = len(rows)
        total_pages_ = 1
    else:
        total = db.execute("SELECT COUNT(*) c FROM patients").fetchone()["c"]
        rows = db.execute(
            f"SELECT p.*, o.name as owner_name, o.phone as owner_phone FROM patients p "
            f"JOIN owners o ON o.id=p.owner_id ORDER BY {sort_col} {direction_sql} LIMIT ? OFFSET ?",
            (PER_PAGE, page_offset(page)),
        ).fetchall()
        total_pages_ = page_count(total)
    return render_template("patients_list.html", patients=rows, search=search, sort=sort, direction=direction,
                            page=page, total_pages=total_pages_, total_count=total)


@bp.route("/patients/<patient_id>")
@auth.permission_required("manage_patients")
def patient_detail(patient_id):
    db = get_db()
    patient = db.execute(
        "SELECT p.*, o.name as owner_name, o.phone as owner_phone, o.id as owner_id FROM patients p "
        "JOIN owners o ON o.id=p.owner_id WHERE p.id=?", (patient_id,)
    ).fetchone()
    if not patient:
        flash("Patient not found.", "error")
        return redirect(url_for("clinical.patients_list"))
    visits = db.execute("SELECT * FROM visits WHERE patient_id=? ORDER BY date DESC", (patient_id,)).fetchall()
    visits = [dict(v) for v in visits]
    for v in visits:
        v["billing"] = logic.visit_billing_summary(db, v["id"])
    grooming_sessions = [v for v in visits if v["grooming_needed"] == "Y"]
    cases = db.execute("SELECT * FROM inpatient_cases WHERE patient_id=? ORDER BY admission_date DESC", (patient_id,)).fetchall()
    boarding_sessions = logic.boarding_sessions_for_patient(db, patient_id)
    return render_template("patient_detail.html", patient=patient, visits=visits, cases=cases,
                            grooming_sessions=grooming_sessions, boarding_sessions=boarding_sessions)


@bp.route("/patients/<patient_id>/edit", methods=["GET", "POST"])
@auth.permission_required("manage_patients")
def patient_edit(patient_id):
    db = get_db()
    patient = db.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
    if not patient:
        flash("Patient not found.", "error")
        return redirect(url_for("clinical.patients_list"))
    if request.method == "POST":
        f = request.form

        def redisplay():
            return render_template("patient_form_edit.html", patient=patient, form=f)

        animal_name = required_field(f, "animal_name", "Pet name")
        if animal_name is None:
            return redisplay()
        species = required_field(f, "species", "Species")
        if species is None:
            return redisplay()
        try:
            microchip = normalize_microchip(f.get("microchip"))
        except BadMicrochip:
            flash("That microchip number doesn't look valid — check the digits and try again.", "error")
            return redisplay()
        # Excluding this patient matters: re-saving the form without touching
        # the chip would otherwise report the animal as a duplicate of itself.
        held_by = patient_with_microchip(db, microchip, exclude_patient_id=patient_id)
        if held_by:
            flash(microchip_taken_message(microchip, held_by), "error")
            return redisplay()
        new_vals = {"animal_name": animal_name, "species": species, "sex": f.get("sex"),
                    "age_note": f.get("age_note"), "repro_status": f.get("repro_status"),
                    "housing": f.get("housing"), "microchip": microchip, "notes": f.get("notes")}
        changes = auth.diff_dict(patient, new_vals)
        try:
            db.execute(
                "UPDATE patients SET animal_name=?, species=?, sex=?, age_note=?, repro_status=?, "
                "housing=?, microchip=?, notes=? WHERE id=?",
                (*new_vals.values(), patient_id),
            )
        except dbmod.IntegrityError:
            # idx_patients_microchip_unique is what actually enforces this;
            # the check above is not atomic and a concurrent save can win the
            # race. Without this the update 500s on a duplicate chip.
            db.rollback()
            flash("That microchip number is already on another patient's record.", "error")
            return redisplay()
        auth.log_change(db, "patients", patient_id, "update", changes)
        db.commit()
        flash("Patient updated.", "success")
        return redirect(url_for("clinical.patient_detail", patient_id=patient_id))
    return render_template("patient_form_edit.html", patient=patient)


@bp.route("/patients/<patient_id>/history")
@auth.permission_required("manage_patients")
def patient_history(patient_id):
    db = get_db()
    patient = db.execute(
        "SELECT p.*, o.name as owner_name FROM patients p JOIN owners o ON o.id=p.owner_id WHERE p.id=?", (patient_id,)
    ).fetchone()
    if not patient:
        flash("Patient not found.", "error")
        return redirect(url_for("clinical.patients_list"))
    events = logic.patient_history(db, patient_id)
    return render_template("patient_history.html", patient=patient, events=events)


@bp.route("/patients/<patient_id>/export/file")
@auth.permission_required("manage_patients")
def patient_export_file(patient_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM patients WHERE id=?", (patient_id,)).fetchone():
        abort(404)
    buf = pdf_export.export_patient_file(db, patient_id)
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=f"{patient_id}_patient_file.pdf")


@bp.route("/patients/<patient_id>/export/billing")
@auth.permission_required("manage_patients")
def patient_export_billing(patient_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM patients WHERE id=?", (patient_id,)).fetchone():
        abort(404)
    buf = pdf_export.export_patient_billing(db, patient_id)
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=f"{patient_id}_billing.pdf")


@bp.route("/visits/<visit_id>/export")
@auth.permission_required("manage_visits")
def visit_export_pdf(visit_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM visits WHERE id=?", (visit_id,)).fetchone():
        abort(404)
    buf = pdf_export.export_visit_pdf(db, visit_id)
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=f"{visit_id}_visit.pdf")


@bp.route("/inpatient/<int:case_id>/export")
@auth.permission_required("manage_inpatient")
def inpatient_export_pdf(case_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM inpatient_cases WHERE id=?", (case_id,)).fetchone():
        abort(404)
    buf = pdf_export.export_inpatient_pdf(db, case_id)
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=f"inpatient_{case_id}.pdf")


# ---------------------------------------------------------------------------
# New Visit workflow
# ---------------------------------------------------------------------------
CASE_STATUSES = ["Needs Filling", "Ongoing", "Admitted to Inpatient", "Deceased/Euthanized",
                  "Lost to Follow Up", "Resolved", "Referred"]
FOLLOWUP_REASONS = ["Surgery Follow Up", "Medical Follow Up", "Vaccine", "Deworming", "Spot On", "Other"]
WELLNESS_TYPES = ["Annual Vaccine", "First Vaccine", "Rabies Vaccine", "Deworming", "Spot On/Pill"]
GROOMING_SERVICES = logic.GROOMING_SERVICES


@bp.route("/visits/new")
@auth.permission_required("manage_visits")
def visit_new_start():
    return render_template("visit_new_start.html")


@bp.route("/visits/new/existing", methods=["GET", "POST"])
@auth.permission_required("manage_visits")
def visit_new_existing():
    db = get_db()
    if request.method == "POST":
        patient_id = request.form.get("patient_id", "").strip()
        patient_row = db.execute(
            "SELECT p.animal_name, o.name as owner_name FROM patients p JOIN owners o ON o.id=p.owner_id WHERE p.id=?",
            (patient_id,),
        ).fetchone() if patient_id else None
        if not patient_id or not patient_row:
            flash("Pick a patient from the search results before logging a visit.", "error")
            return redirect(url_for("clinical.visit_new_existing"))
        try:
            vid = _create_visit(db, patient_id, request.form)
        except (BadDate, BadNumber) as e:
            flash(str(e) if isinstance(e, BadDate) else "Weight and BCS must be valid numbers.", "error")
            return render_template(
                "visit_new_existing.html", vets=vet_users(db), wellness_types=WELLNESS_TYPES,
                grooming_services=GROOMING_SERVICES, form=request.form, selected_patient_id=patient_id,
                selected_patient_label=f"{patient_row['animal_name']} — {patient_row['owner_name']} ({patient_id})",
            )
        return redirect(url_for("clinical.visit_detail", visit_id=vid))
    return render_template("visit_new_existing.html", vets=vet_users(db), wellness_types=WELLNESS_TYPES,
                            grooming_services=GROOMING_SERVICES)


@bp.route("/visits/new/new-patient", methods=["GET", "POST"])
@auth.permission_required("manage_visits")
def visit_new_patient():
    db = get_db()
    if request.method == "POST":
        f = request.form
        def redisplay():
            return render_template("visit_new_patient.html", vets=vet_users(db), wellness_types=WELLNESS_TYPES,
                                    grooming_services=GROOMING_SERVICES, form=f)
        try:
            owner_phone = normalize_phone(f.get("owner_phone"))
        except BadPhone:
            flash("That owner phone number doesn't look valid — check the digits and try again.", "error")
            return redisplay()
        try:
            _parse_visit_fields(f)
        except BadDate as e:
            flash(str(e), "error")
            return redisplay()
        except BadNumber:
            flash("Weight and BCS must be valid numbers.", "error")
            return redisplay()
        owner_name = required_field(f, "owner_name", "Owner name")
        if owner_name is None:
            return redisplay()
        animal_name = required_field(f, "animal_name", "Pet name")
        if animal_name is None:
            return redisplay()
        species = required_field(f, "species", "Species")
        if species is None:
            return redisplay()
        # Validated here, with the other fields, rather than at the INSERT
        # below: everything from this point on writes: the owner row, the
        # patient row and the visit. Failing on the chip afterwards would mean
        # rolling all of that back after the fact, and this form carries a
        # whole visit's worth of typing.
        try:
            microchip = normalize_microchip(f.get("microchip"))
        except BadMicrochip:
            flash("That microchip number doesn't look valid — check the digits and try again.", "error")
            return redisplay()
        held_by = patient_with_microchip(db, microchip)
        if held_by:
            flash(microchip_taken_message(microchip, held_by), "error")
            return redisplay()

        # This form is meant for a genuinely new owner+pet — but nothing
        # stopped staff from re-entering an existing owner's exact
        # name+phone while adding another one of their pets, silently
        # creating a duplicate owner row instead of linking to the
        # existing one. If this phone is already on file, add the new
        # pet under that existing owner instead of making a second one
        # (idx_owners_phone_unique in schema_postgres.sql would otherwise
        # just reject the insert outright, and staff have already filled
        # in the whole visit form by this point — losing that work to a
        # hard error would be a worse experience than quietly reusing the
        # existing owner, which is what they almost always actually meant).
        existing_owner = db.execute("SELECT id FROM owners WHERE phone=?", (owner_phone,)).fetchone() if owner_phone else None
        if existing_owner:
            oid = existing_owner["id"]
            flash(f"Owner {oid} already has this phone number on file — the new pet was added to their existing profile.", "success")
        else:
            oid = dbmod.next_id(db, "OW")
            try:
                db.execute("INSERT INTO owners (id,name,phone,address) VALUES (?,?,?,?)",
                          (oid, owner_name, owner_phone, f.get("owner_address")))
                auth.log_change(db, "owners", oid, "create")
            except dbmod.IntegrityError:
                # Best-effort check above isn't atomic — a concurrent
                # submit for the same new phone number can win the race.
                # idx_owners_phone_unique is what actually prevents the
                # duplicate; fall back to the owner that won. But the
                # violation might not be that constraint at all (e.g. a
                # NOT NULL on owners.name from a blank owner_name), and
                # owner_phone can legitimately be None (phone is optional)
                # — in either case the SELECT below finds nothing, so it
                # must be guarded rather than assumed to succeed, mirroring
                # owner_new()'s own handling of this same race. See
                # ERROR_500_AUDIT.md E-12 / ORPHANED_RECORDS_AUDIT.md F-03.
                db.rollback()
                existing = db.execute("SELECT id FROM owners WHERE phone=?", (owner_phone,)).fetchone() \
                    if owner_phone else None
                if not existing:
                    flash("That owner couldn't be saved — check the name and phone number "
                          "and try again.", "error")
                    return redisplay()
                oid = existing["id"]
                flash(f"Owner {oid} already has this phone number on file — the new pet was added to their existing profile.", "success")

        pid = dbmod.next_id(db, "PT")
        try:
            db.execute(
                "INSERT INTO patients (id,owner_id,animal_name,species,sex,age_note,repro_status,housing,microchip) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (pid, oid, animal_name, species, f.get("sex"), f.get("age_note"),
                 f.get("repro_status"), f.get("housing"), microchip),
            )
        except dbmod.IntegrityError:
            # The pre-check above is not atomic; idx_patients_microchip_unique
            # is. Rolling back also undoes the owner INSERT a few lines up and
            # returns the PT id counter, so a retry leaves nothing behind —
            # which is the point: a half-written owner with no patient is
            # exactly the orphan shape ORPHANED_RECORDS_AUDIT.md F-03 covers.
            db.rollback()
            flash("That microchip number is already on another patient's record.", "error")
            return redisplay()
        auth.log_change(db, "patients", pid, "create")
        db.commit()

        # _parse_visit_fields(f) above already validated date/weight/bcs
        # before the owner+patient writes/commit above, so this can no
        # longer raise BadDate/BadNumber on a well-formed request.
        vid = _create_visit(db, pid, f)
        return redirect(url_for("clinical.visit_detail", visit_id=vid))
    return render_template("visit_new_patient.html", vets=vet_users(db), wellness_types=WELLNESS_TYPES,
                            grooming_services=GROOMING_SERVICES)


def _parse_visit_fields(f):
    """Parses/validates the visit-level fields (date, weight, bcs, wellness
    next-dose date), raising BadDate/BadNumber on bad input. Pure — no DB
    writes. Split out of _create_visit() so a caller that must create
    prerequisite records first (owner+patient, in visit_new_patient()) can
    validate the visit fields BEFORE committing those, instead of
    committing them and only then discovering the visit itself is invalid
    (which used to leave an orphaned owner+patient behind on retry — see
    visit_new_patient()). Also fixes a latent bug in the old inline
    try/except this replaced: _create_visit() used to `return redirect(...)`
    from inside itself on a validation failure, which — since its return
    value is assigned to `vid` by every caller — meant a validation error
    silently produced `vid = <a Response object>`, then
    `url_for('clinical.visit_detail', visit_id=vid)` on that. Both call sites now
    catch BadDate/BadNumber themselves instead."""
    visit_date = clean_date(f.get("date"), field="date") or date.today().isoformat()
    wellness_needed = f.get("wellness_needed", "N")
    grooming_needed = f.get("grooming_needed", "N")
    weight_kg = parse_money(f.get("weight_kg"))
    bcs = parse_bcs(f.get("bcs"))
    # A negative weight is not a real measurement. IQ has always rejected it
    # here; JO did not, so it reached the chart and every trend built on it.
    if has_negative(weight_kg):
        raise BadNumber(f.get("weight_kg"))
    wellness_next_dose_date = (
        clean_date(f.get("wellness_next_dose_date"), field="wellness_next_dose_date")
        if wellness_needed == "Y" else None
    )
    return visit_date, weight_kg, bcs, wellness_needed, grooming_needed, wellness_next_dose_date


def _create_visit(db, patient_id, f):
    vid = dbmod.next_id(db, "V")
    admit_now = f.get("admit_inpatient") == "on"
    visit_date, weight_kg, bcs, wellness_needed, grooming_needed, wellness_next_dose_date = _parse_visit_fields(f)
    grooming_services = ",".join(f.getlist("grooming_services")) if grooming_needed == "Y" else None

    db.execute(
        """INSERT INTO visits (id,patient_id,visit_type,date,doctor,weight_kg,bcs,complaint,history,exam,treatment,
           case_status,case_status_changed_at,updates_log,
           followup_needed,followup_method,followup_reason,followup_date,followup_status,
           wellness_needed,wellness_type,wellness_next_dose_date,wellness_contacted,wellness_contact_method,
           grooming_needed,grooming_services,grooming_notes,grooming_admitted_items,grooming_status,grooming_contacted,
           payment_status,created_by)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (vid, patient_id, "Inpatient" if admit_now else "Outpatient", visit_date,
         f.get("doctor"), weight_kg, bcs, f.get("complaint"), f.get("history"), None, None,
         "Admitted to Inpatient" if admit_now else "Needs Filling", visit_date, None,
         "N", None, None, None, "N/A",
         wellness_needed, f.get("wellness_type") if wellness_needed == "Y" else None,
         wellness_next_dose_date, "N", None,
         grooming_needed, grooming_services, f.get("grooming_notes") if grooming_needed == "Y" else None,
         f.get("grooming_admitted_items") if grooming_needed == "Y" else None,
         "Waiting" if grooming_needed == "Y" else None, "N",
         "N/A", session.get("user_id")),
    )
    auth.log_change(db, "visits", vid, "create")
    if admit_now:
        _create_inpatient_case(db, patient_id, vid, f.get("complaint"), visit_date, weight_kg, bcs)
    db.commit()
    return vid


def _create_inpatient_case(db, patient_id, visit_id, complaint, admission_date, weight_kg=None, bcs=None):
    cur = db.execute(
        "INSERT INTO inpatient_cases (patient_id, visit_id, complaint, admission_date, weight_kg, bcs, dismissed, created_by) VALUES (?,?,?,?,?,?,false,?) RETURNING id",
        (patient_id, visit_id, complaint, admission_date or date.today().isoformat(), weight_kg, bcs, session.get("user_id")),
    )
    case_id = cur.fetchone()["id"]
    auth.log_change(db, "inpatient_cases", str(case_id), "create")
    return case_id


# ---------------------------------------------------------------------------
# Visits (sortable + date filter)
# ---------------------------------------------------------------------------
@bp.route("/visits")
@auth.permission_required("manage_visits")
def visits_list():
    db = get_db()
    sort = request.args.get("sort", "date")
    day_filter = date_filter_arg()
    search = request.args.get("q", "").strip()
    page = get_page()

    from_join = "FROM visits v JOIN patients p ON p.id=v.patient_id JOIN owners o ON o.id=p.owner_id"
    params = []
    where = []
    if day_filter:
        where.append("v.date=?")
        params.append(day_filter)
    if search:
        where.append("(p.animal_name ILIKE ? OR o.name ILIKE ?)")
        params.extend([logic.like_pattern(search), logic.like_pattern(search)])
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    total = db.execute(f"SELECT COUNT(*) c {from_join}{where_sql}", params).fetchone()["c"]

    order_map = {
        "date": "v.date DESC, v.id DESC",
        "type": "v.visit_type ASC, v.date DESC",
        "status": "v.case_status ASC, v.date DESC",
        "payment": "v.payment_status ASC, v.date DESC",
    }
    q = f"SELECT v.*, p.animal_name, o.name as owner_name {from_join}{where_sql}"
    q += " ORDER BY " + order_map.get(sort, order_map["date"])
    q += " LIMIT ? OFFSET ?"

    rows = [dict(r) for r in db.execute(q, params + [PER_PAGE, page_offset(page)]).fetchall()]
    for r in rows:
        r["billing"] = logic.visit_billing_summary(db, r["id"])
    return render_template("visits_list.html", visits=rows, sort=sort, day_filter=day_filter or "", search=search,
                            page=page, total_pages=page_count(total), total_count=total)


def _visit_detail_context(db, visit_id):
    visit = db.execute(
        "SELECT v.*, p.animal_name, p.id as patient_id, o.name as owner_name, o.phone as owner_phone FROM visits v "
        "JOIN patients p ON p.id=v.patient_id JOIN owners o ON o.id=p.owner_id WHERE v.id=?", (visit_id,)
    ).fetchone()
    if not visit:
        return None
    billing_row = db.execute("SELECT * FROM billing WHERE visit_id=?", (visit_id,)).fetchone()
    summary = logic.visit_billing_summary(db, visit_id)
    payments = db.execute("SELECT * FROM payments WHERE visit_id=? ORDER BY date DESC", (visit_id,)).fetchall()
    files = attach_mod.list_attachments(db, "visit", visit_id)
    cap = auth.discount_cap_for()
    return dict(visit=visit, billing=billing_row, summary=summary, payments=payments, files=files, discount_cap=cap)


@bp.route("/visits/<visit_id>")
@auth.permission_required("manage_visits")
def visit_detail(visit_id):
    db = get_db()
    ctx = _visit_detail_context(db, visit_id)
    if ctx is None:
        flash("Visit not found.", "error")
        return redirect(url_for("clinical.visits_list"))
    return render_template("visit_detail.html", **ctx)


@bp.route("/visits/<visit_id>/edit", methods=["GET", "POST"])
@auth.permission_required("manage_visits")
def visit_edit(visit_id):
    db = get_db()
    visit = db.execute("SELECT * FROM visits WHERE id=?", (visit_id,)).fetchone()
    if not visit:
        flash("Visit not found.", "error")
        return redirect(url_for("clinical.visits_list"))
    if request.method == "POST":
        f = request.form

        def redisplay():
            return render_template("visit_form_edit.html", visit=visit, case_statuses=CASE_STATUSES,
                                    followup_reasons=FOLLOWUP_REASONS, wellness_types=WELLNESS_TYPES,
                                    grooming_services=GROOMING_SERVICES, vets=vet_users(db), form=f)

        conflict = stale_edit_error(visit["updated_at"], f.get("expected_updated_at"), "visit")
        if conflict:
            flash(conflict, "error")
            return redisplay()
        wellness_needed = f.get("wellness_needed", "N")
        grooming_needed = f.get("grooming_needed", "N")
        grooming_services = ",".join(f.getlist("grooming_services")) if grooming_needed == "Y" else None
        new_case_status = f.get("case_status", visit["case_status"])
        if new_case_status not in CASE_STATUSES:
            flash("Case status must be one of: " + ", ".join(CASE_STATUSES) + ".", "error")
            return redisplay()
        new_visit_type = f.get("visit_type")
        if new_visit_type not in ("Outpatient", "Inpatient"):
            flash("Visit type must be Outpatient or Inpatient.", "error")
            return redisplay()
        status_changed_at = visit["case_status_changed_at"]
        if new_case_status != visit["case_status"]:
            status_changed_at = date.today().isoformat()

        try:
            edited_date = clean_date(f.get("date"), field="date")
            edited_followup_date = clean_date(f.get("followup_date"), field="followup_date")
            edited_wellness_next_dose_date = clean_date(f.get("wellness_next_dose_date"), field="wellness_next_dose_date") if wellness_needed == "Y" else None
            edited_weight_kg = parse_money(f.get("weight_kg"))
            edited_bcs = parse_bcs(f.get("bcs"))
        except BadDate as e:
            flash(str(e), "error")
            return redisplay()
        except BadNumber:
            flash("Weight and BCS must be valid numbers.", "error")
            return redisplay()
        if has_negative(edited_weight_kg):
            flash("Weight can't be negative.", "error")
            return redisplay()

        new_vals = {
            "visit_type": new_visit_type, "date": edited_date, "doctor": f.get("doctor"),
            "weight_kg": edited_weight_kg, "bcs": edited_bcs,
            "complaint": f.get("complaint"), "history": f.get("history"), "exam": f.get("exam"),
            "treatment": f.get("treatment"), "case_status": new_case_status, "case_status_changed_at": status_changed_at,
            "updates_log": f.get("updates_log"),
            "followup_needed": f.get("followup_needed", "N"), "followup_method": f.get("followup_method") or None,
            "followup_reason": f.get("followup_reason") or None, "followup_date": edited_followup_date,
            "followup_status": f.get("followup_status", "N/A"),
            "wellness_needed": wellness_needed, "wellness_type": f.get("wellness_type") if wellness_needed == "Y" else None,
            "wellness_next_dose_date": edited_wellness_next_dose_date,
            "wellness_contacted": f.get("wellness_contacted", "N"), "wellness_contact_method": f.get("wellness_contact_method") or None,
            "grooming_needed": grooming_needed, "grooming_services": grooming_services,
            "grooming_notes": f.get("grooming_notes") if grooming_needed == "Y" else None,
            "grooming_admitted_items": f.get("grooming_admitted_items") if grooming_needed == "Y" else None,
            "grooming_status": (f.get("grooming_status") or "Waiting") if grooming_needed == "Y" else None,
            "grooming_contacted": f.get("grooming_contacted", "N"),
            "payment_status": f.get("payment_status", "N/A"),
        }
        # An inpatient_cases row is only ever created at visit-creation time
        # (when admit_inpatient was ticked) — nothing here kept case_status
        # in sync with it. Setting Admitted to Inpatient on an existing
        # visit with no case would make the admission invisible to
        # /inpatient; moving an admitted visit's status away with the case
        # still open would deny the admission ever happened while it sits
        # on the active list forever. See ORPHANED_RECORDS_AUDIT.md F-10.
        was_admitted = visit["case_status"] == "Admitted to Inpatient"
        now_admitted = new_case_status == "Admitted to Inpatient"
        existing_case = db.execute(
            "SELECT id, dismissed FROM inpatient_cases WHERE visit_id=? ORDER BY id DESC LIMIT 1",
            (visit_id,)).fetchone()
        if was_admitted and not now_admitted and existing_case and not existing_case["dismissed"]:
            flash(f"Inpatient case #{existing_case['id']} is still open for this visit — "
                  f"dismiss it there first, or leave the status as Admitted to Inpatient.", "error")
            return redisplay()

        changes = auth.diff_dict(visit, new_vals)
        db.execute(
            """UPDATE visits SET visit_type=?, date=?, doctor=?, weight_kg=?, bcs=?, complaint=?, history=?, exam=?, treatment=?,
               case_status=?, case_status_changed_at=?, updates_log=?, followup_needed=?, followup_method=?,
               followup_reason=?, followup_date=?, followup_status=?, wellness_needed=?, wellness_type=?,
               wellness_next_dose_date=?, wellness_contacted=?, wellness_contact_method=?, grooming_needed=?,
               grooming_services=?, grooming_notes=?, grooming_admitted_items=?, grooming_status=?,
               grooming_contacted=?, payment_status=?, updated_at=? WHERE id=?""",
            (*new_vals.values(), datetime.now().isoformat(timespec="seconds"), visit_id),
        )
        auth.log_change(db, "visits", visit_id, "update", changes)
        if now_admitted and not existing_case:
            _create_inpatient_case(db, visit["patient_id"], visit_id, f.get("complaint"),
                                    edited_date or visit["date"], edited_weight_kg, edited_bcs)
            flash("An inpatient case was opened for this visit.", "success")
        db.commit()
        flash("Visit updated.", "success")
        return redirect(url_for("clinical.visit_detail", visit_id=visit_id))
    return render_template("visit_form_edit.html", visit=visit, case_statuses=CASE_STATUSES,
                            followup_reasons=FOLLOWUP_REASONS, wellness_types=WELLNESS_TYPES,
                            grooming_services=GROOMING_SERVICES, vets=vet_users(db))


@bp.route("/visits/<visit_id>/billing", methods=["POST"])
@auth.permission_required("manage_visits")
def visit_billing_save(visit_id):
    db = get_db()
    f = request.form

    # This form's "Automatic" mode is a JS-built cart (price_id/qty_{id}
    # hidden fields injected client-side — see visit_detail.html), not a
    # simple field list. Restoring that cart's contents on a server-side
    # redisplay would mean re-deriving each line's name/price from the DB
    # and re-hydrating the cart-builder JS's own state, disproportionate
    # effort for this pass (same scoping call made for pos_checkout()).
    # redisplay() below still preserves every plain top-level field
    # (billing type, manual amount, date, notes) — strictly better than
    # today's full-blank redirect, just not a full cart restore.
    def redisplay():
        ctx = _visit_detail_context(db, visit_id)
        if ctx is None:
            flash("Visit not found.", "error")
            return redirect(url_for("clinical.visits_list"))
        return render_template("visit_detail.html", **ctx, form=f)

    # Locked for the same reason visit_discount_save() locks this row —
    # see the comment there. A pure mutex against a concurrent discount
    # save on the same visit; nothing about the visits row itself changes.
    if not db.execute("SELECT id FROM visits WHERE id=? FOR UPDATE", (visit_id,)).fetchone():
        flash("Visit not found.", "error")
        return redirect(url_for("clinical.visits_list"))
    billing_type = f.get("billing_type", "Automatic")
    if billing_type not in BILLING_TYPES:
        flash("Billing type must be one of: " + ", ".join(BILLING_TYPES) + ".", "error")
        return redisplay()
    priced_lines = []
    had_bad_number = had_bad_price = False
    if billing_type == "Automatic":
        # Same pattern as inpatient_billing_add(): each cart row is a
        # validated search-result pick (price_id + qty_{id}), not typed
        # free text, so an invalid price_id here only happens on a
        # tampered request — skipped with a flash rather than a raw
        # database error.
        for pid in f.getlist("price_id"):
            try:
                qty = parse_money(f.get(f"qty_{pid}", "").strip())
            except BadNumber:
                had_bad_number = True
                continue
            if not qty or qty <= 0:
                continue
            price_row = db.execute(
                "SELECT name, category, sale_price, cost_price FROM price_list WHERE id=?", (pid,)
            ).fetchone()
            if not price_row:
                had_bad_price = True
                continue
            priced_lines.append({
                "price_id": pid, "name": price_row["name"], "category": price_row["category"],
                "quantity": qty, "unit_price": price_row["sale_price"], "unit_cost": price_row["cost_price"],
            })
        if not priced_lines:
            flash("Add at least one billed item.", "error")
            return redirect(url_for("clinical.visit_detail", visit_id=visit_id))
        # visit_discount_save() only checks non-discountable items against
        # whatever's on the bill *at the moment a discount is applied* — it
        # has no way to know the bill will change later. Re-checking here
        # too closes the gap where a discount already applied earlier would
        # otherwise silently carry forward onto items added afterward that
        # were never supposed to be discountable at all.
        existing_discount = db.execute(
            "SELECT discount_percent FROM billing WHERE visit_id=?", (visit_id,)
        ).fetchone()
        if existing_discount and (existing_discount["discount_percent"] or 0) > 0:
            blocked = logic.non_discountable_line_names(db, [l["price_id"] for l in priced_lines])
            if blocked:
                flash(f"Can't save — this bill has a {existing_discount['discount_percent']:.0f}% discount applied, "
                      f"but includes item(s) marked as not discountable: {', '.join(blocked)}. "
                      "Remove the discount first, or leave these items off this bill.", "error")
                return redirect(url_for("clinical.visit_detail", visit_id=visit_id))
    try:
        manual_amount = parse_money(f.get("manual_amount")) if billing_type == "Manual" else None
    except BadNumber:
        flash("Manual amount must be a valid number.", "error")
        return redisplay()
    if billing_type == "Manual" and (manual_amount is None or manual_amount <= 0):
        flash("Manual Entry requires a Billed Amount greater than 0.", "error")
        return redisplay()
    try:
        date_billed = clean_date(f.get("date_billed"), field="date_billed")
    except BadDate as e:
        flash(str(e), "error")
        return redisplay()
    if not date_billed:
        # A blank Date Billed used to reach the database as NULL — and
        # logic._revenue_and_cogs_by_month() silently skips any billing row
        # with no date_billed, so the revenue never appears in P&L, forever,
        # with nothing flagging it. Falls back to the visit's own date
        # (itself nullable) or today, same as the template's displayed
        # default. See ORPHANED_RECORDS_AUDIT.md F-06.
        visit_row = db.execute("SELECT date FROM visits WHERE id=?", (visit_id,)).fetchone()
        date_billed = (visit_row["date"] if visit_row else None) or date.today().isoformat()
    notes = f.get("notes")
    existing = db.execute("SELECT * FROM billing WHERE visit_id=?", (visit_id,)).fetchone()
    # Re-saving this bill with a shorter cart (or a smaller Manual amount)
    # can shrink the total below what's already been paid against it —
    # nothing else ever surfaces `paid > total` after that. See
    # ORPHANED_RECORDS_AUDIT.md F-14.
    if existing:
        new_subtotal = manual_amount if billing_type == "Manual" else sum(l["quantity"] * l["unit_price"] for l in priced_lines)
        paid_row = db.execute("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE visit_id=?", (visit_id,)).fetchone()
        new_total, _, _, _ = logic.compute_bill_totals(
            new_subtotal or 0, existing["discount_percent"], 0, existing["cleanup_amount"])
        if paid_row["s"] > new_total:
            flash(f"That change would leave {logic.fmt_money(paid_row['s'])} paid against a "
                  f"{logic.fmt_money(new_total)} JOD bill. Process a service refund for the "
                  f"difference first.", "error")
            return redisplay()
    old_month = logic.month_key(existing["date_billed"]) if existing else None
    # UPSERT rather than a SELECT-then-branch INSERT/UPDATE — visit_id is
    # billing's primary key, so two near-simultaneous saves (double-click,
    # a retried request) racing this as a plain branch could both read no
    # existing row and both attempt an INSERT, the second raising an
    # unhandled UniqueViolation. ON CONFLICT makes the second one an
    # atomic update instead of a crash.
    db.execute(
        "INSERT INTO billing (visit_id, billing_type, manual_amount, date_billed, notes) VALUES (?,?,?,?,?) "
        "ON CONFLICT (visit_id) DO UPDATE SET billing_type=excluded.billing_type, "
        "manual_amount=excluded.manual_amount, date_billed=excluded.date_billed, notes=excluded.notes",
        (visit_id, billing_type, manual_amount, date_billed, notes),
    )
    if billing_type == "Automatic":
        # Snapshot the current Price List values for every item in the
        # cart right now, at Save time — this is what stops a price edit
        # next month from silently changing what this visit's bill (and
        # the revenue/COGS report for the month it was billed) says today.
        logic.save_visit_billing_lines(db, visit_id, priced_lines)
    else:
        # Switched to (or re-saved as) Manual — any prior Automatic
        # snapshot for this visit no longer applies.
        db.execute("DELETE FROM visit_billing_lines WHERE visit_id=?", (visit_id,))
    logic.refresh_visit_billing_total(db, visit_id)
    new_month = logic.month_key(date_billed)
    logic.recompute_months_summary(db, [old_month, new_month])
    auth.log_change(db, "billing", visit_id, "update" if existing else "create")
    db.commit()
    if had_bad_number:
        flash("Some quantities weren't valid numbers and were skipped.", "error")
    if had_bad_price:
        flash("Some selected items no longer exist in the Price List and were skipped.", "error")
    flash("Billing saved.", "success")
    return redirect(url_for("clinical.visit_detail", visit_id=visit_id))


@bp.route("/visits/<visit_id>/discount", methods=["POST"])
@auth.permission_required("manage_visits")
def visit_discount_save(visit_id):
    db = get_db()
    f = request.form

    def redisplay():
        ctx = _visit_detail_context(db, visit_id)
        if ctx is None:
            flash("Visit not found.", "error")
            return redirect(url_for("clinical.visits_list"))
        return render_template("visit_detail.html", **ctx, form=f, discount_error=True)

    try:
        percent = parse_money(f.get("discount_percent")) or 0
    except BadNumber:
        flash("Discount must be a valid number.", "error")
        return redisplay()
    cap = auth.discount_cap_for()
    error = discount_percent_error(percent, cap)
    if error:
        flash(error, "error")
        return redisplay()
    # Locked before checking non-discountable items and before writing the
    # discount below — without this, a concurrent visit_billing_save() for
    # the same visit could read the bill's lines before this request's
    # check but write a new (non-discountable) line after it, and both
    # requests' writes would land having each only validated against a
    # snapshot the other had already invalidated.
    if not db.execute("SELECT id FROM visits WHERE id=? FOR UPDATE", (visit_id,)).fetchone():
        flash("Visit not found.", "error")
        return redirect(url_for("clinical.visits_list"))
    if percent > 0:
        summary = logic.visit_billing_summary(db, visit_id)
        blocked = logic.non_discountable_line_names(db, [l["id"] for l in summary["lines"]])
        if blocked:
            flash(f"Can't apply a discount — this bill includes item(s) marked as not discountable: {', '.join(blocked)}.", "error")
            return redisplay()
    existing = db.execute("SELECT * FROM billing WHERE visit_id=?", (visit_id,)).fetchone()
    if not existing:
        # A discount needs a bill to apply to — the old UPSERT here would
        # otherwise create a childless billing row (total=0, no lines, no
        # date_billed) for a visit that was never billed at all. See
        # ORPHANED_RECORDS_AUDIT.md F-16.
        flash("Save the bill first — a discount needs something to apply to.", "error")
        return redisplay()
    db.execute(
        "UPDATE billing SET discount_percent=?, discount_applied_by=? WHERE visit_id=?",
        (percent, session["user_id"], visit_id),
    )
    logic.refresh_visit_billing_total(db, visit_id)
    if existing and existing["date_billed"]:
        logic.recompute_month_summary(db, logic.month_key(existing["date_billed"]))
    auth.log_change(db, "billing", visit_id, "update", {"discount_percent": (existing["discount_percent"] if existing else 0, percent)})
    db.commit()
    flash(f"{percent:.0f}% discount applied.", "success")
    return redirect(url_for("clinical.visit_detail", visit_id=visit_id))


@bp.route("/visits/<visit_id>/payment", methods=["POST"])
@auth.permission_required("manage_visits")
def visit_payment_add(visit_id):
    db = get_db()
    f = request.form

    def redisplay():
        ctx = _visit_detail_context(db, visit_id)
        if ctx is None:
            flash("Visit not found.", "error")
            return redirect(url_for("clinical.visits_list"))
        return render_template("visit_detail.html", **ctx, form=f, payment_error=True)

    # Locked before computing the balance — same reasoning as
    # boarding_payment(): there's no delete/edit route for a payment once
    # recorded, so an overpayment here can never be undone, only journaled
    # around.
    if not db.execute("SELECT id FROM visits WHERE id=? FOR UPDATE", (visit_id,)).fetchone():
        flash("Visit not found.", "error")
        return redirect(url_for("clinical.visits_list"))
    try:
        amount = parse_money(f.get("amount"), required=True)
    except BadNumber:
        flash("Payment amount must be a valid number.", "error")
        return redisplay()
    if amount <= 0:
        flash("Payment amount must be greater than 0.", "error")
        return redisplay()
    summary = logic.visit_billing_summary(db, visit_id)
    balance = summary["balance"]
    if amount > balance:
        flash(f"That's more than the remaining balance of {logic.fmt_money(balance)} JOD on this visit.", "error")
        return redisplay()
    try:
        cleanup_amount = parse_money(f.get("cleanup_amount")) or 0
    except BadNumber:
        flash("Clean Up amount must be a valid number.", "error")
        return redisplay()
    error = cleanup_amount_error(cleanup_amount, summary["cleanup_amount"], balance)
    if error:
        flash(error, "error")
        return redisplay()
    try:
        payment_date = clean_date(f.get("date"), field="date") or date.today().isoformat()
    except BadDate as e:
        flash(str(e), "error")
        return redisplay()
    cur = db.execute(
        "INSERT INTO payments (visit_id, amount, method, date, user_id, notes) VALUES (?,?,?,?,?,?) RETURNING id",
        (visit_id, amount, f.get("method"), payment_date, session["user_id"], f.get("notes")),
    )
    payment_id = cur.fetchone()["id"]
    auth.log_change(db, "payments", str(payment_id), "create")
    if cleanup_amount > 0:
        db.execute(
            "UPDATE billing SET cleanup_amount = cleanup_amount + ?, cleanup_applied_by = ? WHERE visit_id = ?",
            (cleanup_amount, session["user_id"], visit_id),
        )
        auth.log_change(db, "billing", visit_id, "update", changes={
            "cleanup_amount": (summary["cleanup_amount"], summary["cleanup_amount"] + cleanup_amount)})
        logic.refresh_visit_billing_total(db, visit_id)
    db.commit()
    flash("Payment recorded.", "success")
    return redirect(url_for("clinical.visit_detail", visit_id=visit_id))


@bp.route("/visits/<visit_id>/attachments", methods=["POST"])
@auth.permission_required("manage_visits")
def visit_attachment_upload(visit_id):
    db = get_db()
    patient_row = db.execute("SELECT patient_id FROM visits WHERE id=?", (visit_id,)).fetchone()
    if patient_row is None:
        flash("Visit not found.", "error")
        return redirect(url_for("clinical.visits_list"))
    file = request.files.get("file")
    if not file or not file.filename:
        flash("No file selected.", "error")
        return redirect(url_for("clinical.visit_detail", visit_id=visit_id))
    _, err = attach_mod.save_attachment(db, patient_row["patient_id"], "visit", visit_id, file, session["user_id"])
    flash(err if err else "File uploaded.", "error" if err else "success")
    return redirect(url_for("clinical.visit_detail", visit_id=visit_id))


@bp.route("/files/<path:relpath>")
@auth.permission_required("manage_visits", "manage_inpatient")
def serve_attachment(relpath):
    db = get_db()
    row = db.execute("SELECT relative_path FROM attachments WHERE relative_path=?", (relpath,)).fetchone()
    if row is None:
        flash("File not found.", "error")
        return redirect(url_for("dashboard"))
    disk_path = os.path.join(attach_mod.UPLOAD_ROOT, relpath)
    if not os.path.isfile(disk_path):
        # The DB row is fine — only the file itself is missing (e.g.
        # uploads/ wasn't included in a backup/restore, since it's not
        # part of the database backup at all). send_from_directory would
        # otherwise raise a bare 404 with no indication the record is
        # intact. See ORPHANED_RECORDS_AUDIT.md F-12.
        flash("This file's record exists but the file itself is missing from the uploads "
              "folder — it may not have been included in a backup/restore. "
              "Check with whoever manages backups before re-uploading.", "error")
        return redirect(request.referrer or url_for("dashboard"))
    return send_from_directory(attach_mod.UPLOAD_ROOT, relpath)


@bp.route("/attachments/<int:attachment_id>/delete", methods=["POST"])
@auth.permission_required("manage_visits", "manage_inpatient")
def attachment_delete(attachment_id):
    """
    Deletes one uploaded Additional Test / X-Ray — from both the database
    and the uploads/ folder on disk — and records it in the audit log so
    a removed file still shows up in Admin > Logins and Changes. Shared by
    every place in the app that lists attachments (Visit Detail, Inpatient
    Detail), since a visit's and an inpatient case's attachments both live
    in the same `attachments` table.
    """
    db = get_db()
    row = attach_mod.get_attachment(db, attachment_id)
    if row is None:
        flash("File not found — it may have already been deleted.", "error")
        return redirect(request.referrer or url_for("dashboard"))

    if row["visit_id"]:
        redirect_target = url_for("clinical.visit_detail", visit_id=row["visit_id"])
    elif row["inpatient_case_id"]:
        redirect_target = url_for("clinical.inpatient_detail", case_id=row["inpatient_case_id"])
    else:
        redirect_target = url_for("dashboard")

    deleted, err = attach_mod.delete_attachment(db, attachment_id)
    if err:
        flash(err, "error")
        return redirect(redirect_target)
    if deleted is None:
        flash("File not found — it may have already been deleted.", "error")
        return redirect(redirect_target)
    auth.log_change(db, "attachments", str(attachment_id), "delete")
    db.commit()
    flash(f"Deleted {deleted['original_name']}.", "success")
    return redirect(redirect_target)


# ---------------------------------------------------------------------------
# Follow-ups
# ---------------------------------------------------------------------------
@bp.route("/followups")
@auth.permission_required("manage_followups")
def followups_list():
    db = get_db()
    show_all = request.args.get("all") == "1"
    page = get_page()
    rows, total = logic.followups_page(db, only_pending=not show_all, limit=PER_PAGE, offset=page_offset(page))
    return render_template("followups_list.html", followups=rows, show_all=show_all,
                            page=page, total_pages=page_count(total), total_count=total)


@bp.route("/followups/<visit_id>/status", methods=["POST"])
@auth.permission_required("manage_followups")
def followup_status_update(visit_id):
    db = get_db()
    status = request.form.get("status")
    old = db.execute("SELECT followup_status FROM visits WHERE id=?", (visit_id,)).fetchone()
    if not old:
        flash("Visit not found.", "error")
        return redirect(request.referrer or url_for("clinical.followups_list"))
    db.execute("UPDATE visits SET followup_status=? WHERE id=?", (status, visit_id))
    auth.log_change(db, "visits", visit_id, "update", {"followup_status": (old["followup_status"], status)})
    db.commit()
    flash("Follow-up status updated.", "success")
    return redirect(request.referrer or url_for("clinical.followups_list"))


# ---------------------------------------------------------------------------
# Wellness
# ---------------------------------------------------------------------------
@bp.route("/wellness")
@auth.permission_required("manage_wellness")
def wellness_list():
    db = get_db()
    page = get_page()
    rows, total = logic.wellness_reminders_page(db, limit=PER_PAGE, offset=page_offset(page))
    return render_template("wellness_list.html", rows=rows,
                            page=page, total_pages=page_count(total), total_count=total)


@bp.route("/wellness/<visit_id>/update", methods=["POST"])
@auth.permission_required("manage_wellness")
def wellness_update(visit_id):
    db = get_db()
    f = request.form
    old = db.execute("SELECT wellness_contacted, wellness_contact_method FROM visits WHERE id=?", (visit_id,)).fetchone()
    if not old:
        flash("Visit not found.", "error")
        return redirect(url_for("clinical.wellness_list"))
    db.execute("UPDATE visits SET wellness_contacted=?, wellness_contact_method=? WHERE id=?",
              (f.get("wellness_contacted", "N"), f.get("wellness_contact_method") or None, visit_id))
    auth.log_change(db, "visits", visit_id, "update", {"wellness_contacted": (old["wellness_contacted"], f.get("wellness_contacted", "N"))})
    db.commit()
    flash("Wellness reminder updated.", "success")
    return redirect(url_for("clinical.wellness_list"))


# ---------------------------------------------------------------------------
# Grooming
# ---------------------------------------------------------------------------
@bp.route("/grooming")
@auth.permission_required("manage_grooming")
def grooming_list():
    db = get_db()
    include_finished = request.args.get("all") == "1"
    page = get_page()
    rows, total = logic.grooming_queue_page(db, include_finished=include_finished, limit=PER_PAGE, offset=page_offset(page))
    return render_template("grooming_list.html", rows=rows, include_finished=include_finished,
                            page=page, total_pages=page_count(total), total_count=total)


@bp.route("/grooming/<visit_id>/update", methods=["POST"])
@auth.permission_required("manage_grooming")
def grooming_update(visit_id):
    db = get_db()
    f = request.form
    old = db.execute("SELECT grooming_status, grooming_contacted FROM visits WHERE id=?", (visit_id,)).fetchone()
    if not old:
        flash("Visit not found.", "error")
        return redirect(url_for("clinical.grooming_list"))
    db.execute("UPDATE visits SET grooming_status=?, grooming_contacted=? WHERE id=?",
              (f.get("grooming_status"), f.get("grooming_contacted", "N"), visit_id))
    auth.log_change(db, "visits", visit_id, "update", {"grooming_status": (old["grooming_status"], f.get("grooming_status"))})
    db.commit()
    flash("Grooming entry updated.", "success")
    return redirect(url_for("clinical.grooming_list"))


# Mirror the DB CHECK constraints (schema_postgres.sql) so a bypassed <select>
# produces a clean flash message instead of a raw constraint-violation 500.
RESOURCE_TYPES = ["vet", "grooming"]
APPOINTMENT_TYPES = ["Medical", "Grooming"]
BILLING_TYPES = ["Automatic", "Manual"]


# ---------------------------------------------------------------------------
# Boarding
# ---------------------------------------------------------------------------
def _boarding_page_context(show_all):
    """Builds the template context for boarding.html. Split out of
    boarding_page() so boarding_new()/boarding_edit() can re-render the same
    listing (with `form`/redisplay state layered on top) on a validation
    failure instead of discarding the submitted data via redirect."""
    db = get_db()
    page = get_page()
    count_where = "" if show_all else " WHERE dismissed=false"
    total = db.execute(f"SELECT COUNT(*) c FROM boarding_sessions{count_where}").fetchone()["c"]
    q = ("SELECT b.*, p.animal_name, p.species, o.id AS owner_id, o.name AS owner_name, o.phone AS owner_phone "
         "FROM boarding_sessions b JOIN patients p ON p.id=b.patient_id JOIN owners o ON o.id=p.owner_id")
    if not show_all:
        q += " WHERE b.dismissed=false"
    q += " ORDER BY b.entry_date DESC LIMIT ? OFFSET ?"
    rows = [dict(r) for r in db.execute(q, (PER_PAGE, page_offset(page))).fetchall()]
    # Batched across the whole page instead of a paid-sum + incident-count
    # query per row (boarding_billing_summary() alone was also redundantly
    # re-fetching the boarding_sessions row this page already has) — see
    # logic.boarding_billing_summary_from_fields().
    ids = [r["id"] for r in rows]
    paid_by_id = {}
    incidents_by_id = {}
    if ids:
        placeholders = ",".join("?" * len(ids))
        paid_by_id = {p["boarding_id"]: p["s"] for p in db.execute(
            f"SELECT boarding_id, COALESCE(SUM(amount),0) s FROM payments "
            f"WHERE boarding_id IN ({placeholders}) GROUP BY boarding_id", ids
        ).fetchall()}
        incidents_by_id = {i["boarding_id"]: i["c"] for i in db.execute(
            f"SELECT boarding_id, COUNT(*) c FROM boarding_incidents "
            f"WHERE boarding_id IN ({placeholders}) GROUP BY boarding_id", ids
        ).fetchall()}
    for r in rows:
        r["billing"] = logic.boarding_billing_summary_from_fields(r, paid_by_id.get(r["id"], 0))
        r["incident_count"] = incidents_by_id.get(r["id"], 0)
    return dict(sessions=rows, show_all=show_all, today=date.today().isoformat(),
                page=page, total_pages=page_count(total), total_count=total,
                discount_cap=auth.discount_cap_for())


@bp.route("/boarding")
@auth.permission_required("manage_boarding")
def boarding_page():
    show_all = request.args.get("all") == "1"
    return render_template("boarding.html", **_boarding_page_context(show_all))


@bp.route("/boarding/new", methods=["POST"])
@auth.permission_required("manage_boarding")
def boarding_new():
    db = get_db()
    f = request.form

    def redisplay():
        ctx = _boarding_page_context(show_all=False)
        ctx["form"] = f
        ctx["open_new_form"] = True
        pid = f.get("patient_id")
        if pid:
            prow = db.execute(
                "SELECT p.animal_name, p.species, o.name AS owner_name FROM patients p "
                "JOIN owners o ON o.id=p.owner_id WHERE p.id=?", (pid,),
            ).fetchone()
            if prow:
                ctx["new_form_patient_label"] = f"{prow['animal_name']} — {prow['owner_name']} ({pid})"
        return render_template("boarding.html", **ctx)

    patient_id = f.get("patient_id")
    if not patient_id or not db.execute("SELECT 1 FROM patients WHERE id=?", (patient_id,)).fetchone():
        flash("Pick a patient from the search results first.", "error")
        return redisplay()
    try:
        price_per_day = parse_money(f.get("price_per_day"))
        total = parse_money(f.get("total"))
    except BadNumber:
        flash("Price per Day and Total must be valid numbers.", "error")
        return redisplay()
    if has_negative(price_per_day, total):
        flash("Price per Day and Total can't be negative.", "error")
        return redisplay()
    try:
        entry_date = clean_date(f.get("entry_date"), field="entry_date") or date.today().isoformat()
        dismissal_date = clean_date(f.get("dismissal_date"), field="dismissal_date")
    except BadDate as e:
        flash(str(e), "error")
        return redisplay()
    total_is_auto = total is None
    if total_is_auto:
        total = logic.boarding_suggested_total(price_per_day, entry_date, dismissal_date)
    special_needs = f.get("special_needs") == "on"
    cur = db.execute(
        "INSERT INTO boarding_sessions (patient_id, entry_date, dismissal_date, admitted_items, special_needs, "
        "special_needs_notes, room, price_per_day, total, total_is_auto, dismissed, created_by) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,false,?) RETURNING id",
        (patient_id, entry_date, dismissal_date, f.get("admitted_items"), special_needs,
         f.get("special_needs_notes") if special_needs else None, f.get("room"), price_per_day, total,
         total_is_auto, session.get("user_id")),
    )
    boarding_id = cur.fetchone()["id"]
    logic.refresh_boarding_total(db, boarding_id)
    logic.recompute_month_summary(db, logic.month_key(entry_date))
    auth.log_change(db, "boarding_sessions", str(boarding_id), "create")
    db.commit()
    flash("Boarding session added.", "success")
    return redirect(url_for("clinical.boarding_page"))


@bp.route("/boarding/<int:boarding_id>/edit", methods=["POST"])
@auth.permission_required("manage_boarding")
def boarding_edit(boarding_id):
    db = get_db()
    f = request.form
    old = db.execute("SELECT * FROM boarding_sessions WHERE id=?", (boarding_id,)).fetchone()
    if not old:
        flash("Boarding session not found.", "error")
        return redirect(url_for("clinical.boarding_page"))

    def redisplay():
        # A dismissed session is filtered out of the default "currently
        # boarding" listing — force show_all so the edit row we're
        # restoring is actually present on the redisplayed page.
        ctx = _boarding_page_context(show_all=bool(old["dismissed"]))
        ctx["form"] = f
        ctx["edit_boarding_id"] = boarding_id
        return render_template("boarding.html", **ctx)

    conflict = stale_edit_error(old["updated_at"], f.get("expected_updated_at"), "boarding session")
    if conflict:
        flash(conflict, "error")
        return redisplay()
    try:
        price_per_day = parse_money(f.get("price_per_day"))
        total = parse_money(f.get("total"))
    except BadNumber:
        flash("Price per Day and Total must be valid numbers.", "error")
        return redisplay()
    if has_negative(price_per_day, total):
        flash("Price per Day and Total can't be negative.", "error")
        return redisplay()
    try:
        entry_date = clean_date(f.get("entry_date"), field="entry_date") or old["entry_date"]
        dismissal_date = clean_date(f.get("dismissal_date"), field="dismissal_date")
    except BadDate as e:
        flash(str(e), "error")
        return redisplay()
    # A stay cannot end before it began. boarding_suggested_total() floors the
    # night count at one, so this does not produce a negative bill — but it
    # does leave a nonsensical stay in the occupancy and boarding reports,
    # billed for a night it was never here.
    if dismissal_date and entry_date and str(dismissal_date) < str(entry_date):
        flash("A stay can't end before it starts — check the dates.", "error")
        return redisplay()
    total_is_auto = total is None
    if total_is_auto:
        total = logic.boarding_suggested_total(price_per_day, entry_date, dismissal_date)
    # Once picked up, boarding_dismiss() locked in the final billed figure —
    # dates/price/total from this form are ignored from that point on, same
    # as a settled invoice. Other fields (room, admitted items, special
    # needs) stay editable for ordinary record corrections.
    if old["dismissed"]:
        entry_date, dismissal_date = old["entry_date"], old["dismissal_date"]
        price_per_day, total, total_is_auto = old["price_per_day"], old["total"], bool(old["total_is_auto"])
    special_needs = f.get("special_needs") == "on"
    new_vals = {
        "entry_date": entry_date, "dismissal_date": dismissal_date, "admitted_items": f.get("admitted_items"),
        "special_needs": special_needs, "special_needs_notes": f.get("special_needs_notes") if special_needs else None,
        "room": f.get("room"), "price_per_day": price_per_day, "total": total, "total_is_auto": total_is_auto,
    }
    changes = auth.diff_dict(old, new_vals)
    db.execute(
        "UPDATE boarding_sessions SET entry_date=?, dismissal_date=?, admitted_items=?, special_needs=?, "
        "special_needs_notes=?, room=?, price_per_day=?, total=?, total_is_auto=?, updated_at=? WHERE id=?",
        (*new_vals.values(), datetime.now().isoformat(timespec="seconds"), boarding_id),
    )
    logic.refresh_boarding_total(db, boarding_id)
    old_month = logic.month_key(old["entry_date"])
    new_month = logic.month_key(entry_date)
    logic.recompute_months_summary(db, [old_month, new_month])
    auth.log_change(db, "boarding_sessions", str(boarding_id), "update", changes)
    db.commit()
    flash("Boarding session updated.", "success")
    if old["dismissed"]:
        flash("This stay is already picked up, so dates/price/total stayed locked at the billed figure — "
              "only room, admitted items, and special needs were changed.", "error")
    return redirect(url_for("clinical.boarding_page"))


@bp.route("/boarding/<int:boarding_id>/dismiss", methods=["POST"])
@auth.permission_required("manage_boarding")
def boarding_dismiss(boarding_id):
    db = get_db()
    row = db.execute(
        "SELECT price_per_day, entry_date, dismissal_date, total, total_is_auto FROM boarding_sessions WHERE id=?",
        (boarding_id,),
    ).fetchone()
    if not row:
        flash("Boarding session not found.", "error")
        return redirect(url_for("clinical.boarding_page"))
    dismissal_date = row["dismissal_date"] or date.today().isoformat()
    final_total = row["total"]
    if row["total_is_auto"] and row["price_per_day"]:
        # Lock in the final night count now that the stay is actually
        # over — while active, boarding_billing_summary() was recomputing
        # this live; once dismissed, nothing recomputes it anymore, so
        # `total` needs to hold the real final figure, not whatever
        # (usually 1 night) it was left at when the session was created.
        final_total = logic.boarding_suggested_total(row["price_per_day"], row["entry_date"], dismissal_date)
    db.execute("UPDATE boarding_sessions SET dismissed=true, dismissal_date=?, total=? WHERE id=?",
               (dismissal_date, final_total, boarding_id))
    logic.refresh_boarding_total(db, boarding_id)
    # Boarding revenue is attributed to entry_date's month, and that
    # month's P&L was already cached back when this session was created —
    # using whatever `total` was at that moment (usually a 1-night
    # placeholder, per the comment above). Locking in the real final total
    # here without this would leave that month's cached revenue
    # permanently understated.
    logic.recompute_month_summary(db, logic.month_key(row["entry_date"]))
    auth.log_change(db, "boarding_sessions", str(boarding_id), "update", {"dismissed": (False, True)})
    db.commit()
    flash("Marked as picked up.", "success")
    return redirect(url_for("clinical.boarding_page"))


@bp.route("/boarding/<int:boarding_id>/incident", methods=["POST"])
@auth.permission_required("manage_boarding")
def boarding_incident(boarding_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM boarding_sessions WHERE id=?", (boarding_id,)).fetchone():
        flash("Boarding session not found.", "error")
        return redirect(url_for("clinical.boarding_page"))
    f = request.form
    issue = (f.get("issue") or "").strip()
    if not issue:
        flash("Describe what's wrong before submitting.", "error")
        return redirect(url_for("clinical.boarding_page"))
    contacted = "Y" if f.get("contacted") == "on" else "N"
    cur = db.execute(
        "INSERT INTO boarding_incidents (boarding_id, timestamp, issue, contacted, contact_method, response, user_id) "
        "VALUES (?,?,?,?,?,?,?) RETURNING id",
        (boarding_id, datetime.now().isoformat(timespec="seconds"), issue, contacted,
         f.get("contact_method") if contacted == "Y" else None, f.get("response"), session.get("user_id")),
    )
    incident_id = cur.fetchone()["id"]
    auth.log_change(db, "boarding_incidents", str(incident_id), "create")
    db.commit()
    flash("Incident logged.", "success")
    return redirect(url_for("clinical.boarding_page"))


@bp.route("/boarding/<int:boarding_id>/payment", methods=["POST"])
@auth.permission_required("manage_boarding")
def boarding_payment(boarding_id):
    db = get_db()
    f = request.form

    def redisplay():
        return render_template("boarding.html", **_boarding_page_context(False),
                                form=f, payment_error_id=boarding_id)

    # Locked before computing the balance, same reasoning as
    # distributor_payment_new()/consignment_settlement_new() — there's no
    # delete/edit route for a payment once recorded, so an overpayment here
    # can never be undone, only journaled around.
    session_row = db.execute("SELECT id FROM boarding_sessions WHERE id=? FOR UPDATE", (boarding_id,)).fetchone()
    if not session_row:
        flash("Boarding session not found.", "error")
        return redirect(url_for("clinical.boarding_page"))
    try:
        amount = parse_money(f.get("amount")) or 0
    except BadNumber:
        flash("Payment amount must be a valid number.", "error")
        return redisplay()
    if amount <= 0:
        flash("Payment amount must be greater than 0.", "error")
        return redisplay()
    summary = logic.boarding_billing_summary(db, boarding_id)
    try:
        discount_percent = parse_money(f.get("discount_percent")) or 0
    except BadNumber:
        flash("Discount must be a valid number.", "error")
        return redisplay()
    cap = auth.discount_cap_for()
    error = discount_percent_error(discount_percent, cap)
    if error:
        flash(error, "error")
        return redisplay()
    try:
        cleanup_amount = parse_money(f.get("cleanup_amount")) or 0
    except BadNumber:
        flash("Clean Up amount must be a valid number.", "error")
        return redisplay()
    if cleanup_amount < 0:
        flash("Clean Up amount can't be negative.", "error")
        return redisplay()
    if summary["cleanup_amount"] + cleanup_amount > CLEANUP_CAP:
        flash(f"Clean Up can't exceed {CLEANUP_CAP} JOD total on this bill.", "error")
        return redisplay()

    # The discount and the Clean Up both change the balance this payment is
    # being checked against, and all three arrive in the same submission — so
    # validate against the bill as this submission would leave it, not as it
    # stands now. Checking the payment against the pre-submission balance
    # would let a discount-and-pay-in-full click overpay the discounted bill.
    _, _, balance_after_discount, _ = logic.compute_bill_totals(
        summary["subtotal"], discount_percent, summary["paid"], summary["cleanup_amount"])
    error = cleanup_amount_error(cleanup_amount, summary["cleanup_amount"], balance_after_discount)
    if error:
        flash(error, "error")
        return redisplay()
    _, _, balance, _ = logic.compute_bill_totals(
        summary["subtotal"], discount_percent, summary["paid"],
        summary["cleanup_amount"] + cleanup_amount)
    if amount > balance:
        flash(f"That's more than the remaining balance of {logic.fmt_money(balance)} JOD on this stay.", "error")
        return redisplay()
    cur = db.execute(
        "INSERT INTO payments (boarding_id, amount, method, date, user_id, notes) VALUES (?,?,?,?,?,?) RETURNING id",
        (boarding_id, amount, request.form.get("method"), date.today().isoformat(),
         session.get("user_id"), request.form.get("notes")),
    )
    payment_id = cur.fetchone()["id"]
    auth.log_change(db, "payments", str(payment_id), "create")
    if discount_percent != summary["discount_percent"]:
        db.execute(
            "UPDATE boarding_sessions SET discount_percent = ?, discount_applied_by = ? WHERE id = ?",
            (discount_percent, session["user_id"], boarding_id),
        )
        auth.log_change(db, "boarding_sessions", str(boarding_id), "update", changes={
            "discount_percent": (summary["discount_percent"], discount_percent)})
    if cleanup_amount > 0:
        db.execute(
            "UPDATE boarding_sessions SET cleanup_amount = cleanup_amount + ?, cleanup_applied_by = ? WHERE id = ?",
            (cleanup_amount, session["user_id"], boarding_id),
        )
        auth.log_change(db, "boarding_sessions", str(boarding_id), "update", changes={
            "cleanup_amount": (summary["cleanup_amount"], summary["cleanup_amount"] + cleanup_amount)})
    if cleanup_amount > 0 or discount_percent != summary["discount_percent"]:
        logic.refresh_boarding_total(db, boarding_id)
    db.commit()
    flash("Payment recorded.", "success")
    return redirect(url_for("clinical.boarding_page"))


@bp.route("/boarding/<int:boarding_id>/export")
@auth.permission_required("manage_boarding")
def boarding_export_pdf(boarding_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM boarding_sessions WHERE id=?", (boarding_id,)).fetchone():
        abort(404)
    buf = pdf_export.export_boarding_pdf(db, boarding_id)
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=f"boarding_{boarding_id}.pdf")


# ---------------------------------------------------------------------------
# Inpatient system
# ---------------------------------------------------------------------------
@bp.route("/inpatient")
@auth.permission_required("manage_inpatient")
def inpatient_list():
    db = get_db()
    show_all = request.args.get("all") == "1"
    # Discharged cases drop off the two views above by design (dismissed=false),
    # so a charge added *after* discharge (a forgotten procedure billed
    # late) has no natural collection point — nothing ever resurfaces that
    # case for staff to notice the balance and follow up. This view exists
    # specifically to close that gap: any discharged case still owing
    # money, regardless of why.
    balance_due = request.args.get("view") == "balance_due"
    page = get_page()
    if balance_due:
        paid_join = ("LEFT JOIN (SELECT inpatient_case_id, SUM(amount) AS paid FROM payments "
                     "GROUP BY inpatient_case_id) pay ON pay.inpatient_case_id = c.id")
        where = " WHERE c.dismissed=true AND c.total > COALESCE(pay.paid, 0)"
        total = db.execute(f"SELECT COUNT(*) c FROM inpatient_cases c {paid_join}{where}").fetchone()["c"]
        q = (f"SELECT c.*, p.animal_name, o.name as owner_name, COALESCE(pay.paid, 0) AS paid "
             f"FROM inpatient_cases c JOIN patients p ON p.id=c.patient_id JOIN owners o ON o.id=p.owner_id "
             f"{paid_join}{where} ORDER BY c.admission_date DESC LIMIT ? OFFSET ?")
        cases = db.execute(q, (PER_PAGE, page_offset(page))).fetchall()
    else:
        count_where = "" if show_all else " WHERE dismissed=false"
        total = db.execute(f"SELECT COUNT(*) c FROM inpatient_cases{count_where}").fetchone()["c"]
        q = ("SELECT c.*, p.animal_name, o.name as owner_name FROM inpatient_cases c "
             "JOIN patients p ON p.id=c.patient_id JOIN owners o ON o.id=p.owner_id")
        if not show_all:
            q += " WHERE c.dismissed=false"
        q += " ORDER BY c.admission_date DESC LIMIT ? OFFSET ?"
        cases = db.execute(q, (PER_PAGE, page_offset(page))).fetchall()
    return render_template("inpatient_list.html", cases=cases, show_all=show_all, balance_due=balance_due,
                            page=page, total_pages=page_count(total), total_count=total)


@bp.route("/inpatient/new", methods=["GET", "POST"])
@auth.permission_required("manage_inpatient")
def inpatient_new():
    db = get_db()
    if request.method == "POST":
        f = request.form
        def redisplay():
            pid = f.get("patient_id")
            prow = db.execute(
                "SELECT p.animal_name, o.name AS owner_name FROM patients p "
                "JOIN owners o ON o.id=p.owner_id WHERE p.id=?", (pid,),
            ).fetchone() if pid else None
            return render_template(
                "inpatient_new.html", vets=vet_users(db), form=f, selected_patient_id=pid,
                selected_patient_label=f"{prow['animal_name']} — {prow['owner_name']} ({pid})" if prow else None,
            )
        try:
            new_weight_kg = parse_money(f.get("weight_kg"))
            new_bcs = parse_bcs(f.get("bcs"))
            new_admission_date = clean_date(f.get("admission_date"), field="admission_date")
        except BadNumber:
            flash("Weight and BCS must be valid numbers.", "error")
            return redisplay()
        except BadDate as e:
            flash(str(e), "error")
            return redisplay()
        # IQ has guarded this since the fork; JO did not, so a negative
        # admission weight reached the chart and every trend built on it.
        if has_negative(new_weight_kg):
            flash("Weight can't be negative.", "error")
            return redisplay()
        patient_id = (f.get("patient_id") or "").strip()
        if not patient_id or not db.execute("SELECT 1 FROM patients WHERE id=?", (patient_id,)).fetchone():
            flash("Pick a patient from the search results first.", "error")
            return redisplay()
        case_id = _create_inpatient_case(db, patient_id, None, f.get("complaint"), new_admission_date,
                                          new_weight_kg, new_bcs)
        db.execute(
            "UPDATE inpatient_cases SET exam_findings=?, admitted_items=?, attending_vet_id=?, supervising_vet_id=? WHERE id=?",
            (f.get("exam_findings"), f.get("admitted_items"), f.get("attending_vet_id") or None,
             f.get("supervising_vet_id") or None, case_id),
        )
        db.commit()
        flash("Patient admitted.", "success")
        return redirect(url_for("clinical.inpatient_detail", case_id=case_id))
    return render_template("inpatient_new.html", vets=vet_users(db))


def _inpatient_detail_context(db, case_id):
    case = db.execute(
        "SELECT c.*, p.animal_name, p.species, p.sex, p.age_note, o.name as owner_name, o.phone as owner_phone, "
        "p.id as patient_id FROM inpatient_cases c JOIN patients p ON p.id=c.patient_id "
        "JOIN owners o ON o.id=p.owner_id WHERE c.id=?", (case_id,)
    ).fetchone()
    if not case:
        return None
    updates = db.execute("SELECT u.*, us.full_name FROM inpatient_updates u LEFT JOIN users us ON us.id=u.user_id "
                         "WHERE case_id=? ORDER BY timestamp DESC", (case_id,)).fetchall()
    contacts = db.execute("SELECT c.*, us.full_name FROM inpatient_contact_log c LEFT JOIN users us ON us.id=c.staff_user_id "
                          "WHERE case_id=? ORDER BY timestamp DESC", (case_id,)).fetchall()
    billing = logic.inpatient_billing_summary(db, case_id)
    payments = db.execute("SELECT * FROM payments WHERE inpatient_case_id=? ORDER BY date DESC", (case_id,)).fetchall()
    proc_items = db.execute("SELECT * FROM price_list WHERE category='Service' AND active=true ORDER BY id").fetchall()
    files = attach_mod.list_attachments(db, "inpatient", case_id)
    cap = auth.discount_cap_for()
    return dict(case=case, updates=updates, recent_updates=updates[:3],
                contacts=contacts, recent_contacts=contacts[:3], billing=billing, payments=payments,
                proc_items=proc_items, vets=vet_users(db), files=files, discount_cap=cap)


@bp.route("/inpatient/<int:case_id>")
@auth.permission_required("manage_inpatient")
def inpatient_detail(case_id):
    db = get_db()
    ctx = _inpatient_detail_context(db, case_id)
    if ctx is None:
        flash("Inpatient case not found.", "error")
        return redirect(url_for("clinical.inpatient_list"))
    return render_template("inpatient_detail.html", **ctx)


@bp.route("/inpatient/<int:case_id>/edit", methods=["POST"])
@auth.permission_required("manage_inpatient")
def inpatient_edit(case_id):
    db = get_db()
    f = request.form

    def redisplay():
        ctx = _inpatient_detail_context(db, case_id)
        if ctx is None:
            flash("Inpatient case not found.", "error")
            return redirect(url_for("clinical.inpatient_list"))
        return render_template("inpatient_detail.html", **ctx, form=f)

    old = db.execute("SELECT * FROM inpatient_cases WHERE id=?", (case_id,)).fetchone()
    conflict = stale_edit_error(old["updated_at"] if old else None, f.get("expected_updated_at"), "inpatient case")
    if conflict:
        flash(conflict, "error")
        return redisplay()
    dismissed = f.get("dismissed") == "on"
    try:
        edited_dismissal_date = clean_date(f.get("dismissal_date"), field="dismissal_date") if dismissed else None
        edited_weight_kg = parse_money(f.get("weight_kg"))
        edited_bcs = parse_bcs(f.get("bcs"))
    except (BadDate, BadNumber) as e:
        flash(str(e) if isinstance(e, BadDate) else "Weight and BCS must be valid numbers.", "error")
        return redisplay()
    if has_negative(edited_weight_kg):
        flash("Weight can't be negative.", "error")
        return redisplay()
    new_vals = {
        "complaint": f.get("complaint"), "exam_findings": f.get("exam_findings"),
        "weight_kg": edited_weight_kg, "bcs": edited_bcs,
        "admitted_items": f.get("admitted_items"), "dismissed": dismissed,
        "dismissal_date": edited_dismissal_date,
        "attending_vet_id": f.get("attending_vet_id") or None, "supervising_vet_id": f.get("supervising_vet_id") or None,
    }
    changes = auth.diff_dict(old, new_vals)
    db.execute(
        "UPDATE inpatient_cases SET complaint=?, exam_findings=?, weight_kg=?, bcs=?, admitted_items=?, dismissed=?, dismissal_date=?, "
        "attending_vet_id=?, supervising_vet_id=?, updated_at=? WHERE id=?",
        (*new_vals.values(), datetime.now().isoformat(timespec="seconds"), case_id),
    )
    auth.log_change(db, "inpatient_cases", str(case_id), "update", changes)
    db.commit()
    flash("Case updated.", "success")
    return redirect(url_for("clinical.inpatient_detail", case_id=case_id))


@bp.route("/inpatient/<int:case_id>/update", methods=["POST"])
@auth.permission_required("manage_inpatient")
def inpatient_update_add(case_id):
    db = get_db()
    note = request.form.get("note", "").strip()
    if note:
        db.execute("INSERT INTO inpatient_updates (case_id, timestamp, note, user_id) VALUES (?,?,?,?)",
                  (case_id, datetime.now().isoformat(timespec="seconds"), note, session["user_id"]))
        auth.log_change(db, "inpatient_updates", str(case_id), "create")
        db.commit()
        flash("Update logged.", "success")
    return redirect(url_for("clinical.inpatient_detail", case_id=case_id))


@bp.route("/inpatient/<int:case_id>/update/<int:update_id>/edit", methods=["POST"])
@auth.permission_required("manage_inpatient")
def inpatient_update_edit(case_id, update_id):
    db = get_db()
    note = request.form.get("note", "").strip()
    old = db.execute("SELECT note FROM inpatient_updates WHERE id=? AND case_id=?", (update_id, case_id)).fetchone()
    if old and note:
        db.execute("UPDATE inpatient_updates SET note=? WHERE id=?", (note, update_id))
        auth.log_change(db, "inpatient_updates", str(update_id), "update", {"note": (old["note"], note)})
        db.commit()
        flash("Update edited.", "success")
    return redirect(url_for("clinical.inpatient_detail", case_id=case_id))


@bp.route("/inpatient/<int:case_id>/contact", methods=["POST"])
@auth.permission_required("manage_inpatient")
def inpatient_contact_add(case_id):
    db = get_db()
    f = request.form
    picked_up = 1 if f.get("picked_up") == "yes" else 0
    db.execute("INSERT INTO inpatient_contact_log (case_id, timestamp, picked_up, staff_user_id, notes) VALUES (?,?,?,?,?)",
              (case_id, datetime.now().isoformat(timespec="seconds"), picked_up, session["user_id"], f.get("notes")))
    auth.log_change(db, "inpatient_contact_log", str(case_id), "create")
    db.commit()
    flash("Contact attempt logged.", "success")
    return redirect(url_for("clinical.inpatient_detail", case_id=case_id))


@bp.route("/inpatient/<int:case_id>/billing", methods=["POST"])
@auth.permission_required("manage_inpatient")
def inpatient_billing_add(case_id):
    db = get_db()
    # Locked for the same reason inpatient_discount_save() locks this row
    # — see the comment there. A pure mutex against a concurrent discount
    # save on the same case; nothing about the inpatient_cases row itself
    # changes here.
    if not db.execute("SELECT id FROM inpatient_cases WHERE id=? FOR UPDATE", (case_id,)).fetchone():
        flash("Inpatient case not found.", "error")
        return redirect(url_for("clinical.inpatient_list"))
    price_ids = request.form.getlist("price_id")
    now = datetime.now().isoformat(timespec="seconds")
    added = 0
    had_bad_number = False
    had_bad_price = False
    had_blocked = False
    # inpatient_discount_save() only checks non-discountable items against
    # whatever's on the bill *at the moment a discount is applied* — it
    # has no way to know the bill will change later. Re-checking here too
    # closes the gap where a discount already applied earlier would
    # otherwise silently carry forward onto procedures added afterward
    # that were never supposed to be discountable at all (mirrors
    # visit_billing_save()'s equivalent check).
    existing_case = db.execute("SELECT discount_percent FROM inpatient_cases WHERE id=?", (case_id,)).fetchone()
    existing_discount = (existing_case["discount_percent"] or 0) if existing_case else 0
    blocked_pids = set()
    if existing_discount > 0:
        blocked_pids = {r["id"] for r in db.execute(
            f"SELECT id FROM price_list WHERE id IN ({','.join('?' * len(price_ids))}) AND can_discount=false",
            price_ids,
        ).fetchall()} if price_ids else set()
    for pid in price_ids:
        raw_qty = request.form.get(f"qty_{pid}", "").strip()
        try:
            qty = parse_quantity(raw_qty)
        except BadNumber:
            had_bad_number = True
            continue
        if not qty or qty <= 0:
            continue
        if pid in blocked_pids:
            had_blocked = True
            continue
        # Snapshot the current Price List sale price/cost right now, at
        # the moment this procedure is added to the bill — so a price
        # edit made next month can't reach back and change what this
        # stay's bill (or that month's revenue/COGS report) says today.
        price_row = db.execute("SELECT sale_price, cost_price FROM price_list WHERE id=?", (pid,)).fetchone()
        if not price_row:
            had_bad_price = True
            continue
        db.execute(
            "INSERT INTO inpatient_billing (case_id, price_id, quantity, unit_price, unit_cost, logged_by, timestamp) "
            "VALUES (?,?,?,?,?,?,?)",
            (case_id, pid, qty, price_row["sale_price"], price_row["cost_price"], session["user_id"], now),
        )
        added += 1
    if added:
        logic.refresh_inpatient_total(db, case_id)
        logic.recompute_month_summary(db, now[:7])
        auth.log_change(db, "inpatient_billing", str(case_id), "create")
    db.commit()
    if had_bad_number:
        flash("Some quantities weren't valid numbers and were skipped.", "error")
    if had_bad_price:
        flash("Some selected items no longer exist in the Price List and were skipped.", "error")
    if had_blocked:
        flash("Some selected items are marked as not discountable and can't be added to a bill "
              "that already has a discount applied — remove the discount first, or leave them off this bill.", "error")
    if added:
        flash(f"{added} procedure(s) added to the bill.", "success")
    return redirect(url_for("clinical.inpatient_detail", case_id=case_id))


@bp.route("/inpatient/<int:case_id>/billing/<int:line_id>/delete", methods=["POST"])
@auth.permission_required("manage_inpatient")
def inpatient_billing_delete(case_id, line_id):
    db = get_db()
    row = db.execute("SELECT timestamp FROM inpatient_billing WHERE id=? AND case_id=?", (line_id, case_id)).fetchone()
    if not row:
        flash("That billing line was already removed.", "error")
        return redirect(url_for("clinical.inpatient_detail", case_id=case_id))
    # Deleting a line can zero out (or shrink) the case's total while
    # payments already taken against it stay on the books — nothing else
    # ever surfaces `paid > total` after that. See ORPHANED_RECORDS_AUDIT.md
    # F-14.
    summary = logic.inpatient_billing_summary(db, case_id)
    this_line = next((l for l in summary["lines"] if l["id"] == line_id), None)
    remaining_subtotal = summary["subtotal"] - (this_line["line_total"] if this_line else 0)
    remaining_total, _, _, _ = logic.compute_bill_totals(
        remaining_subtotal, summary["discount_percent"], 0, summary["cleanup_amount"])
    if summary["paid"] > remaining_total:
        flash(f"Removing this line would leave {logic.fmt_money(summary['paid'])} paid against a "
              f"{logic.fmt_money(remaining_total)} JOD bill. Process a service refund for the "
              f"difference first.", "error")
        return redirect(url_for("clinical.inpatient_detail", case_id=case_id))
    db.execute("DELETE FROM inpatient_billing WHERE id=? AND case_id=?", (line_id, case_id))
    logic.refresh_inpatient_total(db, case_id)
    if row["timestamp"]:
        logic.recompute_month_summary(db, row["timestamp"][:7])
    auth.log_change(db, "inpatient_billing", str(line_id), "delete")
    db.commit()
    flash("Line removed.", "success")
    return redirect(url_for("clinical.inpatient_detail", case_id=case_id))


@bp.route("/inpatient/<int:case_id>/discount", methods=["POST"])
@auth.permission_required("manage_inpatient")
def inpatient_discount_save(case_id):
    db = get_db()
    f = request.form

    def redisplay():
        ctx = _inpatient_detail_context(db, case_id)
        if ctx is None:
            flash("Inpatient case not found.", "error")
            return redirect(url_for("clinical.inpatient_list"))
        return render_template("inpatient_detail.html", **ctx, form=f, discount_error=True)

    try:
        percent = parse_money(f.get("discount_percent")) or 0
    except BadNumber:
        flash("Discount must be a valid number.", "error")
        return redisplay()
    cap = auth.discount_cap_for()
    error = discount_percent_error(percent, cap)
    if error:
        flash(error, "error")
        return redisplay()
    # Locked before checking non-discountable items and before writing the
    # discount below — same reasoning as visit_discount_save(): without
    # this, a concurrent inpatient_billing_add() for the same case could
    # read the bill's lines before this request's check but write a new
    # (non-discountable) line after it.
    if not db.execute("SELECT id FROM inpatient_cases WHERE id=? FOR UPDATE", (case_id,)).fetchone():
        flash("Inpatient case not found.", "error")
        return redirect(url_for("clinical.inpatient_list"))
    if percent > 0:
        price_ids = [r["price_id"] for r in db.execute(
            "SELECT DISTINCT price_id FROM inpatient_billing WHERE case_id=?", (case_id,)
        ).fetchall()]
        blocked = logic.non_discountable_line_names(db, price_ids)
        if blocked:
            flash(f"Can't apply a discount — this bill includes item(s) marked as not discountable: {', '.join(blocked)}.", "error")
            return redisplay()
    old = db.execute("SELECT discount_percent FROM inpatient_cases WHERE id=?", (case_id,)).fetchone()
    db.execute("UPDATE inpatient_cases SET discount_percent=?, discount_applied_by=? WHERE id=?",
              (percent, session["user_id"], case_id))
    logic.refresh_inpatient_total(db, case_id)
    logic.recompute_months_summary(db, logic.months_touched_by_inpatient_case(db, case_id))
    auth.log_change(db, "inpatient_cases", str(case_id), "update", {"discount_percent": (old["discount_percent"], percent)})
    db.commit()
    flash(f"{percent:.0f}% discount applied.", "success")
    return redirect(url_for("clinical.inpatient_detail", case_id=case_id))


@bp.route("/inpatient/<int:case_id>/payment", methods=["POST"])
@auth.permission_required("manage_inpatient")
def inpatient_payment_add(case_id):
    db = get_db()
    f = request.form

    def redisplay():
        ctx = _inpatient_detail_context(db, case_id)
        if ctx is None:
            flash("Inpatient case not found.", "error")
            return redirect(url_for("clinical.inpatient_list"))
        return render_template("inpatient_detail.html", **ctx, form=f, payment_error=True)

    # Locked before computing the balance — same reasoning as
    # boarding_payment()/visit_payment_add(): there's no delete/edit route
    # for a payment once recorded, so an overpayment here can never be
    # undone, only journaled around.
    if not db.execute("SELECT id FROM inpatient_cases WHERE id=? FOR UPDATE", (case_id,)).fetchone():
        flash("Inpatient case not found.", "error")
        return redirect(url_for("clinical.inpatient_list"))
    try:
        amount = parse_money(f.get("amount"), required=True)
    except BadNumber:
        flash("Payment amount must be a valid number.", "error")
        return redisplay()
    if amount <= 0:
        flash("Payment amount must be greater than 0.", "error")
        return redisplay()
    summary = logic.inpatient_billing_summary(db, case_id)
    balance = summary["balance"]
    if amount > balance:
        flash(f"That's more than the remaining balance of {logic.fmt_money(balance)} JOD on this case.", "error")
        return redisplay()
    try:
        cleanup_amount = parse_money(f.get("cleanup_amount")) or 0
    except BadNumber:
        flash("Clean Up amount must be a valid number.", "error")
        return redisplay()
    error = cleanup_amount_error(cleanup_amount, summary["cleanup_amount"], balance)
    if error:
        flash(error, "error")
        return redisplay()
    try:
        payment_date = clean_date(f.get("date"), field="date") or date.today().isoformat()
    except BadDate as e:
        flash(str(e), "error")
        return redisplay()
    cur = db.execute(
        "INSERT INTO payments (inpatient_case_id, amount, method, date, user_id, notes) VALUES (?,?,?,?,?,?) RETURNING id",
        (case_id, amount, f.get("method"), payment_date, session["user_id"], f.get("notes")),
    )
    payment_id = cur.fetchone()["id"]
    auth.log_change(db, "payments", str(payment_id), "create")
    if cleanup_amount > 0:
        db.execute(
            "UPDATE inpatient_cases SET cleanup_amount = cleanup_amount + ?, cleanup_applied_by = ? WHERE id = ?",
            (cleanup_amount, session["user_id"], case_id),
        )
        auth.log_change(db, "inpatient_cases", str(case_id), "update", changes={
            "cleanup_amount": (summary["cleanup_amount"], summary["cleanup_amount"] + cleanup_amount)})
        logic.refresh_inpatient_total(db, case_id)
    db.commit()
    flash("Payment recorded.", "success")
    return redirect(url_for("clinical.inpatient_detail", case_id=case_id))


@bp.route("/inpatient/<int:case_id>/attachments", methods=["POST"])
@auth.permission_required("manage_inpatient")
def inpatient_attachment_upload(case_id):
    db = get_db()
    case = db.execute("SELECT patient_id FROM inpatient_cases WHERE id=?", (case_id,)).fetchone()
    if not case:
        flash("Inpatient case not found.", "error")
        return redirect(url_for("clinical.inpatient_list"))
    file = request.files.get("file")
    if not file or not file.filename:
        flash("No file selected.", "error")
        return redirect(url_for("clinical.inpatient_detail", case_id=case_id))
    _, err = attach_mod.save_attachment(db, case["patient_id"], "inpatient", case_id, file, session["user_id"])
    flash(err if err else "File uploaded.", "error" if err else "success")
    return redirect(url_for("clinical.inpatient_detail", case_id=case_id))


# ---------------------------------------------------------------------------
# Appointments
# ---------------------------------------------------------------------------
def _appointments_page_context():
    """Builds the template context for appointments.html. Split out of
    appointments_page() so appointment_new() can re-render the same weekly
    grid (with the Add modal reopened and `form` layered on top) on a
    validation failure instead of discarding the submitted booking via
    redirect."""
    db = get_db()
    today_iso = date.today().isoformat()
    week_anchor = request.args.get("week", today_iso)
    try:
        logic.parse_date(week_anchor)
    except ValueError:
        flash("That week link wasn't valid, showing the current week instead.", "error")
        week_anchor = today_iso
    days = logic.week_dates(week_anchor)
    selected_day = request.args.get("day", today_iso)
    try:
        logic.parse_date(selected_day)
    except ValueError:
        flash("That date wasn't valid, showing today instead.", "error")
        selected_day = today_iso
    columns, grid = logic.day_grid(db, selected_day)
    prev_week = (days[0] - timedelta(days=7)).isoformat()
    next_week = (days[0] + timedelta(days=7)).isoformat()
    show_past = request.args.get("show_past") == "1"
    orphaned = logic.orphaned_appointments(db, include_past=show_past)
    return dict(days=days, selected_day=selected_day, columns=columns,
                grid=grid, week_anchor=week_anchor, prev_week=prev_week, next_week=next_week,
                today_iso=today_iso, orphaned=orphaned, show_past=show_past)


@bp.route("/appointments")
@auth.permission_required("manage_appointments")
def appointments_page():
    return render_template("appointments.html", **_appointments_page_context())


@bp.route("/appointments/new", methods=["POST"])
@auth.permission_required("manage_appointments")
def appointment_new():
    db = get_db()
    f = request.form

    def redisplay():
        ctx = _appointments_page_context()
        ctx["form"] = f
        ctx["open_add_modal"] = True
        return render_template("appointments.html", **ctx)

    try:
        appt_date = clean_date(f.get("appt_date"), field="appt_date")
    except BadDate as e:
        flash(str(e), "error")
        return redisplay()
    if appt_date is None:
        flash("Appointment date is required.", "error")
        return redisplay()
    slot_label = required_field(f, "slot_label", "Time slot")
    if slot_label is None:
        return redisplay()
    resource_type = f.get("resource_type")
    if resource_type not in RESOURCE_TYPES:
        flash("Resource type must be one of: " + ", ".join(RESOURCE_TYPES) + ".", "error")
        return redisplay()
    appointment_type = f.get("appointment_type")
    if appointment_type not in APPOINTMENT_TYPES:
        flash("Appointment type must be one of: " + ", ".join(APPOINTMENT_TYPES) + ".", "error")
        return redisplay()
    resource_id = f.get("resource_id") or None
    if resource_type == "grooming":
        # Grooming has no per-resource distinction — every grooming booking
        # shares one column on the grid, keyed as (slot_label, "grooming",
        # NULL) by day_grid()/slot_conflict(). A tampered request smuggling
        # a non-null resource_id here would create a row neither of those
        # ever looks at — invisible on the grid — so this is the only slot
        # type where the value has to be forced rather than merely validated.
        resource_id = None
    elif not resource_id or not any(v["id"] == resource_id for v in vet_users(db)):
        flash("Pick a valid, active vet for this appointment.", "error")
        return redisplay()
    if not any(s["label"] == slot_label for s in logic.generate_slots(db)):
        flash("That's not a valid time slot — the schedule may have changed. Reload and try again.", "error")
        return redisplay()

    if logic.slot_conflict(db, appt_date, slot_label, resource_type, resource_id):
        flash("That slot is already booked for this vet/groomer.", "error")
        return redisplay()

    pet_name = required_field(f, "pet_name", "Pet name")
    if pet_name is None:
        return redisplay()
    owner_name = required_field(f, "owner_name", "Owner name")
    if owner_name is None:
        return redisplay()

    # The check above is a friendly fast-path, not the real guarantee — two
    # concurrent bookings for the same slot could both pass it before either
    # inserts. The database's uq_appointments_slot unique index (see
    # schema_postgres.sql) is what actually prevents the double-booking;
    # this catches the resulting IntegrityError for whichever request loses
    # that race and turns it into the same friendly message instead of a
    # raw 500.
    try:
        cur = db.execute(
            "INSERT INTO appointments (appt_date, slot_label, resource_type, resource_id, pet_name, owner_name, "
            "appointment_type, reason, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?) RETURNING id",
            (appt_date, slot_label, resource_type, resource_id, pet_name, owner_name,
             appointment_type, f.get("reason"), session["user_id"], datetime.now().isoformat(timespec="seconds")),
        )
        appt_id = cur.fetchone()["id"]
        auth.log_change(db, "appointments", str(appt_id), "create")
        db.commit()
    except dbmod.IntegrityError:
        db.rollback()
        flash("That slot is already booked for this vet/groomer.", "error")
        return redisplay()
    flash("Appointment booked.", "success")
    return redirect(url_for("clinical.appointments_page", day=appt_date))


@bp.route("/appointments/<int:appt_id>/cancel", methods=["POST"])
@auth.permission_required("manage_appointments")
def appointment_cancel(appt_id):
    db = get_db()
    row = db.execute("SELECT appt_date FROM appointments WHERE id=?", (appt_id,)).fetchone()
    if not row:
        flash("Appointment not found.", "error")
        return redirect(url_for("clinical.appointments_page"))
    db.execute("DELETE FROM appointments WHERE id=?", (appt_id,))
    auth.log_change(db, "appointments", str(appt_id), "delete")
    db.commit()
    flash("Appointment cancelled.", "success")
    return redirect(url_for("clinical.appointments_page", day=str(row["appt_date"])))
