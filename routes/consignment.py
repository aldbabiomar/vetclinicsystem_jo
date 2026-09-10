"""
Consignment stock and the distributors behind it: receiving, shrinkage, returns and settlements, plus the distributor bill/payment ledger. Consignment items are ordinary Retail inventory rows; this is the distributor-facing layer on top of that shared data, not a second inventory system.

Split out of app.py. Shared request-layer pieces come from core.py rather than
app.py -- app.py registers this blueprint, so importing from it here would be
circular.

Endpoint names carry the `consignment.` prefix Flask gives every blueprint route:
`url_for("consignment.distributors_list")`, not `url_for("consignment.distributors_list")`.
"""

from datetime import date
from datetime import datetime
import auth
import db as dbmod
import logic
import pdf_export

from flask import (
    Blueprint, flash, jsonify, redirect, render_template, request, send_file, session, url_for
)

from core import BadDate, BadNumber, BadPhone, PER_PAGE, _render_with_progress, clean_date, get_db, get_page, normalize_phone, page_count, page_offset, parse_int, parse_money, required_field

bp = Blueprint("consignment", __name__)


# ---------------------------------------------------------------------------
# Distributors
# ---------------------------------------------------------------------------
def _distributors_list_context(search):
    db = get_db()
    if search:
        rows = db.execute("SELECT * FROM distributors WHERE name ILIKE ? ORDER BY name", (logic.like_pattern(search),)).fetchall()
    else:
        rows = db.execute("SELECT * FROM distributors ORDER BY name").fetchall()
    outstanding = logic.distributor_outstanding_totals(db)
    payables = logic.distributor_payables_summary(db)
    return dict(distributors=rows, search=search, outstanding=outstanding, payables=payables)


@bp.route("/distributors")
@auth.permission_required("manage_distributors")
def distributors_list():
    search = request.args.get("q", "").strip()
    return render_template("distributors.html", **_distributors_list_context(search))


@bp.route("/distributors/new", methods=["POST"])
@auth.permission_required("manage_distributors")
def distributor_new():
    db = get_db()
    f = request.form

    def redisplay():
        ctx = _distributors_list_context(request.args.get("q", "").strip())
        ctx["form"] = f
        ctx["open_new_form"] = True
        return render_template("distributors.html", **ctx)

    try:
        phone = normalize_phone(f.get("phone"))
    except BadPhone:
        flash("That phone number doesn't look valid — check the digits and try again.", "error")
        return redisplay()
    try:
        lead_time_days = parse_int(f.get("lead_time_days"))
    except BadNumber:
        flash("Lead Time (Days) must be a whole number.", "error")
        return redisplay()
    # Lead time drives the reorder point in the ordering sheet: a negative
    # one asks for stock to arrive before it was ordered. IQ and JO both
    # parsed it without a sign check.
    if lead_time_days is not None and lead_time_days < 0:
        flash("Lead Time (Days) can't be negative.", "error")
        return redisplay()
    name = required_field(f, "name", "Name")
    if name is None:
        return redisplay()
    did = dbmod.next_id(db, "D")
    db.execute(
        "INSERT INTO distributors (id,name,contact_person,phone,email,catalog_link,lead_time_days,payment_terms,notes) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (did, name, f.get("contact_person"), phone, f.get("email"), f.get("catalog_link"),
         lead_time_days, f.get("payment_terms"), f.get("notes")),
    )
    auth.log_change(db, "distributors", did, "create")
    db.commit()
    flash(f"{did} added.", "success")
    return redirect(url_for("consignment.distributors_list"))


@bp.route("/distributors/<dist_id>/edit", methods=["POST"])
@auth.permission_required("manage_distributors")
def distributor_edit(dist_id):
    db = get_db()
    f = request.form

    def redisplay():
        # Cleared rather than kept from the query string — if the person
        # was mid-search for something that no longer matches this
        # distributor's *submitted* (possibly edited) name, the row being
        # edited would silently vanish from a filtered redisplay.
        ctx = _distributors_list_context("")
        ctx["form"] = f
        ctx["edit_dist_id"] = dist_id
        return render_template("distributors.html", **ctx)

    try:
        phone = normalize_phone(f.get("phone"))
    except BadPhone:
        flash("That phone number doesn't look valid — check the digits and try again.", "error")
        return redisplay()
    try:
        lead_time_days = parse_int(f.get("lead_time_days"))
    except BadNumber:
        flash("Lead Time (Days) must be a whole number.", "error")
        return redisplay()
    # Lead time drives the reorder point in the ordering sheet: a negative
    # one asks for stock to arrive before it was ordered. IQ and JO both
    # parsed it without a sign check.
    if lead_time_days is not None and lead_time_days < 0:
        flash("Lead Time (Days) can't be negative.", "error")
        return redisplay()
    old = db.execute("SELECT * FROM distributors WHERE id=?", (dist_id,)).fetchone()
    name = required_field(f, "name", "Name")
    if name is None:
        return redisplay()
    new_vals = {"name": name, "contact_person": f.get("contact_person"), "phone": phone,
                "email": f.get("email"), "catalog_link": f.get("catalog_link"),
                "lead_time_days": lead_time_days, "payment_terms": f.get("payment_terms"),
                "notes": f.get("notes")}
    changes = auth.diff_dict(old, new_vals)
    db.execute(
        "UPDATE distributors SET name=?, contact_person=?, phone=?, email=?, catalog_link=?, lead_time_days=?, payment_terms=?, notes=? WHERE id=?",
        (*new_vals.values(), dist_id),
    )
    auth.log_change(db, "distributors", dist_id, "update", changes)
    db.commit()
    flash("Distributor updated.", "success")
    return redirect(url_for("consignment.distributors_list"))


@bp.route("/distributors/<dist_id>/delete", methods=["POST"])
@auth.permission_required("manage_distributors")
def distributor_delete(dist_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM distributors WHERE id=?", (dist_id,)).fetchone():
        flash("Distributor not found.", "error")
        return redirect(url_for("consignment.distributors_list"))
    # A distributor can be referenced from six tables (inventory items,
    # manual ledger bills, and every Consignment table) — a bare DELETE
    # would just crash with a raw ForeignKeyViolation the moment any of
    # them has a row, same failure mode admin_role_delete() already
    # guards against for roles. Check first and name what's still linked,
    # rather than let Postgres reject it as an unhandled 500. See
    # ORPHANED_RECORDS_AUDIT.md F-08.
    still_linked = []
    for label, table in [
        ("inventory item(s)", "inventory_list"), ("distributor bill(s)", "distributor_bills"),
        ("consignment receipt(s)", "consignment_receipts"), ("consignment shrinkage entry/entries", "consignment_shrinkage"),
        ("consignment return(s)", "consignment_returns"), ("consignment settlement(s)", "consignment_settlements"),
    ]:
        if db.execute(f"SELECT 1 FROM {table} WHERE distributor_id=? LIMIT 1", (dist_id,)).fetchone():
            still_linked.append(label)
    if still_linked:
        flash("Can't delete this distributor — it still has " + ", ".join(still_linked) +
              " linked to it. Remove or reassign those first.", "error")
        return redirect(url_for("consignment.distributors_list"))
    db.execute("DELETE FROM distributors WHERE id=?", (dist_id,))
    auth.log_change(db, "distributors", dist_id, "delete")
    db.commit()
    flash("Distributor deleted.", "success")
    return redirect(url_for("consignment.distributors_list"))


# ---------------------------------------------------------------------------
# Distributor Ledger — manual bookkeeping for what a distributor has billed
# you and what you've paid them. Lump-sum bills only, no link to inventory,
# POS, or any report; balance/status are always computed (never stored).
# ---------------------------------------------------------------------------
def _distributor_detail_context(dist_id):
    """Returns None if the distributor doesn't exist. Split out of
    distributor_detail() so distributor_bill_new()/distributor_payment_new()
    can re-render the same page (with redisplay state layered on top) on a
    validation failure instead of discarding the submitted data via
    redirect."""
    db = get_db()
    dist = db.execute("SELECT * FROM distributors WHERE id=?", (dist_id,)).fetchone()
    if not dist:
        return None
    ledger = logic.distributor_ledger(db, dist_id)
    return dict(distributor=dist, **ledger)


@bp.route("/distributors/<dist_id>")
@auth.permission_required("manage_distributors")
def distributor_detail(dist_id):
    ctx = _distributor_detail_context(dist_id)
    if ctx is None:
        flash("Distributor not found.", "error")
        return redirect(url_for("consignment.distributors_list"))
    return render_template("distributor_detail.html", **ctx)


@bp.route("/distributors/<dist_id>/bills/new", methods=["POST"])
@auth.permission_required("manage_distributors")
def distributor_bill_new(dist_id):
    db = get_db()
    f = request.form

    def redisplay():
        ctx = _distributor_detail_context(dist_id)
        ctx["form"] = f
        ctx["open_new_bill_form"] = True
        return render_template("distributor_detail.html", **ctx)

    try:
        total_amount = parse_money(f.get("total_amount"), required=True)
    except BadNumber:
        flash("Total amount must be a valid number.", "error")
        return redisplay()
    if total_amount <= 0:
        flash("Total amount must be greater than zero.", "error")
        return redisplay()
    try:
        bill_date = clean_date(f.get("bill_date"), field="bill_date") or date.today().isoformat()
    except BadDate as e:
        flash(str(e), "error")
        return redisplay()
    bid = dbmod.next_id(db, "DB")
    db.execute(
        "INSERT INTO distributor_bills (id,distributor_id,bill_date,bill_reference,total_amount,notes,created_at,created_by) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (bid, dist_id, bill_date, f.get("bill_reference"), total_amount, f.get("notes"),
         datetime.now().isoformat(timespec="seconds"), session.get("user_id")),
    )
    auth.log_change(db, "distributor_bills", bid, "create")
    db.commit()
    flash(f"Bill {bid} logged.", "success")
    return redirect(url_for("consignment.distributor_detail", dist_id=dist_id))


@bp.route("/distributors/<dist_id>/bills/<bill_id>/delete", methods=["POST"])
@auth.permission_required("manage_distributors")
def distributor_bill_delete(dist_id, bill_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM distributor_bills WHERE id=? AND distributor_id=?", (bill_id, dist_id)).fetchone():
        flash("Bill not found.", "error")
        return redirect(url_for("consignment.distributor_detail", dist_id=dist_id))
    has_payments = db.execute(
        "SELECT 1 FROM distributor_bill_payments WHERE bill_id=? LIMIT 1", (bill_id,)
    ).fetchone()
    if has_payments:
        flash("Delete the payments on this bill first.", "error")
        return redirect(url_for("consignment.distributor_detail", dist_id=dist_id))
    db.execute("DELETE FROM distributor_bills WHERE id=? AND distributor_id=?", (bill_id, dist_id))
    auth.log_change(db, "distributor_bills", bill_id, "delete")
    db.commit()
    flash("Bill deleted.", "success")
    return redirect(url_for("consignment.distributor_detail", dist_id=dist_id))


@bp.route("/distributors/<dist_id>/bills/<bill_id>/payments/new", methods=["POST"])
@auth.permission_required("manage_distributors")
def distributor_payment_new(dist_id, bill_id):
    db = get_db()
    f = request.form

    def redisplay():
        ctx = _distributor_detail_context(dist_id)
        ctx["form"] = f
        ctx["payment_form_bill_id"] = bill_id
        return render_template("distributor_detail.html", **ctx)

    # Locked before computing the balance, same reasoning as
    # pos_checkout()'s cart-item locking: without this, two payments each
    # individually within the balance shown at page-load could both pass
    # the check below and both insert, together overpaying the bill.
    bill = db.execute(
        "SELECT * FROM distributor_bills WHERE id=? AND distributor_id=? FOR UPDATE", (bill_id, dist_id)
    ).fetchone()
    if not bill:
        flash("Bill not found.", "error")
        return redirect(url_for("consignment.distributor_detail", dist_id=dist_id))
    try:
        amount = parse_money(f.get("amount"), required=True)
    except BadNumber:
        flash("Payment amount must be a valid number.", "error")
        return redisplay()
    if amount <= 0:
        flash("Payment amount must be greater than zero.", "error")
        return redisplay()
    paid_so_far = db.execute(
        "SELECT COALESCE(SUM(amount),0) s FROM distributor_bill_payments WHERE bill_id=?", (bill_id,)
    ).fetchone()["s"]
    balance = bill["total_amount"] - paid_so_far
    # The HTML max= on the amount field already stops this in the normal
    # UI, but that's client-side only — a crafted request or a stale page
    # (someone else already paid part of it) can still submit more than
    # what's actually left owed, which would flip the bill to a "Paid"
    # badge next to a negative balance with nothing indicating an
    # overpayment/credit happened.
    if amount > balance:
        flash(f"That's more than the remaining balance of {logic.fmt_money(balance)} JOD on this bill.", "error")
        return redisplay()
    try:
        payment_date = clean_date(f.get("payment_date"), field="payment_date") or date.today().isoformat()
    except BadDate as e:
        flash(str(e), "error")
        return redisplay()
    cur = db.execute(
        "INSERT INTO distributor_bill_payments (bill_id,amount,payment_date,method,notes,created_at,created_by) "
        "VALUES (?,?,?,?,?,?,?) RETURNING id",
        (bill_id, amount, payment_date, f.get("method"), f.get("notes"),
         datetime.now().isoformat(timespec="seconds"), session.get("user_id")),
    )
    pid = cur.fetchone()["id"]
    auth.log_change(db, "distributor_bill_payments", str(pid), "create")
    db.commit()
    flash("Payment recorded.", "success")
    return redirect(url_for("consignment.distributor_detail", dist_id=dist_id))


@bp.route("/distributors/<dist_id>/payments/<int:payment_id>/delete", methods=["POST"])
@auth.permission_required("manage_distributors")
def distributor_payment_delete(dist_id, payment_id):
    db = get_db()
    owned = db.execute(
        "SELECT 1 FROM distributor_bill_payments p JOIN distributor_bills b ON b.id = p.bill_id "
        "WHERE p.id=? AND b.distributor_id=?",
        (payment_id, dist_id),
    ).fetchone()
    if not owned:
        flash("Payment not found.", "error")
        return redirect(url_for("consignment.distributor_detail", dist_id=dist_id))
    db.execute("DELETE FROM distributor_bill_payments WHERE id=?", (payment_id,))
    auth.log_change(db, "distributor_bill_payments", str(payment_id), "delete")
    db.commit()
    flash("Payment deleted.", "success")
    return redirect(url_for("consignment.distributor_detail", dist_id=dist_id))


@bp.route("/distributors/<dist_id>/export.pdf")
@auth.permission_required("manage_distributors")
def distributor_export_pdf(dist_id):
    db = get_db()
    dist = db.execute("SELECT id FROM distributors WHERE id=?", (dist_id,)).fetchone()
    if not dist:
        flash("Distributor not found.", "error")
        return redirect(url_for("consignment.distributors_list"))
    buf = pdf_export.export_distributor_ledger(db, dist_id)
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=f"{dist_id}_ledger.pdf")


# ---------------------------------------------------------------------------
# Consignment — a distributor's stock sitting on your shelf; they're owed
# cost_price per unit once it sells, you keep the markup. Consignment
# items are ordinary Retail inventory_list rows (ownership_type=
# 'Consignment') and already flow through POS/audit/P&L unmodified — this
# section is the distributor-facing receiving/shrinkage/returns/
# settlement layer on top of that shared data.
# ---------------------------------------------------------------------------
@bp.route("/consignment")
@auth.permission_required("view_consignment")
def consignment_overview():
    # consignment_distributors_overview() recomputes full inventory status
    # per Consignment item per distributor — wrapped in the same
    # background-job loading shell Insights/Retention use, for framework
    # parity (not because this clinic's distributor count makes it slow
    # today).
    def compute(update):
        con = dbmod.connect()
        try:
            rows = logic.consignment_distributors_overview(con)
        finally:
            con.close()
        return {"rows": rows}

    return _render_with_progress(
        "consignment_overview.html",
        ["Computing distributor balances"],
        compute,
        page_title="Loading Consignment Overview",
        page_note="Computing shelf stock and amount owed for every distributor with Consignment items.",
    )


@bp.route("/consignment/items")
@auth.permission_required("view_consignment")
def consignment_items():
    db = get_db()
    page = get_page()
    total = db.execute("SELECT COUNT(*) c FROM inventory_list WHERE category='Retail' AND active=true").fetchone()["c"]
    rows = db.execute(
        "SELECT i.*, d.name AS distributor_name FROM inventory_list i "
        "LEFT JOIN distributors d ON d.id = i.distributor_id "
        "WHERE i.category='Retail' AND i.active=true ORDER BY i.ownership_type DESC, i.name LIMIT ? OFFSET ?",
        (PER_PAGE, page_offset(page)),
    ).fetchall()
    distributors = db.execute("SELECT * FROM distributors ORDER BY name").fetchall()
    locked = {r["id"]: logic.consignment_item_locked(db, r["id"]) for r in rows if r["ownership_type"] == "Consignment"}
    return render_template("consignment_items.html", items=rows, distributors=distributors, locked=locked,
                            page=page, total_pages=page_count(total), total_count=total)


@bp.route("/consignment/items/bulk-edit", methods=["POST"])
@auth.permission_required("manage_consignment_items")
def consignment_items_bulk_edit():
    """
    Same batching rationale as inventory_catalog_bulk_edit() — one
    request, one transaction, instead of one click (and full page reload)
    per item. Flips ownership_type via an inline "Consignment?" checkbox +
    Distributor + Cost Price, saved together through the shared
    unsaved-changes.js Save Changes button, the same pattern Inventory
    Catalog's Track Expiry column already uses.

    A locked item (real receiving/sale/settlement activity already
    against it) is skipped entirely, silently — its checkbox/distributor
    are disabled client-side so a normal user can't reach this, but
    nothing here trusts that alone.
    """
    db = get_db()
    payload = request.get_json(silent=True) or {}
    items = payload.get("items") or []
    saved, errors = [], {}
    for item in items:
        item_id = str(item.get("id", ""))
        fields = item.get("fields") or {}
        old = db.execute("SELECT * FROM inventory_list WHERE id=?", (item_id,)).fetchone()
        if not old or old["category"] != "Retail":
            errors[item_id] = "Item not found."
            continue
        if logic.consignment_item_locked(db, item_id):
            continue
        want_consignment = fields.get("is_consignment") == "on"
        if want_consignment:
            distributor_id = fields.get("distributor_id") or None
            if not distributor_id:
                errors[item_id] = "Pick a distributor to flag this item as Consignment."
                continue
            try:
                cost_price = parse_money(fields.get("cost_price"), required=True)
            except BadNumber:
                errors[item_id] = "Cost Price is required and must be a valid number to flag an item as Consignment."
                continue
            if cost_price < 0:
                errors[item_id] = "Cost Price can't be negative."
                continue
            consignment_since = (
                old["consignment_since"] if old["ownership_type"] == "Consignment"
                else datetime.now().isoformat(timespec="seconds")
            )
            new_vals = {
                "ownership_type": "Consignment", "distributor_id": distributor_id,
                "cost_price": cost_price, "consignment_since": consignment_since,
            }
        else:
            new_vals = {
                "ownership_type": "Owned", "distributor_id": None,
                "cost_price": old["cost_price"], "consignment_since": old["consignment_since"],
            }
        changes = auth.diff_dict(old, new_vals)
        if not changes:
            continue
        db.execute(
            "UPDATE inventory_list SET ownership_type=?, distributor_id=?, cost_price=?, consignment_since=? WHERE id=?",
            (new_vals["ownership_type"], new_vals["distributor_id"], new_vals["cost_price"],
             new_vals["consignment_since"], item_id),
        )
        auth.log_change(db, "inventory_list", item_id, "update", changes)
        saved.append(item_id)
    db.commit()
    return jsonify({"ok": len(errors) == 0, "saved": saved, "errors": errors})


def _consignment_item_choices(db):
    """Consignment items for the Receiving/Shrinkage/Returns pickers,
    each with its distributor attached so the form can filter/label."""
    return db.execute(
        "SELECT i.id, i.name, i.unit, i.cost_price, i.distributor_id, d.name AS distributor_name "
        "FROM inventory_list i JOIN distributors d ON d.id = i.distributor_id "
        "WHERE i.ownership_type='Consignment' AND i.active=true ORDER BY d.name, i.name"
    ).fetchall()


def _consignment_receiving_page_context():
    db = get_db()
    page = get_page()
    total = db.execute("SELECT COUNT(*) c FROM consignment_receipts").fetchone()["c"]
    rows = db.execute(
        "SELECT cr.*, i.name AS item_name, d.name AS distributor_name FROM consignment_receipts cr "
        "JOIN inventory_list i ON i.id=cr.item_id JOIN distributors d ON d.id=cr.distributor_id "
        "ORDER BY cr.created_at DESC LIMIT ? OFFSET ?", (PER_PAGE, page_offset(page)),
    ).fetchall()
    return dict(receipts=rows, items=_consignment_item_choices(db),
                page=page, total_pages=page_count(total), total_count=total)


@bp.route("/consignment/receiving")
@auth.permission_required("view_consignment")
def consignment_receiving_page():
    return render_template("consignment_receiving.html", **_consignment_receiving_page_context())


@bp.route("/consignment/receiving/new", methods=["POST"])
@auth.permission_required("manage_consignment_stock")
def consignment_receiving_new():
    db = get_db()
    f = request.form

    def redisplay():
        ctx = _consignment_receiving_page_context()
        ctx["form"] = f
        ctx["open_new_form"] = True
        return render_template("consignment_receiving.html", **ctx)

    item_id = f.get("item_id")
    item = db.execute("SELECT * FROM inventory_list WHERE id=? AND ownership_type='Consignment'", (item_id,)).fetchone()
    if not item:
        flash("Pick a Consignment item first.", "error")
        return redisplay()
    try:
        quantity = parse_money(f.get("quantity"), required=True)
        unit_cost = parse_money(f.get("unit_cost"), required=True)
    except BadNumber:
        flash("Quantity and Unit Cost must be valid numbers.", "error")
        return redisplay()
    if quantity <= 0:
        flash("Quantity must be greater than 0.", "error")
        return redisplay()
    if unit_cost < 0:
        flash("Unit Cost can't be negative.", "error")
        return redisplay()
    try:
        received_date = clean_date(f.get("received_date"), field="received_date") or date.today().isoformat()
    except BadDate as e:
        flash(str(e), "error")
        return redisplay()
    logic.record_consignment_receipt(db, item_id, item["distributor_id"], quantity, unit_cost,
                                      received_date, f.get("delivery_reference"), f.get("notes"), session["user_id"])
    auth.log_change(db, "consignment_receipts", item_id, "create")
    db.commit()
    flash(f"Received {quantity:g} {item['name']}.", "success")
    return redirect(url_for("consignment.consignment_receiving_page"))


def _consignment_shrinkage_page_context():
    db = get_db()
    page = get_page()
    total = db.execute("SELECT COUNT(*) c FROM consignment_shrinkage").fetchone()["c"]
    rows = db.execute(
        "SELECT cs.*, i.name AS item_name, d.name AS distributor_name FROM consignment_shrinkage cs "
        "JOIN inventory_list i ON i.id=cs.item_id JOIN distributors d ON d.id=cs.distributor_id "
        "ORDER BY cs.logged_at DESC LIMIT ? OFFSET ?", (PER_PAGE, page_offset(page)),
    ).fetchall()
    return dict(lines=rows, items=_consignment_item_choices(db),
                page=page, total_pages=page_count(total), total_count=total)


@bp.route("/consignment/shrinkage")
@auth.permission_required("view_consignment")
def consignment_shrinkage_page():
    return render_template("consignment_shrinkage.html", **_consignment_shrinkage_page_context())


@bp.route("/consignment/shrinkage/new", methods=["POST"])
@auth.permission_required("manage_consignment_stock")
def consignment_shrinkage_new():
    db = get_db()
    f = request.form

    def redisplay():
        ctx = _consignment_shrinkage_page_context()
        ctx["form"] = f
        ctx["open_new_form"] = True
        return render_template("consignment_shrinkage.html", **ctx)

    item_id = f.get("item_id")
    item = db.execute("SELECT * FROM inventory_list WHERE id=? AND ownership_type='Consignment'", (item_id,)).fetchone()
    if not item:
        flash("Pick a Consignment item first.", "error")
        return redisplay()
    try:
        quantity = parse_money(f.get("quantity"), required=True)
    except BadNumber:
        flash("Quantity must be a valid number.", "error")
        return redisplay()
    if quantity <= 0:
        flash("Quantity must be greater than 0.", "error")
        return redisplay()
    reason = f.get("reason")
    if reason not in ("Damaged", "Expired", "Other"):
        flash("Reason must be Damaged, Expired, or Other.", "error")
        return redisplay()
    # Default liability by reason: Expired defaults to Distributor (bad
    # stock rotation on their end), Damaged/Other default to Clinic
    # (mishandled on-site) — either can be overridden per line.
    default_liable = "Distributor" if reason == "Expired" else "Clinic"
    liable_party = f.get("liable_party") or default_liable
    if liable_party not in ("Distributor", "Clinic"):
        flash("Liable Party must be Distributor or Clinic.", "error")
        return redisplay()
    overridden = liable_party != default_liable
    ok, _, error = logic.record_consignment_shrinkage(
        db, item_id, item["distributor_id"], quantity, reason, liable_party, overridden,
        f.get("notes"), session["user_id"],
    )
    if not ok:
        flash(error, "error")
        return redisplay()
    auth.log_change(db, "consignment_shrinkage", item_id, "create")
    db.commit()
    flash(f"Logged {quantity:g} {item['name']} as shrinkage ({liable_party} liable).", "success")
    return redirect(url_for("consignment.consignment_shrinkage_page"))


def _consignment_returns_page_context():
    db = get_db()
    page = get_page()
    total = db.execute("SELECT COUNT(*) c FROM consignment_returns").fetchone()["c"]
    rows = db.execute(
        "SELECT cr.*, i.name AS item_name, d.name AS distributor_name FROM consignment_returns cr "
        "JOIN inventory_list i ON i.id=cr.item_id JOIN distributors d ON d.id=cr.distributor_id "
        "ORDER BY cr.created_at DESC LIMIT ? OFFSET ?", (PER_PAGE, page_offset(page)),
    ).fetchall()
    return dict(returns=rows, items=_consignment_item_choices(db),
                page=page, total_pages=page_count(total), total_count=total)


@bp.route("/consignment/returns")
@auth.permission_required("view_consignment")
def consignment_returns_page():
    return render_template("consignment_returns.html", **_consignment_returns_page_context())


@bp.route("/consignment/returns/new", methods=["POST"])
@auth.permission_required("manage_consignment_stock")
def consignment_returns_new():
    db = get_db()
    f = request.form

    def redisplay():
        ctx = _consignment_returns_page_context()
        ctx["form"] = f
        ctx["open_new_form"] = True
        return render_template("consignment_returns.html", **ctx)

    item_id = f.get("item_id")
    item = db.execute("SELECT * FROM inventory_list WHERE id=? AND ownership_type='Consignment'", (item_id,)).fetchone()
    if not item:
        flash("Pick a Consignment item first.", "error")
        return redisplay()
    try:
        quantity = parse_money(f.get("quantity"), required=True)
    except BadNumber:
        flash("Quantity must be a valid number.", "error")
        return redisplay()
    if quantity <= 0:
        flash("Quantity must be greater than 0.", "error")
        return redisplay()
    try:
        return_date = clean_date(f.get("return_date"), field="return_date") or date.today().isoformat()
    except BadDate as e:
        flash(str(e), "error")
        return redisplay()
    ok, _, error = logic.record_consignment_return(
        db, item_id, item["distributor_id"], quantity, return_date, f.get("reason"), f.get("notes"), session["user_id"],
    )
    if not ok:
        flash(error, "error")
        return redisplay()
    auth.log_change(db, "consignment_returns", item_id, "create")
    db.commit()
    flash(f"Returned {quantity:g} {item['name']} to {item['distributor_id']}.", "success")
    return redirect(url_for("consignment.consignment_returns_page"))


@bp.route("/consignment/sales")
@auth.permission_required("view_consignment")
def consignment_sales_page():
    db = get_db()
    distributor_id = request.args.get("distributor_id") or None
    date_from = request.args.get("date_from") or None
    date_to = request.args.get("date_to") or None
    all_rows = logic.consignment_sales_by_distributor(db, distributor_id, date_from, date_to)
    page = get_page()
    total = len(all_rows)
    rows = all_rows[page_offset(page):page_offset(page) + PER_PAGE]
    distributors = db.execute(
        "SELECT DISTINCT d.id, d.name FROM distributors d JOIN inventory_list i ON i.distributor_id=d.id "
        "WHERE i.ownership_type='Consignment' ORDER BY d.name"
    ).fetchall()
    return render_template("consignment_sales.html", rows=rows, distributors=distributors,
                            distributor_id=distributor_id, date_from=date_from or "", date_to=date_to or "",
                            page=page, total_pages=page_count(total), total_count=total)


def _consignment_settlements_page_context(distributor_id):
    """Returns None if the distributor doesn't exist. Split out of
    consignment_settlements_page() so consignment_settlement_new() can
    re-render the same page (with `form` layered on top) on a validation
    failure instead of discarding the submitted data via redirect."""
    db = get_db()
    distributor = db.execute("SELECT * FROM distributors WHERE id=?", (distributor_id,)).fetchone()
    if not distributor:
        return None
    balance = logic.consignment_balance(db, distributor_id)
    history = db.execute(
        "SELECT s.*, u.full_name AS settled_by_name FROM consignment_settlements s "
        "LEFT JOIN users u ON u.id=s.settled_by WHERE s.distributor_id=? ORDER BY s.created_at DESC",
        (distributor_id,),
    ).fetchall()
    return dict(distributor=distributor, balance=balance, history=history)


@bp.route("/consignment/settlements/<distributor_id>")
@auth.permission_required("manage_consignment_settlements")
def consignment_settlements_page(distributor_id):
    ctx = _consignment_settlements_page_context(distributor_id)
    if ctx is None:
        flash("Distributor not found.", "error")
        return redirect(url_for("consignment.consignment_overview"))
    return render_template("consignment_settlements.html", **ctx)


@bp.route("/consignment/settlements/<distributor_id>/new", methods=["POST"])
@auth.permission_required("manage_consignment_settlements")
def consignment_settlement_new(distributor_id):
    db = get_db()
    # Locked before computing the balance — consignment_balance() reads
    # whatever settlement was most recently committed as its starting
    # point, so without this, two near-simultaneous submissions (double-
    # click, a retried request) could both read the same "last
    # settlement" before either commits, both compute a balance covering
    # the identical sales window, and both insert as separate settlement
    # rows — crediting/paying out the same batch of sales twice. The lock
    # is purely a mutex here (nothing about the distributor row itself
    # changes); same technique record_consignment_shrinkage() and
    # record_consignment_return() already use on inventory_list rows.
    distributor = db.execute("SELECT * FROM distributors WHERE id=? FOR UPDATE", (distributor_id,)).fetchone()
    if not distributor:
        flash("Distributor not found.", "error")
        return redirect(url_for("consignment.consignment_overview"))
    # Recomputed fresh at submit time, not trusted from a hidden form
    # field — the balance is a live figure (more could have sold since
    # the page was opened) and this is a cash-recording action, not
    # something to take on faith from the client.
    balance = logic.consignment_balance(db, distributor_id)

    def redisplay():
        history = db.execute(
            "SELECT s.*, u.full_name AS settled_by_name FROM consignment_settlements s "
            "LEFT JOIN users u ON u.id=s.settled_by WHERE s.distributor_id=? ORDER BY s.created_at DESC",
            (distributor_id,),
        ).fetchall()
        return render_template("consignment_settlements.html", distributor=distributor, balance=balance,
                                history=history, form=request.form)

    try:
        amount_paid = parse_money(request.form.get("amount_paid"), required=True)
    except BadNumber:
        flash("Amount Paid must be a valid number.", "error")
        return redisplay()
    if amount_paid < 0:
        flash("Amount Paid can't be negative.", "error")
        return redisplay()
    # Nothing to settle: either this distributor has had no consignment
    # activity at all (in which case balance["period_start"] is None and the
    # INSERT below would violate consignment_settlements.period_start's NOT
    # NULL constraint, surfacing as an error page), or the period is already
    # settled and this would record a no-op row. Both are refused here rather
    # than reaching the database.
    if balance["period_start"] is None or balance["amount_owed"] <= 0:
        flash("There's nothing to settle for this distributor yet.", "error")
        return redisplay()
    if amount_paid > balance["amount_owed"]:
        flash(f"That's more than the {logic.fmt_money(balance['amount_owed'])} JOD owed this period.", "error")
        return redisplay()
    amount_paid = round(amount_paid, 3)
    cur = db.execute(
        "INSERT INTO consignment_settlements (distributor_id, period_start, period_end, amount_owed, amount_paid, "
        "payment_method, notes, settled_by, created_at) VALUES (?,?,?,?,?,?,?,?,?) RETURNING id",
        (distributor_id, balance["period_start"], balance["period_end"], balance["amount_owed"], amount_paid,
         request.form.get("payment_method"), request.form.get("notes"), session["user_id"],
         datetime.now().isoformat(timespec="seconds")),
    )
    settlement_id = cur.fetchone()["id"]
    auth.log_change(db, "consignment_settlements", str(settlement_id), "create")
    db.commit()
    residual = round(balance["amount_owed"] - amount_paid, 3)
    if residual > 0:
        flash(f"Settlement recorded: {logic.fmt_money(amount_paid)} JOD paid of "
              f"{logic.fmt_money(balance['amount_owed'])} JOD owed — {logic.fmt_money(residual)} JOD carries forward.", "success")
    else:
        flash(f"Settlement recorded: {logic.fmt_money(amount_paid)} JOD paid, settled in full.", "success")
    return redirect(url_for("consignment.consignment_settlements_page", distributor_id=distributor_id))


@bp.route("/consignment/settlements/export/<int:settlement_id>")
@auth.permission_required("manage_consignment_settlements")
def consignment_settlement_export(settlement_id):
    db = get_db()
    settlement = db.execute("SELECT id FROM consignment_settlements WHERE id=?", (settlement_id,)).fetchone()
    if not settlement:
        flash("Settlement not found.", "error")
        return redirect(url_for("consignment.consignment_overview"))
    buf = pdf_export.export_consignment_settlement_pdf(db, settlement_id)
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=f"settlement_{settlement_id}.pdf")
