"""
The inventory catalogue and everything priced from it: the price list, stock status, the ordering sheet, barcode management, and the audit sessions that establish current stock.

Split out of app.py. Shared request-layer pieces come from core.py rather than
app.py -- app.py registers this blueprint, so importing from it here would be
circular.

Endpoint names carry the `inventory.` prefix Flask gives every blueprint route:
`url_for("inventory.api_inventory_lookup")`, not `url_for("inventory.api_inventory_lookup")`.
"""

from datetime import date
from datetime import datetime
import auth
import barcode as barcode_mod
import db as dbmod
import json
import logic
import re

from flask import (
    Blueprint, flash, jsonify, redirect, render_template, request, session, url_for
)

from core import BadNumber, PER_PAGE, get_db, get_page, has_negative, page_count, page_offset, parse_money, parse_quantity, required_field

bp = Blueprint("inventory", __name__)


@bp.route("/api/inventory/lookup")
@auth.permission_required("process_pos_sales")
def api_inventory_lookup():
    db = get_db()
    barcode_val = request.args.get("barcode", "").strip()
    q = request.args.get("q", "").strip()
    if barcode_val:
        row = db.execute("SELECT id, name, barcode FROM inventory_list WHERE barcode=? AND active=true", (barcode_val,)).fetchone()
        if not row:
            return jsonify(None)
        price = logic.item_sale_price(db, row["id"])
        status = logic.inventory_status_by_id(db, row["id"])
        return jsonify({"id": row["id"], "name": row["name"], "price": price,
                        "stock": status["current_stock"] if status else None})
    if q:
        rows = db.execute("SELECT id, name FROM inventory_list WHERE active=true AND category='Retail' AND name ILIKE ? LIMIT 10",
                          (logic.like_pattern(q),)).fetchall()
        # inventory_status_by_id() re-runs the whole catalog-wide status
        # computation and linear-scans for one item — fine called once, not
        # once per matched row here (up to 10x per autocomplete keystroke
        # otherwise). Computed once up front and looked up by item_id
        # instead.
        status_by_item = {s["item_id"]: s for s in logic.inventory_status(db)}
        out = []
        for r in rows:
            price = logic.item_sale_price(db, r["id"])
            status = status_by_item.get(r["id"])
            out.append({"id": r["id"], "name": r["name"], "price": price,
                        "stock": status["current_stock"] if status else None})
        return jsonify(out)
    return jsonify([])


@bp.route("/api/price-list/lookup")
@auth.permission_required("manage_visits", "manage_inpatient")
def api_price_list_lookup():
    db = get_db()
    q = request.args.get("q", "").strip()
    # Repeatable — e.g. ?category=Service&category=Medicine. Both callers
    # (visit billing, inpatient billing) always pass at least one; Retail
    # is never a valid value here — Retail is sold exclusively through POS
    # (its own dedicated search against inventory_list, untouched by this).
    categories = [c for c in request.args.getlist("category") if c]
    if len(q) < 2 or not categories:
        return jsonify([])
    placeholders = ",".join("?" * len(categories))
    sql = (f"SELECT id, name, category, sale_price FROM price_list "
           f"WHERE active=true AND sale_price IS NOT NULL AND category IN ({placeholders}) "
           f"AND (id ILIKE ? OR name ILIKE ?) ORDER BY name LIMIT 15")
    params = [*categories, logic.like_pattern(q), logic.like_pattern(q)]
    rows = db.execute(sql, params).fetchall()
    return jsonify([{"id": r["id"], "name": r["name"], "category": r["category"], "price": r["sale_price"]} for r in rows])


# ---------------------------------------------------------------------------
# Price List (Admin only)
# ---------------------------------------------------------------------------
PRICE_CATEGORIES = ["Service", "Medicine", "Retail"]


def _price_list_context(db):
    cat = request.args.get("category")
    search = request.args.get("q", "").strip()
    page = get_page()
    where = ["active=true"]
    params = []
    if cat:
        where.append("category=?")
        params.append(cat)
    if search:
        where.append("name ILIKE ?")
        params.append(logic.like_pattern(search))
    where_sql = " WHERE " + " AND ".join(where)
    total = db.execute(f"SELECT COUNT(*) c FROM price_list{where_sql}", params).fetchone()["c"]
    q = f"SELECT * FROM price_list{where_sql} ORDER BY category, name LIMIT ? OFFSET ?"
    rows = db.execute(q, params + [PER_PAGE, page_offset(page)]).fetchall()
    inv_items = db.execute("SELECT id, name, cost_price FROM inventory_list WHERE active=true AND category='Retail' ORDER BY name").fetchall()
    flagged_price, _ = logic.retail_consistency_flags(db)
    return dict(items=rows, categories=PRICE_CATEGORIES, active_cat=cat,
                inv_items=inv_items, search=search, flagged_price=flagged_price,
                page=page, total_pages=page_count(total), total_count=total)


@bp.route("/price-list")
@auth.permission_required("manage_price_list")
def price_list():
    db = get_db()
    return render_template("price_list.html", **_price_list_context(db))


@bp.route("/price-list/new", methods=["POST"])
@auth.permission_required("manage_price_list")
def price_list_new():
    db = get_db()
    f = request.form

    def redisplay():
        return render_template("price_list.html", **_price_list_context(db), form=f, show_new_form=True)

    try:
        cost_price = parse_money(f.get("cost_price"))
        sale_price = parse_money(f.get("sale_price"))
    except BadNumber:
        flash("Cost Price and Sale Price must be valid numbers.", "error")
        return redisplay()
    if has_negative(cost_price, sale_price):
        flash("Cost Price and Sale Price can't be negative.", "error")
        return redisplay()
    if f.get("category") not in PRICE_CATEGORIES:
        flash("Category must be one of: " + ", ".join(PRICE_CATEGORIES) + ".", "error")
        return redisplay()
    linked_item_id = f.get("linked_item_id") or None
    if linked_item_id and not db.execute(
            "SELECT 1 FROM inventory_list WHERE id=?", (linked_item_id,)).fetchone():
        flash("That inventory item no longer exists — reload the page and pick again.", "error")
        return redisplay()
    if linked_item_id:
        # Two active rows linking to the same inventory item makes POS
        # pricing nondeterministic — item_sale_price() picks whichever one
        # a plain LIMIT 1 happens to return, with no ordering guarantee,
        # so the same product could ring up at two different prices with
        # no error or warning telling staff the catalog is inconsistent.
        existing_link = db.execute(
            "SELECT id, name FROM price_list WHERE linked_item_id=? AND active=true", (linked_item_id,)
        ).fetchone()
        if existing_link:
            flash(f"That inventory item is already linked to {existing_link['id']} ({existing_link['name']}) — "
                  f"an item can only be linked from one active Price List row at a time.", "error")
            return redisplay()
    name = required_field(f, "name", "Name")
    if name is None:
        return redisplay()
    pid = dbmod.next_id(db, "P")
    can_discount = f.get("can_discount") == "on"
    db.execute(
        "INSERT INTO price_list (id,name,category,cost_price,sale_price,notes,active,linked_item_id,can_discount) VALUES (?,?,?,?,?,?,true,?,?)",
        (pid, name, f["category"], cost_price, sale_price,
         f.get("notes"), linked_item_id, can_discount),
    )
    auth.log_change(db, "price_list", pid, "create")
    db.commit()
    flash(f"{pid} added to price list.", "success")
    return redirect(url_for("inventory.price_list"))


@bp.route("/price-list/<item_id>/edit", methods=["POST"])
@auth.permission_required("manage_price_list")
def price_list_edit(item_id):
    db = get_db()
    f = request.form

    def redisplay():
        return render_template("price_list.html", **_price_list_context(db), form=f, edit_error_id=item_id)

    try:
        cost_price = parse_money(f.get("cost_price"))
        sale_price = parse_money(f.get("sale_price"))
    except BadNumber:
        flash("Cost Price and Sale Price must be valid numbers.", "error")
        return redisplay()
    if has_negative(cost_price, sale_price):
        flash("Cost Price and Sale Price can't be negative.", "error")
        return redisplay()
    if f.get("category") not in PRICE_CATEGORIES:
        flash("Category must be one of: " + ", ".join(PRICE_CATEGORIES) + ".", "error")
        return redisplay()
    old = db.execute("SELECT * FROM price_list WHERE id=?", (item_id,)).fetchone()
    if not old:
        flash("Price list item not found.", "error")
        return redirect(url_for("inventory.price_list"))
    new_linked_item_id = (f.get("linked_item_id") or None) if "linked_item_id" in f else old["linked_item_id"]
    if new_linked_item_id and not db.execute(
            "SELECT 1 FROM inventory_list WHERE id=?", (new_linked_item_id,)).fetchone():
        flash("That inventory item no longer exists — reload the page and pick again.", "error")
        return redisplay()
    if new_linked_item_id and new_linked_item_id != old["linked_item_id"]:
        dup = db.execute(
            "SELECT id, name FROM price_list WHERE linked_item_id=? AND active=true AND id != ?",
            (new_linked_item_id, item_id),
        ).fetchone()
        if dup:
            flash(f"That inventory item is already linked to {dup['id']} ({dup['name']}) — "
                  f"an item can only be linked from one active Price List row at a time.", "error")
            return redisplay()
    name = required_field(f, "name", "Name")
    if name is None:
        return redisplay()
    new_vals = {"name": name, "category": f["category"], "cost_price": cost_price,
                "sale_price": sale_price, "notes": f.get("notes"),
                "linked_item_id": new_linked_item_id,
                "can_discount": f.get("can_discount") == "on"}
    changes = auth.diff_dict(old, new_vals)
    db.execute("UPDATE price_list SET name=?, category=?, cost_price=?, sale_price=?, notes=?, linked_item_id=?, can_discount=? WHERE id=?",
              (*new_vals.values(), item_id))
    if "cost_price" in changes or "sale_price" in changes:
        # Billing/inpatient revenue and COGS are computed against the
        # *current* Price List value, not one frozen at transaction time —
        # so a cost/sale price edit can retroactively change any past
        # month that ever billed this code. Full rebuild is the only way
        # to know which months without re-scanning anyway.
        logic.recompute_full_summary(db)
    auth.log_change(db, "price_list", item_id, "update", changes)
    db.commit()
    flash("Price updated.", "success")
    return redirect(url_for("inventory.price_list"))


@bp.route("/price-list/bulk-edit", methods=["POST"])
@auth.permission_required("manage_price_list")
def price_list_bulk_edit():
    """
    Saves many Price List row edits in a single request instead of one
    request per row. This matters a lot at scale: each row edit that
    touches cost_price/sale_price triggers a full recompute of the
    materialized financial summary (since billing/inpatient revenue and
    COGS are looked up against the *current* Price List value — see
    logic._revenue_and_cogs_by_month) — that full recompute is cheap once,
    but doing it 50 separate times back-to-back for a 50-row bulk edit is
    what actually caused the lag. Batching means it runs at most once
    total, in one DB transaction, with one response instead of 50 full
    page redirects being fetched and thrown away by the browser.
    """
    db = get_db()
    payload = request.get_json(silent=True) or {}
    items = payload.get("items") or []
    saved, errors = [], {}
    any_price_changed = False
    claimed_in_batch = {}
    for item in items:
        item_id = str(item.get("id", ""))
        fields = item.get("fields") or {}
        try:
            cost_price = parse_money(fields.get("cost_price"))
            sale_price = parse_money(fields.get("sale_price"))
        except BadNumber:
            errors[item_id] = "Cost Price and Sale Price must be valid numbers."
            continue
        if has_negative(cost_price, sale_price):
            errors[item_id] = "Cost Price and Sale Price can't be negative."
            continue
        old = db.execute("SELECT * FROM price_list WHERE id=?", (item_id,)).fetchone()
        if not old:
            errors[item_id] = "Item not found."
            continue
        name = (fields.get("name") or "").strip()
        if not name:
            errors[item_id] = "Name is required."
            continue
        category = fields.get("category", "")
        if category not in PRICE_CATEGORIES:
            errors[item_id] = "Category must be one of: " + ", ".join(PRICE_CATEGORIES) + "."
            continue
        new_linked_item_id = (fields.get("linked_item_id") or None) if "linked_item_id" in fields else old["linked_item_id"]
        if new_linked_item_id and not db.execute(
                "SELECT 1 FROM inventory_list WHERE id=?", (new_linked_item_id,)).fetchone():
            errors[item_id] = "That inventory item no longer exists — reload the page and pick again."
            continue
        if new_linked_item_id:
            # Checked against both the database (another row, unrelated to
            # this batch) and what this same batch has already claimed (two
            # rows in one bulk save both trying to link the same item).
            dup = db.execute(
                "SELECT id FROM price_list WHERE linked_item_id=? AND active=true AND id != ?",
                (new_linked_item_id, item_id),
            ).fetchone()
            dup_id = dup["id"] if dup else claimed_in_batch.get(new_linked_item_id)
            if dup_id and dup_id != item_id:
                errors[item_id] = f"That inventory item is already linked to {dup_id} — an item can only be linked from one active row at a time."
                continue
            claimed_in_batch[new_linked_item_id] = item_id
        new_vals = {"name": name, "category": category,
                    "cost_price": cost_price, "sale_price": sale_price, "notes": fields.get("notes"),
                    "linked_item_id": new_linked_item_id,
                    "can_discount": fields.get("can_discount") == "on"}
        changes = auth.diff_dict(old, new_vals)
        db.execute(
            "UPDATE price_list SET name=?, category=?, cost_price=?, sale_price=?, notes=?, linked_item_id=?, can_discount=? WHERE id=?",
            (*new_vals.values(), item_id),
        )
        if "cost_price" in changes or "sale_price" in changes:
            any_price_changed = True
        auth.log_change(db, "price_list", item_id, "update", changes)
        saved.append(item_id)
    if any_price_changed:
        logic.recompute_full_summary(db)
    db.commit()
    return jsonify({"ok": len(errors) == 0, "saved": saved, "errors": errors})


@bp.route("/price-list/<item_id>/delete", methods=["POST"])
@auth.permission_required("manage_price_list")
def price_list_delete(item_id):
    db = get_db()
    db.execute("UPDATE price_list SET active=false WHERE id=?", (item_id,))
    auth.log_change(db, "price_list", item_id, "delete")
    db.commit()
    flash("Item removed from price list.", "success")
    return redirect(url_for("inventory.price_list"))


# ---------------------------------------------------------------------------
# Inventory catalog
# ---------------------------------------------------------------------------
INVENTORY_CATEGORIES = ["Medical", "Retail"]

def _inventory_catalog_context(db):
    show_inactive = request.args.get("inactive") == "1"
    search = request.args.get("q", "").strip()
    page = get_page()
    where = []
    params = []
    if not show_inactive:
        where.append("i.active=true")
    if search:
        where.append("i.name ILIKE ?")
        params.append(logic.like_pattern(search))
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    total = db.execute(f"SELECT COUNT(*) c FROM inventory_list i{where_sql}", params).fetchone()["c"]
    q = ("SELECT i.*, d.name as distributor_name FROM inventory_list i LEFT JOIN distributors d ON d.id=i.distributor_id"
         + where_sql + " ORDER BY i.category, i.name LIMIT ? OFFSET ?")
    rows = db.execute(q, params + [PER_PAGE, page_offset(page)]).fetchall()
    distributors = db.execute("SELECT * FROM distributors ORDER BY name").fetchall()
    _, flagged_inventory = logic.retail_consistency_flags(db)
    has_barcodes = db.execute(
        "SELECT EXISTS(SELECT 1 FROM inventory_list WHERE barcode_source='generated' AND active=true) AS e"
    ).fetchone()["e"]
    return dict(items=rows, distributors=distributors,
                show_inactive=show_inactive, categories=INVENTORY_CATEGORIES, search=search,
                flagged_inventory=flagged_inventory, has_barcodes=has_barcodes,
                page=page, total_pages=page_count(total), total_count=total)


@bp.route("/inventory-catalog")
@auth.permission_required("manage_inventory_catalog")
def inventory_catalog():
    db = get_db()
    return render_template("inventory_catalog.html", **_inventory_catalog_context(db))


@bp.route("/inventory-catalog/new", methods=["POST"])
@auth.permission_required("manage_inventory_catalog")
def inventory_catalog_new():
    db = get_db()
    f = request.form

    def redisplay():
        return render_template("inventory_catalog.html", **_inventory_catalog_context(db), form=f, show_new_form=True)

    try:
        cost_price = parse_money(f.get("cost_price"))
    except BadNumber:
        flash("Cost Price must be a valid number.", "error")
        return redisplay()
    # A negative cost makes an item look infinitely profitable everywhere
    # margin and COGS are calculated. IQ guards this; JO did not.
    if has_negative(cost_price):
        flash("Cost Price can't be negative.", "error")
        return redisplay()
    if f.get("category", "Medical") not in INVENTORY_CATEGORIES:
        flash("Category must be one of: " + ", ".join(INVENTORY_CATEGORIES) + ".", "error")
        return redisplay()
    distributor_id = f.get("distributor_id") or None
    if distributor_id and not db.execute(
            "SELECT 1 FROM distributors WHERE id=?", (distributor_id,)).fetchone():
        flash("That distributor no longer exists — reload the page and pick again.", "error")
        return redisplay()
    name = required_field(f, "name", "Name")
    if name is None:
        return redisplay()
    iid = dbmod.next_id(db, "INV")
    db.execute(
        "INSERT INTO inventory_list (id,name,category,unit,track_expiry,cost_price,distributor_id,active,notes) "
        "VALUES (?,?,?,?,?,?,?,true,?)",
        (iid, name, f.get("category", "Medical"), f.get("unit"), f.get("track_expiry") == "on",
         cost_price, distributor_id, f.get("notes")),
    )
    auth.log_change(db, "inventory_list", iid, "create")
    db.commit()
    flash(f"{iid} added to inventory catalog.", "success")
    return redirect(url_for("inventory.inventory_catalog"))


@bp.route("/inventory-catalog/<item_id>/edit", methods=["POST"])
@auth.permission_required("manage_inventory_catalog")
def inventory_catalog_edit(item_id):
    db = get_db()
    f = request.form

    def redisplay():
        return render_template("inventory_catalog.html", **_inventory_catalog_context(db), form=f, edit_error_id=item_id)

    try:
        cost_price = parse_money(f.get("cost_price"))
    except BadNumber:
        flash("Cost Price must be a valid number.", "error")
        return redisplay()
    if has_negative(cost_price):
        flash("Cost Price can't be negative.", "error")
        return redisplay()
    if f.get("category", "Medical") not in INVENTORY_CATEGORIES:
        flash("Category must be one of: " + ", ".join(INVENTORY_CATEGORIES) + ".", "error")
        return redisplay()
    old = db.execute("SELECT * FROM inventory_list WHERE id=?", (item_id,)).fetchone()
    if not old:
        flash("Inventory item not found.", "error")
        return redirect(url_for("inventory.inventory_catalog"))
    category = f.get("category", "Medical")
    if category != old["category"] and old["ownership_type"] == "Consignment":
        flash("Set this item back to Owned on the Consignment Items page before changing its category.", "error")
        return redisplay()
    distributor_id = f.get("distributor_id") or None
    if distributor_id and not db.execute(
            "SELECT 1 FROM distributors WHERE id=?", (distributor_id,)).fetchone():
        flash("That distributor no longer exists — reload the page and pick again.", "error")
        return redisplay()
    if distributor_id != old["distributor_id"] and logic.consignment_item_locked(db, item_id):
        flash("This item has consignment activity against it — its distributor can't be "
              "changed here. Create a new inventory item for the new supply source.", "error")
        return redisplay()
    name = required_field(f, "name", "Name")
    if name is None:
        return redisplay()
    new_vals = {"name": name, "category": category, "unit": f.get("unit"),
                "track_expiry": f.get("track_expiry") == "on", "cost_price": cost_price,
                "distributor_id": distributor_id,
                "notes": f.get("notes", old["notes"]), "active": old["active"]}
    changes = auth.diff_dict(old, new_vals)
    db.execute(
        "UPDATE inventory_list SET name=?, category=?, unit=?, track_expiry=?, cost_price=?, distributor_id=?, notes=?, active=? WHERE id=?",
        (*new_vals.values(), item_id),
    )
    if "cost_price" in changes:
        # Retail COGS is computed against the *current* inventory cost_price,
        # not a value frozen at sale time — so this can retroactively change
        # COGS for any past month that ever sold or refunded this item.
        logic.recompute_full_summary(db)
    auth.log_change(db, "inventory_list", item_id, "update", changes)
    db.commit()
    flash("Inventory item updated.", "success")
    return redirect(url_for("inventory.inventory_catalog"))


@bp.route("/inventory-catalog/bulk-edit", methods=["POST"])
@auth.permission_required("manage_inventory_catalog")
def inventory_catalog_bulk_edit():
    """Same batching rationale as price_list_bulk_edit — see that route's
    docstring. One request, one transaction, at most one financial-summary
    recompute for the whole batch instead of one per row."""
    db = get_db()
    payload = request.get_json(silent=True) or {}
    items = payload.get("items") or []
    saved, errors = [], {}
    any_cost_changed = False
    for item in items:
        item_id = str(item.get("id", ""))
        fields = item.get("fields") or {}
        try:
            cost_price = parse_money(fields.get("cost_price"))
        except BadNumber:
            errors[item_id] = "Cost Price must be a valid number."
            continue
        # Same guard as inventory_catalog_new — a bulk edit is the easier
        # way to set a negative cost, not the harder one.
        if has_negative(cost_price):
            errors[item_id] = "Cost Price can't be negative."
            continue
        old = db.execute("SELECT * FROM inventory_list WHERE id=?", (item_id,)).fetchone()
        if not old:
            errors[item_id] = "Item not found."
            continue
        name = (fields.get("name") or "").strip()
        if not name:
            errors[item_id] = "Name is required."
            continue
        category = fields.get("category", "Medical")
        if category not in INVENTORY_CATEGORIES:
            errors[item_id] = "Category must be one of: " + ", ".join(INVENTORY_CATEGORIES) + "."
            continue
        if category != old["category"] and old["ownership_type"] == "Consignment":
            errors[item_id] = "Set this item back to Owned on the Consignment Items page before changing its category."
            continue
        distributor_id = fields.get("distributor_id") or None
        if distributor_id and not db.execute(
                "SELECT 1 FROM distributors WHERE id=?", (distributor_id,)).fetchone():
            errors[item_id] = "That distributor no longer exists — reload the page and pick again."
            continue
        if distributor_id != old["distributor_id"] and logic.consignment_item_locked(db, item_id):
            errors[item_id] = ("This item has consignment activity against it — its distributor can't be "
                                "changed here. Create a new inventory item for the new supply source.")
            continue
        new_vals = {"name": name, "category": category,
                    "unit": fields.get("unit"), "track_expiry": fields.get("track_expiry") == "on",
                    "cost_price": cost_price, "distributor_id": distributor_id,
                    "notes": fields.get("notes", old["notes"]), "active": old["active"]}
        changes = auth.diff_dict(old, new_vals)
        db.execute(
            "UPDATE inventory_list SET name=?, category=?, unit=?, track_expiry=?, cost_price=?, distributor_id=?, notes=?, active=? WHERE id=?",
            (*new_vals.values(), item_id),
        )
        if "cost_price" in changes:
            any_cost_changed = True
        auth.log_change(db, "inventory_list", item_id, "update", changes)
        saved.append(item_id)
    if any_cost_changed:
        logic.recompute_full_summary(db)
    db.commit()
    return jsonify({"ok": len(errors) == 0, "saved": saved, "errors": errors})


@bp.route("/inventory-catalog/<item_id>/toggle-active", methods=["POST"])
@auth.permission_required("manage_inventory_catalog")
def inventory_catalog_toggle(item_id):
    db = get_db()
    row = db.execute("SELECT active FROM inventory_list WHERE id=?", (item_id,)).fetchone()
    if row is None:
        flash("Item not found.", "error")
        return redirect(url_for("inventory.inventory_catalog"))
    new_val = not row["active"]
    db.execute("UPDATE inventory_list SET active=? WHERE id=?", (new_val, item_id))
    auth.log_change(db, "inventory_list", item_id, "update", {"active": (row["active"], new_val)})
    db.commit()
    flash("Item " + ("reactivated." if new_val else "deactivated."), "success")
    return redirect(url_for("inventory.inventory_catalog"))


@bp.route("/inventory-catalog/<item_id>/create-barcode", methods=["POST"])
@auth.permission_required("manage_inventory_catalog")
def inventory_catalog_barcode_generate(item_id):
    db = get_db()
    item = db.execute("SELECT barcode FROM inventory_list WHERE id=?", (item_id,)).fetchone()
    if not item:
        return jsonify({"error": "Item not found."}), 404
    if item["barcode"]:
        return jsonify({"error": "A barcode already exists for this item."}), 400
    try:
        code = barcode_mod.generate_barcode(db)
    except RuntimeError as e:
        # generate_barcode() gives up after 50 collision retries and raises
        # rather than looping forever — without this, that message never
        # reaches the user. See ERROR_500_AUDIT.md E-15.
        return jsonify({"error": str(e)}), 500
    # The check above is a friendly fast-path, not the real guarantee — two
    # concurrent "Generate" clicks racing into the same candidate code (low
    # but non-zero probability) would otherwise surface as a raw 500 instead
    # of a friendly error; inventory_list.barcode is DB-UNIQUE, so the loser
    # raises instead of corrupting anything.
    try:
        db.execute("UPDATE inventory_list SET barcode=?, barcode_source='generated' WHERE id=?", (code, item_id))
    except dbmod.IntegrityError:
        db.rollback()
        return jsonify({"error": "That code was just claimed by another item — try again."}), 400
    auth.log_change(db, "inventory_list", item_id, "update", {"barcode": (None, code)})
    db.commit()
    return jsonify({"ok": True, "barcode": code, "source": "generated",
                     "label_url": url_for("inventory.inventory_barcode_label", item_id=item_id)})


@bp.route("/inventory-catalog/<item_id>/barcode/manual", methods=["POST"])
@auth.permission_required("manage_inventory_catalog")
def inventory_catalog_barcode_manual(item_id):
    db = get_db()
    item = db.execute("SELECT barcode, barcode_source FROM inventory_list WHERE id=?", (item_id,)).fetchone()
    if not item:
        return jsonify({"error": "Item not found."}), 404
    data = request.get_json(silent=True) or {}
    raw = (data.get("barcode") or "").strip()
    if not raw:
        return jsonify({"error": "Enter a barcode."}), 400
    if len(raw) > 64:
        return jsonify({"error": "That's too long to be a real barcode — check what you entered."}), 400
    if not re.match(r"^[A-Za-z0-9 .\-_]+$", raw):
        return jsonify({"error": "Only letters, numbers, spaces, and . - _ are allowed."}), 400
    if item["barcode_source"] == "generated":
        return jsonify({"error": "A barcode already exists for this item."}), 400
    if raw != item["barcode"]:
        dupe = db.execute(
            "SELECT name FROM inventory_list WHERE barcode=? AND id!=?", (raw, item_id)
        ).fetchone()
        if dupe:
            return jsonify({"error": f'That barcode is already used by "{dupe["name"]}".'}), 400
    try:
        db.execute("UPDATE inventory_list SET barcode=?, barcode_source='manual' WHERE id=?", (raw, item_id))
    except dbmod.IntegrityError:
        db.rollback()
        return jsonify({"error": "That barcode was just claimed by another item — try again."}), 400
    auth.log_change(db, "inventory_list", item_id, "update", {"barcode": (item["barcode"], raw)})
    db.commit()
    return jsonify({"ok": True, "barcode": raw, "source": "manual"})


@bp.route("/inventory-catalog/<item_id>/barcode/remove", methods=["POST"])
@auth.permission_required("manage_inventory_catalog")
def inventory_catalog_barcode_remove(item_id):
    db = get_db()
    item = db.execute("SELECT barcode FROM inventory_list WHERE id=?", (item_id,)).fetchone()
    if not item:
        return jsonify({"error": "Item not found."}), 404
    if not item["barcode"]:
        return jsonify({"ok": True, "removed": False})
    db.execute("UPDATE inventory_list SET barcode=NULL, barcode_source=NULL WHERE id=?", (item_id,))
    auth.log_change(db, "inventory_list", item_id, "update", {"barcode": (item["barcode"], None)})
    db.commit()
    return jsonify({"ok": True, "removed": True})


@bp.route("/inventory-catalog/<item_id>/barcode/status")
@auth.permission_required("manage_inventory_catalog")
def inventory_catalog_barcode_status(item_id):
    db = get_db()
    item = db.execute("SELECT barcode, barcode_source FROM inventory_list WHERE id=?", (item_id,)).fetchone()
    if not item:
        return jsonify({"error": "Item not found."}), 404
    return jsonify({
        "barcode": item["barcode"],
        "source": item["barcode_source"],
        "label_url": url_for("inventory.inventory_barcode_label", item_id=item_id) if item["barcode"] else None,
    })


@bp.route("/inventory-catalog/<item_id>/barcode-label")
@auth.permission_required("manage_inventory_catalog")
def inventory_barcode_label(item_id):
    db = get_db()
    item = db.execute("SELECT * FROM inventory_list WHERE id=?", (item_id,)).fetchone()
    if not item or not item["barcode"]:
        flash("This item doesn't have a barcode yet.", "error")
        return redirect(url_for("inventory.inventory_catalog"))
    return render_template("barcode_label.html", item=item)


@bp.route("/inventory-catalog/barcodes/generated")
@auth.permission_required("manage_inventory_catalog")
def inventory_catalog_barcodes_generated():
    """Every active item with a VetClinicSystem-generated barcode, across the
    whole catalog regardless of which page of Inventory Catalog is showing —
    feeds the Bulk Barcode Print picker. Deliberately excludes manually
    entered barcodes (a real code copied from the manufacturer's own
    packaging) — bulk printing exists to produce labels for barcodes this
    app itself made up, since a manually entered one is already printed on
    the item's own packaging and needs no new label."""
    db = get_db()
    rows = db.execute(
        "SELECT id, name, barcode FROM inventory_list "
        "WHERE barcode_source='generated' AND active=true ORDER BY name"
    ).fetchall()
    return jsonify([{"id": r["id"], "name": r["name"], "barcode": r["barcode"]} for r in rows])


@bp.route("/inventory-catalog/barcodes/bulk-print", methods=["POST"])
@auth.permission_required("manage_inventory_catalog")
def inventory_catalog_barcodes_bulk_print():
    db = get_db()
    try:
        requested = json.loads(request.form.get("items") or "[]")
    except (ValueError, TypeError):
        requested = []
    labels = []
    for entry in requested if isinstance(requested, list) else []:
        if not isinstance(entry, dict):
            continue
        item_id = str(entry.get("id", ""))
        try:
            qty = int(entry.get("qty", 1))
        except (TypeError, ValueError):
            qty = 1
        qty = max(1, min(qty, 500))
        # Re-checked server-side, same as everywhere else a client-supplied
        # id gets acted on — only an item that currently has a generated
        # barcode can end up on the printed sheet, no matter what the
        # client sent (matches the picker's own filter above).
        item = db.execute(
            "SELECT id, name, barcode FROM inventory_list WHERE id=? AND barcode_source='generated'",
            (item_id,),
        ).fetchone()
        if item and item["barcode"]:
            labels.append({"id": item["id"], "name": item["name"], "barcode": item["barcode"], "qty": qty})
    if not labels:
        flash("No barcodes selected to print.", "error")
        return redirect(url_for("inventory.inventory_catalog"))
    return render_template("barcode_bulk_print.html", labels=labels)


# ---------------------------------------------------------------------------
# Inventory Status / Ordering Sheet
# ---------------------------------------------------------------------------
@bp.route("/inventory-status")
@auth.permission_required("view_inventory_status")
def inventory_status_page():
    db = get_db()
    rows = logic.inventory_status(db)
    filter_ = request.args.get("filter")
    if filter_ == "low_stock":
        rows = [r for r in rows if r["stock_status"] == "LOW STOCK"]
    elif filter_ == "overdue":
        rows = [r for r in rows if r["audit_status"] in ("OVERDUE", "Never audited")]
    elif filter_ == "expiring":
        rows = [r for r in rows if r["expiry_status"] in ("EXPIRING SOON", "EXPIRED")]
    search = request.args.get("q", "").strip()
    if search:
        needle = search.lower()
        rows = [r for r in rows if needle in (r["name"] or "").lower()]
    return render_template("inventory_status.html", rows=rows, filter_=filter_, search=search)


@bp.route("/ordering-sheet")
@auth.permission_required("manage_ordering_sheet")
def ordering_sheet_page():
    db = get_db()
    rows = logic.ordering_sheet(db)
    return render_template("ordering_sheet.html", rows=rows)


# ---------------------------------------------------------------------------
# Audit sessions (whole-catalog Save / Confirm)
# ---------------------------------------------------------------------------
@bp.route("/audit-history")
@auth.permission_required("manage_audit_history")
def audit_history_list():
    db = get_db()
    page = get_page()
    sessions, total = logic.list_audit_sessions(db, limit=PER_PAGE, offset=page_offset(page))
    return render_template("audit_sessions_list.html", sessions=sessions,
                            page=page, total_pages=page_count(total), total_count=total)


@bp.route("/audit-history/start", methods=["POST"])
@auth.permission_required("manage_audit_history")
def audit_session_start():
    db = get_db()
    session_id = logic.get_or_create_draft_session(db, date.today().isoformat(), session["user_id"])
    return redirect(url_for("inventory.audit_session_view", session_id=session_id))


def _audit_session_context(db, session_id):
    sess = db.execute("SELECT s.*, u.full_name as performed_by_name FROM audit_sessions s "
                      "LEFT JOIN users u ON u.id=s.performed_by WHERE s.id=?", (session_id,)).fetchone()
    if not sess:
        return None
    # A line saved for an item that's since been deactivated must stay
    # visible/editable here — otherwise it's invisible on this view (can't
    # be seen or fixed) while still there when the session confirms,
    # locking in a value nobody could check. See ORPHANED_RECORDS_AUDIT.md
    # F-11.
    items = db.execute(
        "SELECT i.* FROM inventory_list i WHERE i.active = true "
        "OR EXISTS (SELECT 1 FROM audit_session_lines l WHERE l.session_id=? AND l.item_id=i.id) "
        "ORDER BY i.category, i.name",
        (session_id,),
    ).fetchall()
    existing_lines = {r["item_id"]: dict(r) for r in db.execute(
        "SELECT * FROM audit_session_lines WHERE session_id=?", (session_id,)).fetchall()}
    # Effective (carried-forward) values from the last CONFIRMED audit, for placeholder display
    confirmed_rows = logic.confirmed_audit_rows_by_item(db)
    latest_confirmed = {}
    for r in confirmed_rows:
        latest_confirmed[r["item_id"]] = r
    readonly = sess["status"] == "Confirmed"
    return dict(sess=sess, items=items, existing_lines=existing_lines,
                latest_confirmed=latest_confirmed, readonly=readonly)


@bp.route("/audit-history/session/<int:session_id>")
@auth.permission_required("manage_audit_history")
def audit_session_view(session_id):
    db = get_db()
    ctx = _audit_session_context(db, session_id)
    if ctx is None:
        flash("Audit session not found.", "error")
        return redirect(url_for("inventory.audit_history_list"))
    return render_template("audit_session_view.html", **ctx)


def _save_audit_lines(db, session_id):
    """Persists whatever count values are in the submitted form into
    audit_session_lines. Shared by Save and Confirm so that clicking Confirm
    directly (without Save first) can never silently discard the numbers
    someone just typed in."""
    # Same reasoning as _audit_session_context() — a line already saved for
    # a now-deactivated item must still be re-savable, or a technician's
    # just-typed count for it silently drops on Save. See
    # ORPHANED_RECORDS_AUDIT.md F-11.
    items = db.execute(
        "SELECT id FROM inventory_list i WHERE i.active = true "
        "OR EXISTS (SELECT 1 FROM audit_session_lines l WHERE l.session_id=? AND l.item_id=i.id)",
        (session_id,),
    ).fetchall()
    for it in items:
        iid = it["id"]
        stock = request.form.get(f"stock_{iid}", "").strip()
        if stock == "":
            continue
        received = request.form.get(f"received_{iid}", "").strip() or "0"
        threshold = request.form.get(f"threshold_{iid}", "").strip()
        critical = request.form.get(f"critical_{iid}", "").strip()
        target = request.form.get(f"target_{iid}", "").strip()
        expiry = request.form.get(f"expiry_{iid}", "").strip()
        notes = request.form.get(f"notes_{iid}", "").strip()

        # parse_money(), not float(). float() accepts "nan"/"inf" without
        # raising, and a NaN count was confirmable and then poisoned every
        # downstream comparison: in this app `qty > current_stock` is a
        # Decimal against a float NaN, which raises decimal.InvalidOperation
        # and turns every POS checkout of that item into a 500 -- the till
        # stops working until the count is corrected. (The same NaN in IQ
        # fails the opposite way, silently passing the oversell check;
        # one root cause, two symptoms, per CLAUDE.md §1.)
        # This was also the one numeric entry point still putting a raw
        # Python float into a column, against this app's Decimal rule.
        # has_negative() covers the other half: -5 is not a physical count.
        # parse_quantity(), not parse_money(): these are counts, and it is the
        # same parser _merged_cart_quantities() uses for the POS cart, so both
        # sides of the `qty > current_stock` comparison share one ceiling.
        # (IQ has no parse_quantity and uses parse_money on both sides for the
        # same reason -- same intent, each app's own helper. CLAUDE.md §1.)
        try:
            stock_v = parse_quantity(stock, required=True)
            received_v = parse_quantity(received)
            threshold_v = parse_quantity(threshold) if threshold else None
            target_v = parse_quantity(target) if target else None
        except BadNumber:
            raise BadNumber(iid)
        if has_negative(stock_v, received_v, threshold_v, target_v):
            raise BadNumber(iid)
        vals = (
            stock_v, received_v if received_v is not None else 0,
            threshold_v,
            (1 if critical == "Y" else (0 if critical == "N" else None)),
            target_v,
            expiry or None, notes or None,
        )
        # UPSERT rather than a SELECT-then-branch INSERT/UPDATE — closes
        # the race where two concurrent saves for the same item could
        # both read no existing row and both attempt an INSERT, the
        # second raising an unhandled UniqueViolation against the
        # (session_id, item_id) UNIQUE constraint.
        db.execute(
            "INSERT INTO audit_session_lines (session_id,item_id,stock_counted,received_since_prior,"
            "reorder_threshold,critical_item,target_coverage_days,nearest_expiry_date,notes) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (session_id, item_id) DO UPDATE SET stock_counted=excluded.stock_counted, "
            "received_since_prior=excluded.received_since_prior, reorder_threshold=excluded.reorder_threshold, "
            "critical_item=excluded.critical_item, target_coverage_days=excluded.target_coverage_days, "
            "nearest_expiry_date=excluded.nearest_expiry_date, notes=excluded.notes",
            (session_id, iid, *vals),
        )


@bp.route("/audit-history/session/<int:session_id>/save", methods=["POST"])
@auth.permission_required("manage_audit_history")
def audit_session_save(session_id):
    db = get_db()
    sess = db.execute("SELECT * FROM audit_sessions WHERE id=?", (session_id,)).fetchone()
    if not sess or sess["status"] != "Draft":
        flash("This audit is confirmed and can no longer be edited.", "error")
        return redirect(url_for("inventory.audit_history_list"))

    try:
        _save_audit_lines(db, session_id)
    except BadNumber:
        # Whichever earlier items in the loop already had a db.execute()
        # called for them (before the item that failed) were never
        # committed — roll them back rather than leaving them sitting in
        # an open transaction, so a save that overall failed can't
        # partially apply.
        db.rollback()
        flash("Audit counts must be valid numbers. The draft was not saved — please correct the highlighted value(s).", "error")
        ctx = _audit_session_context(db, session_id)
        if ctx is None:
            return redirect(url_for("inventory.audit_history_list"))
        return render_template("audit_session_view.html", **ctx, form=request.form)
    auth.log_change(db, "audit_sessions", str(session_id), "update")
    db.commit()
    flash("Audit saved. You can come back and finish it later, or confirm it once it's complete.", "success")
    return redirect(url_for("inventory.audit_session_view", session_id=session_id))


@bp.route("/audit-history/session/<int:session_id>/confirm", methods=["POST"])
@auth.permission_required("manage_audit_history")
def audit_session_confirm(session_id):
    db = get_db()
    sess = db.execute("SELECT * FROM audit_sessions WHERE id=?", (session_id,)).fetchone()
    if not sess or sess["status"] != "Draft":
        flash("This audit is already confirmed.", "error")
        return redirect(url_for("inventory.audit_history_list"))
    try:
        _save_audit_lines(db, session_id)
    except BadNumber:
        db.rollback()
        flash("Audit counts must be valid numbers. Nothing was confirmed — please correct the highlighted value(s).", "error")
        ctx = _audit_session_context(db, session_id)
        if ctx is None:
            return redirect(url_for("inventory.audit_history_list"))
        return render_template("audit_session_view.html", **ctx, form=request.form)

    # An all-blank confirm produces an immutable Confirmed session with
    # zero lines — permanent noise in Audit History with nothing to show
    # for it. See ORPHANED_RECORDS_AUDIT.md F-15.
    filled = db.execute(
        "SELECT COUNT(*) c FROM audit_session_lines WHERE session_id=? AND stock_counted IS NOT NULL",
        (session_id,)).fetchone()["c"]
    if not filled:
        flash("Nothing has been counted in this audit yet — fill in at least one item "
              "before confirming.", "error")
        return redirect(url_for("inventory.audit_session_view", session_id=session_id))
    # Microsecond precision (not seconds) — inventory_status()'s stock
    # calculation compares inventory_transactions.timestamp against this
    # column with a strict '>' on whole-second-precision TEXT strings; a
    # sale landing in the exact same second as this confirm would tie and
    # get silently excluded from the running total, letting a same-second
    # sale slip past the stock check unnoticed (see the matching change to
    # the `now` timestamps written alongside every inventory_transactions
    # row: pos_checkout(), refund restocking, and the consignment
    # receipt/shrinkage/return helpers in logic.py).
    db.execute("UPDATE audit_sessions SET status='Confirmed', confirmed_at=? WHERE id=?",
              (datetime.now().isoformat(timespec="microseconds"), session_id))
    auth.log_change(db, "audit_sessions", str(session_id), "update", {"status": ("Draft", "Confirmed")})
    db.commit()
    flash("Audit confirmed and locked. Inventory Status and Ordering Sheet now reflect these counts.", "success")
    return redirect(url_for("inventory.audit_session_view", session_id=session_id))


@bp.route("/audit-history/session/<int:session_id>/delete", methods=["POST"])
@auth.permission_required("manage_audit_history")
def audit_session_delete(session_id):
    """Discards an abandoned draft. Clicking Start on the same day commits
    an empty session immediately, and the reuse query is scoped to
    audit_date=today, so an abandoned draft from any previous day is never
    picked up again — with no delete route, these just accumulated
    forever. Confirmed sessions are immutable history, not deletable here.
    See ORPHANED_RECORDS_AUDIT.md F-15."""
    db = get_db()
    sess = db.execute("SELECT status FROM audit_sessions WHERE id=?", (session_id,)).fetchone()
    if not sess or sess["status"] != "Draft":
        flash("Only a draft audit can be discarded.", "error")
        return redirect(url_for("inventory.audit_history_list"))
    db.execute("DELETE FROM audit_session_lines WHERE session_id=?", (session_id,))
    db.execute("DELETE FROM audit_sessions WHERE id=?", (session_id,))
    auth.log_change(db, "audit_sessions", str(session_id), "delete")
    db.commit()
    flash("Draft audit discarded.", "success")
    return redirect(url_for("inventory.audit_history_list"))
