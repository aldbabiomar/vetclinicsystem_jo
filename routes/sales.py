"""
Money changing hands: the point-of-sale till, refunds against a sale or a service, and the end-of-day cash register.

Split out of app.py. Shared request-layer pieces come from core.py rather than
app.py -- app.py registers this blueprint, so importing from it here would be
circular.

Endpoint names carry the `sales.` prefix Flask gives every blueprint route:
`url_for("sales.api_sale_refundable_items")`, not `url_for("sales.api_sale_refundable_items")`.
"""

from datetime import date
from datetime import datetime
from decimal import Decimal
import auth
import db as dbmod
import logic
import pdf_export
import uuid

from flask import (
    Blueprint, abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for
)

from core import BadDate, BadNumber, PAYMENT_METHODS, PER_PAGE, clean_date, clean_date_filter, cleanup_amount_error, date_filter_arg, discount_percent_error, get_db, get_page, page_count, page_offset, parse_money, parse_quantity

bp = Blueprint("sales", __name__)


@bp.route("/api/sales/<int:sale_id>/refundable-items")
@auth.permission_required("manage_refunds")
def api_sale_refundable_items(sale_id):
    sale, lines = logic.refundable_sale_items(get_db(), sale_id)
    if not sale:
        return jsonify({"error": "No sale with that ID."}), 404
    return jsonify({
        "sale_id": sale["id"],
        "sale_date": sale["sale_date"],
        "sale_total": sale["total"],
        "cleanup_amount": sale["cleanup_amount"] or 0,
        "lines": [
            {
                "sale_item_id": l["sale_item_id"],
                "item_id": l["item_id"],
                "name": l["name"],
                "unit_price": l["unit_price"],
                "quantity": l["quantity"],
                "remaining": l["remaining"],
            }
            for l in lines
        ],
    })


@bp.route("/pos/history/<int:sale_id>/export")
@auth.permission_required("view_sales_history")
def pos_export_receipt(sale_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM sales WHERE id=?", (sale_id,)).fetchone():
        abort(404)
    buf = pdf_export.export_sale_receipt(db, sale_id)
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=f"sale_{sale_id}_receipt.pdf")


# ---------------------------------------------------------------------------
# Cash Register — a unified daily view of every place money actually
# changed hands (POS sales, Visit/Inpatient/Boarding payments, refunds),
# built for end-of-day cash-up: compare what the system says came in
# against what's physically in the till. "Pay From Cash Register" logs
# manual cash leaving the drawer for a reason that isn't a refund (petty
# cash, paying a supplier directly out of the till). "Perform Audit"
# records what staff actually counted against the system's Cash total for
# that day and immutably logs the outcome (Deficit/Surplus/Perfect) — see
# logic.cash_register_* for the actual math.
# ---------------------------------------------------------------------------
def _cash_register_page_context(day):
    db = get_db()
    ledger = logic.cash_register_ledger(db, day)
    totals = logic.cash_register_totals(db, day)
    payouts = logic.cash_register_payouts_for_day(db, day)
    latest_audit = logic.cash_register_latest_audit(db, day)
    return dict(day=day, ledger=ledger, totals=totals, payouts=payouts, latest_audit=latest_audit)


@bp.route("/cash-register")
@auth.permission_required("manage_cash_register")
def cash_register_page():
    day = request.args.get("date", "").strip() or date.today().isoformat()
    try:
        logic.parse_date(day)
    except ValueError:
        flash("That date wasn't valid — showing today instead.", "error")
        day = date.today().isoformat()
    return render_template("cash_register.html", **_cash_register_page_context(day))


@bp.route("/cash-register/payout", methods=["POST"])
@auth.permission_required("manage_cash_register")
def cash_register_payout_new():
    db = get_db()
    f = request.form

    def redisplay(day):
        ctx = _cash_register_page_context(day)
        ctx["payout_form"] = f
        return render_template("cash_register.html", **ctx)

    try:
        day = clean_date(f.get("day"), field="day") or date.today().isoformat()
    except BadDate as e:
        flash(str(e), "error")
        return redisplay(date.today().isoformat())
    try:
        amount = parse_money(f.get("amount"), required=True)
    except BadNumber:
        flash("Amount must be a valid number.", "error")
        return redisplay(day)
    if amount <= 0:
        flash("Amount must be greater than 0.", "error")
        return redisplay(day)
    reason = (f.get("reason") or "").strip()
    if not reason:
        flash("Enter a reason for this payout.", "error")
        return redisplay(day)
    # Recomputed fresh at submit time — cash_register_totals() already
    # subtracts every payout already logged for this day, so this is
    # exactly how much is left in the drawer before this new one.
    drawer_cash = logic.cash_register_totals(db, day)["Cash"]
    if amount > drawer_cash:
        flash(f"That's more than the {logic.fmt_money(drawer_cash)} JOD currently expected in the drawer for this day.", "error")
        return redisplay(day)
    cur = db.execute(
        "INSERT INTO cash_register_payouts (payout_date, amount, reason, logged_by, created_at) "
        "VALUES (?,?,?,?,?) RETURNING id",
        (day, amount, reason, session["user_id"], datetime.now().isoformat(timespec="seconds")),
    )
    payout_id = cur.fetchone()["id"]
    auth.log_change(db, "cash_register_payouts", str(payout_id), "create")
    db.commit()
    flash(f"{logic.fmt_money(amount)} JOD logged out of the register.", "success")
    return redirect(url_for("sales.cash_register_page", date=day))


@bp.route("/cash-register/audit", methods=["POST"])
@auth.permission_required("manage_cash_register")
def cash_register_audit_new():
    db = get_db()
    f = request.form

    def redisplay(day):
        ctx = _cash_register_page_context(day)
        ctx["audit_form"] = f
        return render_template("cash_register.html", **ctx)

    try:
        day = clean_date(f.get("day"), field="day") or date.today().isoformat()
    except BadDate as e:
        flash(str(e), "error")
        return redisplay(date.today().isoformat())
    try:
        counted_cash = parse_money(f.get("counted_cash"), required=True)
    except BadNumber:
        flash("Counted cash must be a valid number.", "error")
        return redisplay(day)
    if counted_cash < 0:
        flash("Counted cash can't be negative.", "error")
        return redisplay(day)
    # Recomputed fresh here, never trusted from the form — same reasoning
    # as consignment_settlement_new(): this is the figure the audit result
    # gets permanently compared against, so it has to be the real live
    # number, not whatever the page happened to show when it was loaded.
    totals = logic.cash_register_totals(db, day)
    difference = round(counted_cash - totals["Cash"], 3)
    if abs(difference) < 1:
        status = "Perfect"
    elif difference < 0:
        status = "Deficit"
    else:
        status = "Surplus"
    cur = db.execute(
        "INSERT INTO cash_register_audits (audit_date, system_cash, system_card, system_transfer, "
        "counted_cash, difference, status, notes, performed_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?) RETURNING id",
        (day, totals["Cash"], totals["Card"], totals["Transfer"], counted_cash, difference, status,
         (f.get("notes") or "").strip() or None, session["user_id"], datetime.now().isoformat(timespec="seconds")),
    )
    audit_id = cur.fetchone()["id"]
    auth.log_change(db, "cash_register_audits", str(audit_id), "create")
    db.commit()
    if status == "Perfect":
        flash(f"Audit recorded for {day}: Perfect — counted cash matches the system exactly.", "success")
    else:
        # "warning", not "error": the audit DID save. Flashing a saved
        # record in the same red as a failure reads as "that did not work"
        # and invites staff to re-run the count. The discrepancy still
        # needs attention, which is what the warning state is for.
        flash(f"Audit recorded for {day}: {status} of {logic.fmt_money(abs(difference))} JOD.", "warning")
    return redirect(url_for("sales.cash_register_page", date=day))


# ---------------------------------------------------------------------------
# Point of Sale (Retail only)
# ---------------------------------------------------------------------------
@bp.route("/pos")
@auth.permission_required("process_pos_sales")
def pos_page():
    db = get_db()
    cap = auth.discount_cap_for()
    # Fresh one-time token per page load — see pos_checkout()'s dedup
    # check and idx_sales_idempotency_key in schema_postgres.sql.
    return render_template("pos.html", discount_cap=cap, idempotency_key=uuid.uuid4().hex)


# ---------------------------------------------------------------------------
# pos_checkout's steps, extracted (review finding M4)
#
# Each helper returns (value…, error) with `error` a message string or None,
# matching the discount_percent_error() / cleanup_amount_error() convention
# already in core.py. None of them flashes and none of them renders: the route
# owns the response, so a helper cannot return a page from three frames down —
# which is the failure mode that makes a long route hard to change safely.
#
# These are NOT shared with IQ, and must not become shared. JO prices in exact
# 3-decimal `Decimal` with no denomination rounding anywhere, and parses cart
# quantities with parse_quantity(); IQ prices in `float` and rounds every
# payable figure to the nearest 250-IQD note. A line copied across in either
# direction is a TypeError at best and a silent precision bug at worst.
# COMPARISON.md §1.1 and CLAUDE.md §2.
# ---------------------------------------------------------------------------


def _merged_cart_quantities(item_ids, quantities):
    """Sum the submitted lines per item.

    The UI cart already merges duplicates client-side, but nothing on the
    server enforced that — checking each submitted line against the *live*
    current_stock independently meant two lines of the same item (3 + 3
    against a stock of 5) could each individually pass and together oversell
    it. Merging first is what makes the stock check below mean anything.
    """
    qty_by_item = {}
    for iid, qty in zip(item_ids, quantities):
        try:
            qty = parse_quantity(qty, required=True)
        except BadNumber:
            return None, "Cart quantities must be valid numbers."
        if qty <= 0:
            continue
        qty_by_item[iid] = qty_by_item.get(iid, 0) + qty
    return qty_by_item, None


def _lock_and_snapshot_cart_items(db, qty_by_item):
    """Lock every cart item's row, then snapshot cost and distributor.

    Locking in a *fixed* order — sorted by id, never "the order the items
    happen to be in this cart" — is what closes the oversell race and what
    stops two carts sharing two items from deadlocking on each other (cart A
    locks item1 then waits on item2 while cart B does the reverse). Previously
    two concurrent checkouts for the same item could both read "5 in stock"
    before either had written its sale. Now the second SELECT ... FOR UPDATE
    blocks until the first transaction commits or rolls back, and Postgres
    gives that blocked SELECT a fresh read once it proceeds.

    Cost basis is snapshotted alongside price at checkout time, so COGS
    reporting reflects what this item actually cost when it was sold — not
    whatever inventory_list.cost_price says whenever the report is later run
    (see sale_items.unit_cost's own column comment). Read once, right after
    locking: it is the cost that will be recorded against this sale.
    Distributor is snapshotted for the same reason — it is what makes
    consignment_balance()'s attribution stable against a later distributor
    re-point. ORPHANED_RECORDS_AUDIT.md F-07.
    """
    for iid in sorted(qty_by_item.keys()):
        db.execute("SELECT id FROM inventory_list WHERE id=? FOR UPDATE", (iid,))
    if not qty_by_item:
        return {}, {}
    item_rows = {r["id"]: r for r in db.execute(
        "SELECT id, cost_price, distributor_id FROM inventory_list WHERE id IN ("
        + ",".join("?" * len(qty_by_item)) + ")",
        list(qty_by_item.keys()),
    ).fetchall()}
    return ({iid: r["cost_price"] for iid, r in item_rows.items()},
            {iid: r["distributor_id"] for iid, r in item_rows.items()})


def _priced_cart_lines(db, qty_by_item, cost_by_item, distributor_by_item):
    """Price each line and check it against stock.

    Returns (subtotal, lines, notices, error). `notices` are non-fatal
    messages the caller flashes in submitted order before any error, so the
    "skipped, no sale price" message still arrives ahead of a later failure
    exactly as it did when this was one long function.
    """
    subtotal, lines, notices = 0, [], []
    for iid, qty in qty_by_item.items():
        price = logic.item_sale_price(db, iid)
        if price is None:
            notices.append(f"Item {iid} has no sale price set in the Price List — skipped.")
            continue
        status = logic.inventory_status_by_id(db, iid)
        # current_stock is None until this item has been through at least one
        # confirmed inventory audit — treated as zero available stock here
        # (fail closed) rather than skipping the check, since skipping it let
        # a never-audited item be oversold via POS with no limit at all,
        # silently and deterministically (not just under a race). A clinic
        # sells a brand-new item for the first time by running a quick audit
        # on it first, same as any other item.
        # `!= itself` is the NaN test, and it has to happen BEFORE the
        # comparison below: here `qty` is a Decimal, so `qty > nan` does not
        # quietly return False the way it does in IQ -- it raises
        # decimal.InvalidOperation and 500s the whole checkout.
        # _save_audit_lines() now rejects NaN at entry; this is the second
        # layer, so a count that predates that fix fails closed with the
        # same message as a never-audited item instead of taking the till
        # down. "No usable count" is what both branches mean.
        if status and (status["current_stock"] is None
                       or status["current_stock"] != status["current_stock"]):
            return 0, [], notices, (
                f"{status['name']} hasn't been through an inventory audit yet — "
                "run an audit before selling it.")
        if status and qty > status["current_stock"]:
            return 0, [], notices, (
                f"Only {status['current_stock']} {status['unit'] or ''} of "
                f"{status['name']} in stock — sale blocked.")
        line_total = price * qty
        subtotal += line_total
        lines.append((iid, qty, price, line_total,
                      cost_by_item.get(iid), distributor_by_item.get(iid)))
    return subtotal, lines, notices, None


def _cash_payment_for(f, total):
    """Resolve cash received and change due. Returns (received, change, error).

    Non-cash payments resolve to (None, None, None) — the columns stay null
    rather than storing a zero that would look like "paid nothing in cash".

    Change is exact to the fils: JOD has no denomination floor, so unlike IQ
    there is nothing to round down to and no clinic-absorbed remainder.
    """
    if f.get("payment_method") != "Cash":
        return None, None, None
    try:
        cash_received = parse_money(f.get("cash_received"))
    except BadNumber:
        return None, None, "Cash Received must be a valid number."
    if cash_received is None:
        return None, None, None
    if cash_received < total:
        return None, None, (f"Cash received ({cash_received:,.3f} JOD) is less than "
                            f"the total ({total:,.3f} JOD).")
    return cash_received, max(round(cash_received - total, 3), 0), None


def _record_sale(db, lines, *, subtotal, discount_percent, total, cleanup_amount,
                 payment_method, cash_received, change_given, idempotency_key, now):
    """Write the sale, its lines, and the stock movements. Returns the sale id.

    Caller commits — this deliberately does not, so the whole checkout stays
    one transaction and the row locks taken above are still held while these
    rows are written.
    """
    cur = db.execute(
        "INSERT INTO sales (sale_date, cashier_id, subtotal, discount_percent, discount_applied_by, total, "
        "payment_method, cash_received, change_given, idempotency_key, cleanup_amount, cleanup_applied_by) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
        (now, session["user_id"], round(subtotal, 3), discount_percent,
         session["user_id"] if discount_percent else None, total, payment_method,
         cash_received, change_given, idempotency_key, cleanup_amount,
         session["user_id"] if cleanup_amount else None),
    )
    sale_id = cur.fetchone()["id"]
    for iid, qty, price, line_total, unit_cost, distributor_id in lines:
        db.execute(
            "INSERT INTO sale_items (sale_id, item_id, quantity, unit_price, line_total, unit_cost, distributor_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (sale_id, iid, qty, price, round(line_total, 3), unit_cost, distributor_id))
        db.execute(
            "INSERT INTO inventory_transactions (item_id, change_qty, reason, ref_id, timestamp, user_id) "
            "VALUES (?,?,?,?,?,?)",
            (iid, -qty, "sale", str(sale_id), now, session["user_id"]))
    logic.recompute_month_summary(db, now[:7])
    auth.log_change(db, "sales", str(sale_id), "create")
    return sale_id


@bp.route("/pos/checkout", methods=["POST"])
@auth.permission_required("process_pos_sales")
def pos_checkout():
    db = get_db()
    f = request.form

    def redisplay():
        # Special case (see the porting task's own notes on this route):
        # the cart itself is built entirely client-side in JS and never
        # round-trips as visible form fields except at submit time, so
        # restoring it here would mean re-deriving names/prices/stock for
        # every line server-side and re-hydrating the JS `cart` array —
        # disproportionate effort for this pass. What IS cheap and worth
        # doing: discount %, payment method, and cash received are plain
        # fields the person typed, so those come back via fv() below. The
        # cart itself starts empty again, same as a fresh /pos page load.
        cap = auth.discount_cap_for()
        return render_template("pos.html", discount_cap=cap,
                                idempotency_key=f.get("idempotency_key") or uuid.uuid4().hex, form=f)

    def refuse(message):
        flash(message, "error")
        return redisplay()

    # Friendly fast-path for a double-click on "Complete Sale" — the same
    # unchanged cart submitted twice previously created two separate,
    # fully valid sales (double-charge, double stock deduction). The
    # token is one-time per POS page load (see pos_page()); a second
    # submission carrying the same token is recognized here as a repeat
    # of a sale that already went through, and sent straight to that
    # sale's receipt instead of creating another one. Not the real
    # guarantee — see the IntegrityError catch below for that.
    idempotency_key = f.get("idempotency_key") or None
    if idempotency_key:
        existing_sale = db.execute("SELECT id FROM sales WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if existing_sale:
            return redirect(url_for("sales.pos_receipt", sale_id=existing_sale["id"]))

    item_ids = request.form.getlist("item_id")
    quantities = request.form.getlist("quantity")
    try:
        discount_percent = parse_money(f.get("discount_percent")) or 0
    except BadNumber:
        return refuse("Discount must be a valid number.")
    cap = auth.discount_cap_for()
    error = discount_percent_error(discount_percent, cap)
    if error:
        return refuse(error)
    if not item_ids:
        return refuse("Cart is empty.")
    if discount_percent > 0:
        blocked = logic.non_discountable_line_names_for_items(db, item_ids)
        if blocked:
            return refuse("Can't apply a discount — the cart includes item(s) marked as "
                          f"not discountable: {', '.join(blocked)}.")

    qty_by_item, error = _merged_cart_quantities(item_ids, quantities)
    if error:
        return refuse(error)

    cost_by_item, distributor_by_item = _lock_and_snapshot_cart_items(db, qty_by_item)
    subtotal, lines, notices, error = _priced_cart_lines(
        db, qty_by_item, cost_by_item, distributor_by_item)
    for notice in notices:
        flash(notice, "error")
    if error:
        return refuse(error)
    if not lines:
        return refuse("Nothing to sell.")

    total = round(subtotal * (1 - discount_percent / Decimal(100)), 3)
    try:
        cleanup_amount = parse_money(f.get("cleanup_amount")) or 0
    except BadNumber:
        return refuse("Clean Up amount must be a valid number.")
    # existing_amount=0: a brand-new sale has no prior Clean Up to accumulate
    # against, unlike the other three surfaces.
    error = cleanup_amount_error(cleanup_amount, 0, total)
    if error:
        return refuse(error)
    total = max(total - cleanup_amount, 0)

    cash_received, change_given, error = _cash_payment_for(f, total)
    if error:
        return refuse(error)

    # Microsecond precision — see the matching comment on audit_confirm's
    # confirmed_at write; a sale timestamped in the same second as an audit
    # confirmation would otherwise tie under the strict '>' stock-since-audit
    # comparison and get silently excluded from inventory_status()'s total.
    now = datetime.now().isoformat(timespec="microseconds")
    try:
        sale_id = _record_sale(
            db, lines, subtotal=subtotal, discount_percent=discount_percent, total=total,
            cleanup_amount=cleanup_amount, payment_method=f.get("payment_method"),
            cash_received=cash_received, change_given=change_given,
            idempotency_key=idempotency_key, now=now)
        db.commit()
    except dbmod.IntegrityError:
        # The fast-path check above isn't atomic — two near-simultaneous
        # submissions carrying the same idempotency_key can both pass it
        # before either commits. idx_sales_idempotency_key is what
        # actually prevents the double sale; this catches the resulting
        # IntegrityError for whichever request loses that race and sends
        # it to the sale that won instead of erroring.
        db.rollback()
        if idempotency_key:
            existing_sale = db.execute("SELECT id FROM sales WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if existing_sale:
                return redirect(url_for("sales.pos_receipt", sale_id=existing_sale["id"]))
        raise
    flash(f"Sale #{sale_id} completed — total {logic.fmt_money(total)} JOD.", "success")
    return redirect(url_for("sales.pos_receipt", sale_id=sale_id))


@bp.route("/pos/receipt/<int:sale_id>")
@auth.permission_required("process_pos_sales", "view_sales_history")
def pos_receipt(sale_id):
    db = get_db()
    sale = db.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
    if sale is None:
        flash("Sale not found.", "error")
        return redirect(url_for("sales.pos_history"))
    items = db.execute(
        "SELECT si.*, i.name FROM sale_items si JOIN inventory_list i ON i.id=si.item_id WHERE si.sale_id=?", (sale_id,)
    ).fetchall()
    return render_template("pos_receipt.html", sale=sale, items=items)


@bp.route("/pos/history")
@auth.permission_required("view_sales_history")
def pos_history():
    db = get_db()
    page = get_page()
    date_filter = date_filter_arg()
    where = " WHERE s.sale_date LIKE ?" if date_filter else ""
    params = [date_filter + "%"] if date_filter else []
    total = db.execute(f"SELECT COUNT(*) c FROM sales s{where}", params).fetchone()["c"]
    sales = db.execute(
        f"SELECT s.*, u.full_name as cashier_name FROM sales s LEFT JOIN users u ON u.id=s.cashier_id{where} "
        "ORDER BY s.sale_date DESC LIMIT ? OFFSET ?", params + [PER_PAGE, page_offset(page)]
    ).fetchall()
    return render_template("pos_history.html", sales=sales, date_filter=date_filter,
                            page=page, total_pages=page_count(total), total_count=total)


def _refunds_page_context():
    """Builds the template context for refunds.html. Split out of
    refunds_page() so refund_retail_save()/refund_service_save() can
    re-render the same page (with `form` layered on top) on a validation
    failure instead of discarding the submitted data via redirect."""
    db = get_db()
    page = get_page()
    date_filter = clean_date_filter(request.args.get("date"))
    count_where = " WHERE refund_date = ?" if date_filter else ""
    count_params = [date_filter] if date_filter else []
    total = db.execute(f"SELECT COUNT(*) c FROM refunds{count_where}", count_params).fetchone()["c"]
    refunds = logic.recent_refunds(db, limit=PER_PAGE, offset=page_offset(page), date_filter=date_filter)
    return dict(refunds=refunds, today=date.today().isoformat(), date_filter=date_filter,
                page=page, total_pages=page_count(total), total_count=total)


@bp.route("/refunds")
@auth.permission_required("manage_refunds")
def refunds_page():
    # Side-effect only: warns if ?date= was present but unusable.
    # _refunds_page_context() re-reads and re-parses it, deliberately in
    # silence — refund_retail_save()/refund_service_save() re-render through
    # that same builder on a validation failure, and must not stack a date
    # warning on top of the real error. IQ avoids the double-read by taking
    # date_filter as a parameter; JO's builder takes none, so the warning
    # lives here instead of restructuring three call sites for it.
    date_filter_arg()
    return render_template("refunds.html", **_refunds_page_context())


@bp.route("/refunds/retail", methods=["POST"])
@auth.permission_required("manage_refunds")
def refund_retail_save():
    db = get_db()
    f = request.form

    def redisplay():
        # Genuinely complex client-side-JS-driven multi-item builder, same
        # category as pos_checkout()'s cart (sale lookup + per-line refund
        # quantities that only exist as in-page JS state) — full
        # restoration isn't required for this pass. The plain fields
        # (date/method/reason/restock) are cheap to restore via fv(), so
        # those come back; the looked-up sale and its line quantities do
        # not — the person re-enters the sale ID and re-picks quantities.
        ctx = _refunds_page_context()
        ctx["retail_form"] = f
        return render_template("refunds.html", **ctx)

    try:
        sale_id = int(f.get("sale_id", ""))
    except (TypeError, ValueError):
        flash("Look up a sale first — a retail refund must be linked to the sale it's refunding.", "error")
        return redisplay()
    sale_item_ids_raw = f.getlist("sale_item_id")
    quantities = f.getlist("quantity")
    restock = f.get("restock") == "on"
    # Without this the refund is recorded but how the money physically left
    # the clinic is not -- which is precisely what cash reconciliation reads.
    # The dropdown's default option is empty, so this was reachable straight
    # from the UI, not just from a crafted POST. IQ has always validated it.
    refund_method = f.get("refund_method")
    if refund_method not in PAYMENT_METHODS:
        flash("Pick how this refund was actually paid out: " + ", ".join(PAYMENT_METHODS) + ".", "error")
        return redisplay()
    reason = (f.get("reason") or "").strip()
    try:
        refund_date = clean_date(f.get("refund_date"), field="refund_date") or date.today().isoformat()
    except BadDate as e:
        flash(str(e), "error")
        return redisplay()

    if not sale_item_ids_raw:
        flash("No items selected — nothing to refund.", "error")
        return redisplay()
    try:
        sale_item_ids = [int(sid) for sid in sale_item_ids_raw]
    except ValueError:
        flash("Invalid item selection.", "error")
        return redisplay()

    # Lock every sale_items row being refunded, in a fixed order, before
    # computing how much of each is still refundable — same reasoning as
    # pos_checkout()'s stock-row locking: without this, two concurrent
    # refunds against the same sale could each read "2 remaining" and both
    # submit, over-refunding a sale that only had 2 to give back.
    for sid in sorted(set(sale_item_ids)):
        db.execute("SELECT id FROM sale_items WHERE id=? AND sale_id=? FOR UPDATE", (sid, sale_id))

    sale, refundable = logic.refundable_sale_items(db, sale_id)
    if not sale:
        flash("Sale not found.", "error")
        return redisplay()
    remaining_by_id = {l["sale_item_id"]: l for l in refundable}

    lines, total = [], 0
    for sid, qty_raw in zip(sale_item_ids, quantities):
        try:
            qty = parse_quantity(qty_raw, required=True)
        except BadNumber:
            flash("Refund quantities must be valid numbers.", "error")
            return redisplay()
        if qty <= 0:
            continue
        # Priced from what this sale actually charged per unit
        # (refundable_sale_items()'s discount-adjusted unit_price) — never
        # re-looked-up against today's Price List, which may have changed
        # since the sale.
        line = remaining_by_id.get(sid)
        if not line:
            flash("One of the selected items isn't part of that sale.", "error")
            return redisplay()
        if qty > line["remaining"]:
            flash(f"Can't refund {qty:g} {line['name']} — only {line['remaining']:g} left refundable from this sale.", "error")
            return redisplay()
        price = line["unit_price"]
        line_total = round(price * qty, 3)
        total += line_total
        lines.append((line["item_id"], sid, qty, price, line_total))

    if not lines:
        flash("Nothing to refund.", "error")
        return redisplay()

    # Microsecond precision — same reasoning as pos_checkout()'s `now`
    # (restocking here writes an inventory_transactions row too).
    now = datetime.now().isoformat(timespec="microseconds")
    rounded_total = round(total, 3)
    # Aggregate cap — see CLEANUP_FEATURE_PLAN.md §3.7/§4.5. Per-line
    # pricing above is untouched; this only stops the SUM of every retail
    # refund against this sale from exceeding what the sale actually
    # collected (sale["total"] already reflects any Clean Up applied at
    # sale time, so no separate reference to cleanup_amount is needed
    # here).
    already_refunded_total = db.execute(
        "SELECT COALESCE(SUM(amount),0) s FROM refunds WHERE sale_id=? AND refund_type='retail'", (sale_id,)
    ).fetchone()["s"]
    if already_refunded_total + rounded_total > sale["total"]:
        flash(f"That's more than this sale actually collected ({logic.fmt_money(sale['total'])} JOD, after any "
              f"Clean Up applied at sale time) minus what's already been refunded.", "error")
        return redisplay()
    cur = db.execute(
        "INSERT INTO refunds (refund_type, refund_date, amount, restocked, sale_id, reason, refund_method, "
        "processed_by, created_at, cleanup_amount_at_refund) VALUES ('retail',?,?,?,?,?,?,?,?,?) RETURNING id",
        (refund_date, rounded_total, restock, sale_id, reason, refund_method, session["user_id"], now,
         sale["cleanup_amount"] or 0),
    )
    refund_id = cur.fetchone()["id"]

    for iid, sid, qty, price, line_total in lines:
        db.execute(
            "INSERT INTO refund_items (refund_id, item_id, quantity, unit_price, line_total, sale_item_id) "
            "VALUES (?,?,?,?,?,?)",
            (refund_id, iid, qty, price, line_total, sid),
        )
        if restock:
            db.execute(
                "INSERT INTO inventory_transactions (item_id, change_qty, reason, ref_id, timestamp, user_id) "
                "VALUES (?,?,?,?,?,?)",
                (iid, qty, "refund", str(refund_id), now, session["user_id"]),
            )

    logic.recompute_month_summary(db, logic.month_key(refund_date))
    auth.log_change(db, "refunds", str(refund_id), "create")
    db.commit()
    flash(f"Refund of {total:,.3f} JOD recorded" + (" and stock restored." if restock else "."), "success")
    return redirect(url_for("sales.refunds_page"))


@bp.route("/refunds/service", methods=["POST"])
@auth.permission_required("manage_refunds")
def refund_service_save():
    db = get_db()
    f = request.form

    def redisplay():
        ctx = _refunds_page_context()
        ctx["service_form"] = f
        return render_template("refunds.html", **ctx)

    try:
        amount = parse_money(f.get("amount")) or 0
    except BadNumber:
        flash("Refund amount must be a valid number.", "error")
        return redisplay()
    reason = (f.get("reason") or "").strip()
    # Same reasoning as the retail refund above: a recorded refund with no
    # payout method leaves no trace of how the money left the clinic.
    refund_method = f.get("refund_method")
    if refund_method not in PAYMENT_METHODS:
        flash("Pick how this refund was actually paid out: " + ", ".join(PAYMENT_METHODS) + ".", "error")
        return redisplay()
    try:
        refund_date = clean_date(f.get("refund_date"), field="refund_date") or date.today().isoformat()
    except BadDate as e:
        flash(str(e), "error")
        return redisplay()
    visit_id = (f.get("visit_id") or "").strip() or None
    case_id_raw = (f.get("inpatient_case_id") or "").strip()
    boarding_id_raw = (f.get("boarding_id") or "").strip()

    if amount <= 0:
        flash("Refund amount must be greater than 0.", "error")
        return redisplay()
    # A service refund always reverses one specific visit or one specific
    # inpatient case — never both at once, and never neither. A goodwill/
    # no-specific-record refund is handled through Cash Register instead,
    # not this table. See ORPHANED_RECORDS_AUDIT.md F-05.
    # Boarding was missing here until 2026-09-10: payments has anchored on
    # visit / inpatient case / boarding since it existed, so a boarding stay
    # could be paid for and there was no way to hand the money back through
    # this page.
    if [bool(visit_id), bool(case_id_raw), bool(boarding_id_raw)].count(True) != 1:
        flash("A service refund must be linked to exactly one visit, inpatient case, "
              "or boarding stay.", "error")
        return redisplay()

    # Locked before computing the cap — same reasoning as
    # consignment_settlement_new()/boarding_payment(): without this, two
    # near-simultaneous service refunds against the same visit/case could
    # each read the same "amount paid so far minus prior refunds" before
    # either commits, and both pass a cap check that together they exceed.
    if visit_id:
        if not db.execute("SELECT 1 FROM visits WHERE id=? FOR UPDATE", (visit_id,)).fetchone():
            flash(f"Visit {visit_id} not found.", "error")
            return redisplay()
        paid = logic.visit_billing_summary(db, visit_id)["paid"]
        already_refunded = db.execute(
            "SELECT COALESCE(SUM(amount),0) s FROM refunds WHERE refund_type='service' AND visit_id=?", (visit_id,)
        ).fetchone()["s"]
        cap = paid - already_refunded
        if amount > cap:
            flash(f"That's more than what's left refundable on this visit ({logic.fmt_money(cap)} JOD).", "error")
            return redisplay()

    case_id = None
    if case_id_raw:
        if not case_id_raw.isdigit() or not db.execute(
            "SELECT 1 FROM inpatient_cases WHERE id=? FOR UPDATE", (int(case_id_raw),)
        ).fetchone():
            flash(f"Inpatient case {case_id_raw} not found.", "error")
            return redisplay()
        case_id = int(case_id_raw)
        paid = logic.inpatient_billing_summary(db, case_id)["paid"]
        already_refunded = db.execute(
            "SELECT COALESCE(SUM(amount),0) s FROM refunds WHERE refund_type='service' AND inpatient_case_id=?", (case_id,)
        ).fetchone()["s"]
        cap = paid - already_refunded
        if amount > cap:
            flash(f"That's more than what's left refundable on this case ({logic.fmt_money(cap)} JOD).", "error")
            return redisplay()

    boarding_id = None
    if boarding_id_raw:
        if not boarding_id_raw.isdigit() or not db.execute(
            "SELECT 1 FROM boarding_sessions WHERE id=? FOR UPDATE", (int(boarding_id_raw),)
        ).fetchone():
            flash(f"Boarding stay {boarding_id_raw} not found.", "error")
            return redisplay()
        boarding_id = int(boarding_id_raw)
        paid = logic.boarding_billing_summary(db, boarding_id)["paid"]
        already_refunded = db.execute(
            "SELECT COALESCE(SUM(amount),0) s FROM refunds WHERE refund_type='service' AND boarding_id=?", (boarding_id,)
        ).fetchone()["s"]
        cap = paid - already_refunded
        if amount > cap:
            flash(f"That's more than what's left refundable on this stay ({logic.fmt_money(cap)} JOD).", "error")
            return redisplay()

    now = datetime.now().isoformat(timespec="seconds")
    # Snapshot, not a live lookup — see CLEANUP_FEATURE_PLAN.md §3.7/§4.5.
    # No cap change here: the paid-minus-already-refunded caps above
    # already correctly exclude any Clean Up amount, since Clean Up is
    # never itself a `payments` row.
    cleanup_amount_at_refund = 0
    if visit_id:
        b = db.execute("SELECT cleanup_amount FROM billing WHERE visit_id=?", (visit_id,)).fetchone()
        cleanup_amount_at_refund += (b["cleanup_amount"] if b else 0) or 0
    if case_id:
        c = db.execute("SELECT cleanup_amount FROM inpatient_cases WHERE id=?", (case_id,)).fetchone()
        cleanup_amount_at_refund += (c["cleanup_amount"] if c else 0) or 0
    if boarding_id:
        bs = db.execute("SELECT cleanup_amount FROM boarding_sessions WHERE id=?", (boarding_id,)).fetchone()
        cleanup_amount_at_refund += (bs["cleanup_amount"] if bs else 0) or 0
    cur = db.execute(
        "INSERT INTO refunds (refund_type, refund_date, amount, visit_id, inpatient_case_id, boarding_id, reason, "
        "refund_method, processed_by, created_at, cleanup_amount_at_refund) "
        "VALUES ('service',?,?,?,?,?,?,?,?,?,?) RETURNING id",
        (refund_date, round(amount, 3), visit_id, case_id, boarding_id, reason, refund_method, session["user_id"], now,
         cleanup_amount_at_refund),
    )
    refund_id = cur.fetchone()["id"]
    logic.recompute_month_summary(db, logic.month_key(refund_date))
    auth.log_change(db, "refunds", str(refund_id), "create")
    db.commit()
    flash(f"Service refund of {amount:,.3f} JOD recorded.", "success")
    return redirect(url_for("sales.refunds_page"))
