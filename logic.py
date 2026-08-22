"""
Jordan Referral Center — computation engine (v3).
Pure computation over SQLite tables; no Flask imports.
"""
import calendar
from datetime import date, datetime, timedelta
from collections import defaultdict

import auth as authmod

MISSED_WINDOW_DAYS = 14   # 2 weeks — used for follow-ups, wellness, and Lost to Follow Up
WELLNESS_LEAD_DAYS = 5    # remind 5 days before the next-dose date


def parse_date(v):
    if v is None or v == "":
        return None
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def fmt_date(d):
    return d.isoformat() if d else None


def month_key(d):
    """Returns the 'YYYY-MM' prefix of a date value, accepting either an
    ISO date string or a datetime.date/datetime object -- DATE columns
    (via psycopg) come back as the latter, values built in Python are
    usually the former. Returns None for a falsy input."""
    if not d:
        return None
    if hasattr(d, "isoformat"):
        return d.isoformat()[:7]
    return str(d)[:7]


def get_setting(db, key, default=None):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def backup_alert_message(last_backup_row):
    """Returns a warning string for the Dashboard, or None if backups look healthy."""
    if not last_backup_row:
        return "No database backup has ever run yet — set a backup folder on the Settings page."
    if last_backup_row["status"] == "failed":
        return f"The last database backup failed: {last_backup_row['error'] or 'unknown error'}."
    started = parse_date(last_backup_row["started_at"])
    if started and (date.today() - started).days >= 2:
        return "The database hasn't been backed up in 2+ days — check the Settings page."
    return None


def fmt_money(amount):
    """JOD has no practical decimal subdivision in everyday use — whole numbers, comma-separated."""
    if amount is None:
        return "\u2014"
    return f"{round(amount):,}"


# ---------------------------------------------------------------------------
# Audit sessions (Save / Confirm) — only Confirmed sessions count as history
# ---------------------------------------------------------------------------
def audit_session_status_label(status):
    return {"Draft": "Saved", "Confirmed": "Confirmed"}.get(status, status)


def get_or_create_draft_session(db, audit_date, user_id):
    row = db.execute(
        "SELECT * FROM audit_sessions WHERE audit_date=? AND status='Draft' ORDER BY id DESC LIMIT 1",
        (audit_date,),
    ).fetchone()
    if row:
        return row["id"]
    cur = db.execute(
        "INSERT INTO audit_sessions (audit_date, performed_by, status, created_at) VALUES (?,?,'Draft',?) RETURNING id",
        (audit_date, user_id, datetime.now().isoformat(timespec="seconds")),
    )
    new_id = cur.fetchone()["id"]
    db.commit()
    return new_id


def list_audit_sessions(db):
    rows = db.execute(
        "SELECT s.*, u.full_name as performed_by_name, "
        "(SELECT COUNT(*) FROM audit_session_lines l WHERE l.session_id=s.id AND l.stock_counted IS NOT NULL) as lines_filled "
        "FROM audit_sessions s LEFT JOIN users u ON u.id=s.performed_by ORDER BY s.audit_date DESC, s.id DESC"
    ).fetchall()
    return rows


def confirmed_audit_rows_by_item(db, item_id=None):
    """
    Flattened, chronological, per-item rows from CONFIRMED sessions only — the
    equivalent of the old audit_history table — annotated with prior-audit usage,
    daily rate, and carried-forward threshold/critical/target values.
    """
    q = """
    SELECT l.*, s.audit_date as audit_date
    FROM audit_session_lines l JOIN audit_sessions s ON s.id = l.session_id
    WHERE s.status='Confirmed' AND l.stock_counted IS NOT NULL
    """
    params = []
    if item_id:
        q += " AND l.item_id=?"
        params.append(item_id)
    q += " ORDER BY l.item_id, s.audit_date, l.id"
    rows = [dict(r) for r in db.execute(q, params).fetchall()]

    by_item = defaultdict(list)
    for r in rows:
        by_item[r["item_id"]].append(r)

    out = []
    for iid, item_rows in by_item.items():
        eff_threshold = eff_critical = eff_target = None
        prior = None
        for r in item_rows:
            if r["reorder_threshold"] is not None:
                eff_threshold = r["reorder_threshold"]
            if r["critical_item"] is not None:
                eff_critical = r["critical_item"]
            if r["target_coverage_days"] is not None:
                eff_target = r["target_coverage_days"]
            r["effective_reorder_threshold"] = eff_threshold
            r["effective_critical_item"] = eff_critical
            r["effective_target_coverage_days"] = eff_target if eff_target is not None else 30

            if prior is not None:
                r["prior_audit_date"] = prior["audit_date"]
                r["prior_stock"] = prior["stock_counted"]
                usage = prior["stock_counted"] + r["received_since_prior"] - r["stock_counted"]
                r["usage_since_prior"] = usage
                days = (parse_date(r["audit_date"]) - parse_date(prior["audit_date"])).days
                r["days_since_prior"] = days
                r["daily_usage_rate"] = round(usage / days, 4) if days > 0 else None
            else:
                r["prior_audit_date"] = r["prior_stock"] = r["usage_since_prior"] = None
                r["days_since_prior"] = r["daily_usage_rate"] = None

            out.append(r)
            prior = r

    out.sort(key=lambda r: (r["audit_date"], r["id"]))
    return out


def _txn_qty_since(db, item_id, since_date):
    row = db.execute(
        "SELECT COALESCE(SUM(change_qty),0) s FROM inventory_transactions WHERE item_id=? AND timestamp > ?",
        (item_id, since_date),
    ).fetchone()
    return row["s"] or 0


# ---------------------------------------------------------------------------
# Inventory Status
# ---------------------------------------------------------------------------
def inventory_status(db):
    audit_overdue_days = int(get_setting(db, "audit_overdue_days", 35))
    expiry_soon_days = int(get_setting(db, "expiry_soon_days", 60))
    today = date.today()

    items = [dict(r) for r in db.execute("SELECT * FROM inventory_list WHERE active=1 ORDER BY name").fetchall()]
    all_confirmed = confirmed_audit_rows_by_item(db)
    by_item = defaultdict(list)
    for r in all_confirmed:
        by_item[r["item_id"]].append(r)

    status = []
    for it in items:
        rows = by_item.get(it["id"], [])
        latest = rows[-1] if rows else None

        base_stock = latest["stock_counted"] if latest else None
        latest_audit_date = parse_date(latest["audit_date"]) if latest else None
        nearest_expiry = parse_date(latest["nearest_expiry_date"]) if latest else None
        daily_usage_rate = latest["daily_usage_rate"] if latest else None
        reorder_threshold = latest["effective_reorder_threshold"] if latest else None
        critical_item = bool(latest["effective_critical_item"]) if latest else False
        target_coverage_days = latest["effective_target_coverage_days"] if latest else 30

        current_stock = base_stock
        if base_stock is not None:
            current_stock = round(base_stock + _txn_qty_since(db, it["id"], str(latest["audit_date"])), 3)

        days_since_audit = (today - latest_audit_date).days if latest_audit_date else None
        days_to_expiry = (nearest_expiry - today).days if nearest_expiry else None

        if latest is None:
            stock_status = "No audits yet"
        elif reorder_threshold is not None and current_stock is not None and current_stock <= reorder_threshold:
            stock_status = "LOW STOCK"
        else:
            stock_status = "OK"

        if not it["track_expiry"] or nearest_expiry is None:
            expiry_status = None
        elif days_to_expiry < 0:
            expiry_status = "EXPIRED"
        elif days_to_expiry <= expiry_soon_days:
            expiry_status = "EXPIRING SOON"
        else:
            expiry_status = "OK"

        if latest is None:
            audit_status = "Never audited"
        elif days_since_audit > audit_overdue_days:
            audit_status = "OVERDUE"
        else:
            audit_status = "OK"

        status.append({
            "item_id": it["id"], "name": it["name"], "unit": it["unit"], "category": it["category"],
            "barcode": it["barcode"], "reorder_threshold": reorder_threshold,
            "track_expiry": bool(it["track_expiry"]), "latest_audit_date": fmt_date(latest_audit_date),
            "current_stock": current_stock, "nearest_expiry_date": fmt_date(nearest_expiry),
            "daily_usage_rate": daily_usage_rate, "days_since_audit": days_since_audit,
            "days_to_expiry": days_to_expiry, "stock_status": stock_status, "expiry_status": expiry_status,
            "audit_status": audit_status, "critical_item": critical_item,
            "target_coverage_days": target_coverage_days, "distributor_id": it["distributor_id"],
            "ownership_type": it["ownership_type"],
        })
    return status


def inventory_status_by_id(db, item_id):
    for r in inventory_status(db):
        if r["item_id"] == item_id:
            return r
    return None


# ---------------------------------------------------------------------------
# Ordering Sheet
# ---------------------------------------------------------------------------
def ordering_sheet(db):
    inv_status = inventory_status(db)
    all_confirmed = confirmed_audit_rows_by_item(db)
    by_item = defaultdict(list)
    for r in all_confirmed:
        by_item[r["item_id"]].append(r)

    dists = {d["id"]: dict(d) for d in db.execute("SELECT * FROM distributors").fetchall()}

    rows = []
    for s in inv_status:
        stock, rate = s["current_stock"], s["daily_usage_rate"]
        days_left = (stock / rate) if (stock is not None and rate and rate > 0) else None
        urgency = -1 if s["critical_item"] else (days_left if days_left is not None else 9999)

        if s["critical_item"]:
            priority = "CRITICAL"
        elif days_left is None:
            priority = "No data"
        elif days_left <= 7:
            priority = "URGENT"
        elif days_left <= 21:
            priority = "SOON"
        else:
            priority = "OK"

        target = s["target_coverage_days"] or 30
        suggested_qty = None
        if rate and rate > 0 and stock is not None:
            suggested_qty = max(0, int(-(-((target * rate) - stock) // 1)))

        item_rows = by_item.get(s["item_id"], [])
        trend, trend_note = "Not enough data", "Not enough audit history yet (need 2+ confirmed audits)"
        if len(item_rows) >= 2:
            prior_rate = item_rows[-2]["daily_usage_rate"]
            if rate is not None and prior_rate is not None:
                if rate > prior_rate * 1.15:
                    trend, trend_note = "Increasing", "Usage rising - consider more coverage days"
                elif rate < prior_rate * 0.85:
                    trend, trend_note = "Decreasing", "Usage falling - consider fewer coverage days"
                else:
                    trend, trend_note = "Steady", "Usage steady - keep current target"

        dist = dists.get(s["distributor_id"]) if s["distributor_id"] else None
        rows.append({
            **s, "days_of_stock_left": round(days_left, 1) if days_left is not None else None,
            "urgency_score": urgency, "priority": priority, "suggested_order_qty": suggested_qty,
            "usage_trend": trend, "trend_recommendation": trend_note,
            "distributor_name": dist["name"] if dist else None,
            "lead_time_days": dist["lead_time_days"] if dist else None,
            "catalog_link": dist["catalog_link"] if dist else None,
        })

    rows.sort(key=lambda r: (r["urgency_score"] if r["urgency_score"] is not None else 9999))
    for i, r in enumerate(rows, start=1):
        r["priority_rank"] = i
    return rows


# ---------------------------------------------------------------------------
# Billing (Automatic line-items OR Manual lump sum) + discount + payment status
# ---------------------------------------------------------------------------
def retail_consistency_flags(db):
    """
    Cross-checks the Retail category between Inventory Catalog and Price
    List. Returns (flagged_price_ids, flagged_inventory_ids):
      - a Price List Retail row is flagged if its linked item doesn't exist
        or isn't an active Retail inventory item
      - an active Retail inventory item is flagged if no active Price List
        Retail row links to it
    """
    price_rows = db.execute(
        "SELECT id, linked_item_id FROM price_list WHERE active=1 AND category='Retail'"
    ).fetchall()
    inv_ids = {r["id"] for r in db.execute(
        "SELECT id FROM inventory_list WHERE active=1 AND category='Retail'"
    ).fetchall()}

    flagged_price = {r["id"] for r in price_rows if not r["linked_item_id"] or r["linked_item_id"] not in inv_ids}
    linked_ids = {r["linked_item_id"] for r in price_rows if r["linked_item_id"]}
    flagged_inventory = inv_ids - linked_ids

    return flagged_price, flagged_inventory


def non_discountable_line_names(db, price_ids):
    """Given a set/list of price_list IDs on a bill, returns the display
    names of any that are marked as not discountable (can_discount=0)."""
    if not price_ids:
        return []
    ids = [i for i in price_ids if i]
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = db.execute(
        f"SELECT name FROM price_list WHERE id IN ({placeholders}) AND can_discount=0",
        tuple(ids),
    ).fetchall()
    return [r["name"] for r in rows]


def non_discountable_line_names_for_items(db, inventory_item_ids):
    """Same as non_discountable_line_names(), but for POS carts, which
    reference inventory_list IDs rather than price_list IDs directly —
    looks up each item's linked Retail price_list entry."""
    ids = [i for i in inventory_item_ids if i]
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = db.execute(
        f"SELECT name FROM price_list WHERE linked_item_id IN ({placeholders}) AND can_discount=0",
        tuple(ids),
    ).fetchall()
    return [r["name"] for r in rows]


def save_visit_billing_lines(db, visit_id, lines):
    """
    Replaces every visit_billing_lines row for this visit with a fresh
    snapshot of what's in the cart at Save time \u2014 price_id/name/category/
    quantity/unit_price/unit_cost per line, from the search-and-add
    billing UI (visit_billing_save() in app.py builds this list). Does
    not commit (caller's job, same convention as every other write in
    this module).
    """
    db.execute("DELETE FROM visit_billing_lines WHERE visit_id=?", (visit_id,))
    now_str = datetime.now().isoformat(timespec="seconds")
    for l in lines:
        db.execute(
            "INSERT INTO visit_billing_lines (visit_id, price_id, name, category, quantity, unit_price, unit_cost, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (visit_id, l["price_id"], l["name"], l["category"], l["quantity"],
             l["unit_price"], l["unit_cost"], now_str),
        )


def compute_bill_totals(subtotal, discount_percent, paid):
    """
    Shared by visit_billing_summary() and inpatient_billing_summary() so the
    money math (total/balance/status) is defined in exactly one place instead
    of being duplicated per billing type.
    """
    discount_percent = discount_percent or 0
    total = round(subtotal * (1 - discount_percent / 100), 2)
    paid = round(paid or 0, 2)
    balance = round(total - paid, 2)
    if total <= 0:
        status = "N/A"
    elif paid <= 0:
        status = "Unpaid"
    elif balance <= 0.5:
        status = "Fully Paid"
    else:
        status = "Partially Paid"
    return total, paid, balance, status


def visit_billing_summary(db, visit_id):
    b = db.execute("SELECT * FROM billing WHERE visit_id=?", (visit_id,)).fetchone()
    if not b:
        return {"billing_type": "Automatic", "lines": [], "subtotal": 0, "discount_percent": 0,
                "total": 0, "paid": 0, "balance": 0, "status": "N/A"}

    if b["billing_type"] == "Manual":
        lines = [{"id": None, "name": "Veterinary Services", "category": "Service",
                  "price": b["manual_amount"] or 0}] if b["manual_amount"] else []
        subtotal = b["manual_amount"] or 0
    else:
        snapshot_rows = db.execute(
            "SELECT price_id, name, category, quantity, unit_price FROM visit_billing_lines WHERE visit_id=? ORDER BY id",
            (visit_id,),
        ).fetchall()
        # Priced from the snapshot taken when this bill was saved (via the
        # search-and-add billing UI) — doesn't change if someone edits the
        # Price List afterward. Mirrors inpatient_billing_summary()'s shape
        # exactly (quantity + line_total per line).
        lines = [{"id": r["price_id"], "name": r["name"], "category": r["category"],
                  "price": r["unit_price"], "quantity": r["quantity"],
                  "line_total": round(r["unit_price"] * r["quantity"], 2)}
                 for r in snapshot_rows]
        subtotal = sum(l["line_total"] for l in lines)

    discount_percent = b["discount_percent"] or 0
    paid_row = db.execute("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE visit_id=?", (visit_id,)).fetchone()
    total, paid, balance, status = compute_bill_totals(subtotal, discount_percent, paid_row["s"])
    return {"billing_type": b["billing_type"], "lines": lines, "subtotal": round(subtotal, 2),
            "discount_percent": discount_percent, "total": total, "paid": paid, "balance": balance,
            "status": status}


def refresh_visit_billing_total(db, visit_id):
    """Recomputes and persists billing.total after any change to this
    bill's lines, discount, or manual amount. Call this in the same
    transaction right after such a change, before commit — so reports
    can read the stored total instead of re-deriving it independently."""
    total = visit_billing_summary(db, visit_id)["total"]
    db.execute("UPDATE billing SET total=? WHERE visit_id=?", (total, visit_id))


def visit_total_bill(db, visit_id):
    return visit_billing_summary(db, visit_id)["total"]


# ---------------------------------------------------------------------------
# Inpatient billing (procedures only — never touches stock)
# ---------------------------------------------------------------------------
def inpatient_billing_summary(db, case_id):
    rows = db.execute(
        "SELECT ib.*, p.name, p.sale_price, p.category FROM inpatient_billing ib "
        "JOIN price_list p ON p.id = ib.price_id WHERE ib.case_id=? ORDER BY ib.timestamp",
        (case_id,),
    ).fetchall()
    lines, subtotal = [], 0
    for r in rows:
        # Prefer the snapshot taken when this line was added (unit_price)
        # — falls back to the live Price List join (p.sale_price) only if
        # that specific line's snapshot is NULL (e.g. added before this
        # column existed, or the price_list item had no sale_price set at
        # the moment it was billed).
        unit_price = r["unit_price"] if r["unit_price"] is not None else (r["sale_price"] or 0)
        line_total = unit_price * r["quantity"]
        subtotal += line_total
        lines.append({"id": r["id"], "name": r["name"], "quantity": r["quantity"],
                       "unit_price": unit_price, "line_total": round(line_total, 2)})
    case = db.execute("SELECT discount_percent FROM inpatient_cases WHERE id=?", (case_id,)).fetchone()
    discount_percent = case["discount_percent"] if case else 0
    paid_row = db.execute("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE inpatient_case_id=?", (case_id,)).fetchone()
    total, paid, balance, status = compute_bill_totals(subtotal, discount_percent, paid_row["s"])
    return {"lines": lines, "subtotal": round(subtotal, 2), "discount_percent": discount_percent,
            "total": total, "paid": paid, "balance": balance, "status": status}


def refresh_inpatient_total(db, case_id):
    """Recomputes and persists inpatient_cases.total after any change to
    this case's procedures or discount. Call this in the same transaction
    right after such a change, before commit — see
    refresh_visit_billing_total()."""
    total = inpatient_billing_summary(db, case_id)["total"]
    db.execute("UPDATE inpatient_cases SET total=? WHERE id=?", (total, case_id))


# ---------------------------------------------------------------------------
# Boarding
# ---------------------------------------------------------------------------
def boarding_nights(entry_date, dismissal_date):
    """Nights stayed so far (or planned), at least 1."""
    start = parse_date(entry_date)
    end = parse_date(dismissal_date) if dismissal_date else date.today()
    if not start:
        return 1
    return max(1, (end - start).days)


def boarding_suggested_total(price_per_day, entry_date, dismissal_date):
    if not price_per_day:
        return None
    return round(price_per_day * boarding_nights(entry_date, dismissal_date), 2)


def boarding_billing_summary(db, boarding_id):
    b = db.execute("SELECT total FROM boarding_sessions WHERE id=?", (boarding_id,)).fetchone()
    subtotal = (b["total"] or 0) if b else 0
    paid_row = db.execute("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE boarding_id=?", (boarding_id,)).fetchone()
    total, paid, balance, status = compute_bill_totals(subtotal, 0, paid_row["s"])
    return {"total": total, "paid": paid, "balance": balance, "status": status}


def boarding_sessions_for_patient(db, patient_id):
    return db.execute(
        "SELECT * FROM boarding_sessions WHERE patient_id=? ORDER BY entry_date DESC", (patient_id,)
    ).fetchall()


# ---------------------------------------------------------------------------
# Follow-ups (method, not type)
# ---------------------------------------------------------------------------
def followups(db, only_pending=False):
    q = """
    SELECT v.id as visit_id, v.followup_method, v.followup_reason, v.followup_date,
           v.followup_status, v.doctor, v.created_by, v.date as visit_date,
           p.animal_name, o.id as owner_id, o.name as owner_name, o.phone
    FROM visits v JOIN patients p ON p.id = v.patient_id JOIN owners o ON o.id = p.owner_id
    WHERE v.followup_needed = 'Y'
    """
    rows = [dict(r) for r in db.execute(q).fetchall()]
    today = date.today()
    out = []
    for r in rows:
        if only_pending and r["followup_status"] != "Pending":
            continue
        reminder_call_date = None
        if r["followup_method"] == "Physical Visit" and r["followup_date"]:
            reminder_call_date = fmt_date(parse_date(r["followup_date"]) - timedelta(days=1))
        r["reminder_call_date"] = reminder_call_date
        r["missed"] = False
        if r["followup_status"] == "Pending" and r["followup_date"]:
            fdate = parse_date(r["followup_date"])
            if fdate and (today - fdate).days >= MISSED_WINDOW_DAYS:
                r["missed"] = True
        out.append(r)
    out.sort(key=lambda r: (r["followup_date"] or date.max))
    return out


# ---------------------------------------------------------------------------
# Wellness reminders
# ---------------------------------------------------------------------------
def wellness_reminders(db, only_due=False):
    q = """
    SELECT v.id as visit_id, v.wellness_type, v.wellness_next_dose_date, v.wellness_contacted,
           v.wellness_contact_method, v.doctor, v.created_by,
           p.animal_name, o.id as owner_id, o.name as owner_name, o.phone
    FROM visits v JOIN patients p ON p.id = v.patient_id JOIN owners o ON o.id = p.owner_id
    WHERE v.wellness_needed = 'Y' AND v.wellness_next_dose_date IS NOT NULL
    """
    rows = [dict(r) for r in db.execute(q).fetchall()]
    today = date.today()
    out = []
    for r in rows:
        next_dose = parse_date(r["wellness_next_dose_date"])
        remind_from = next_dose - timedelta(days=WELLNESS_LEAD_DAYS) if next_dose else None
        due = bool(remind_from and today >= remind_from and r["wellness_contacted"] != "Y")
        missed = bool(next_dose and (today - next_dose).days >= MISSED_WINDOW_DAYS and r["wellness_contacted"] != "Y")
        if only_due and not due:
            continue
        r["remind_from_date"] = fmt_date(remind_from)
        r["due"] = due
        r["missed"] = missed
        out.append(r)
    out.sort(key=lambda r: (r["wellness_next_dose_date"] or date.max))
    return out


# ---------------------------------------------------------------------------
# Grooming queue (lives on the visit record itself)
# ---------------------------------------------------------------------------
GROOMING_SERVICES = ["Bath", "Haircut", "De-shedding", "Nail Trim", "Ear Cleaning", "Ear Mites Cleaning",
                      "Paw Clipping", "Nail Caps", "Anal Gland Emptying", "Zoning"]


def grooming_queue(db, include_finished=False):
    q = """
    SELECT v.id as visit_id, v.date, v.grooming_services, v.grooming_notes, v.grooming_admitted_items,
           v.grooming_status, v.grooming_contacted, p.id as patient_id, p.animal_name,
           o.name as owner_name, o.phone
    FROM visits v JOIN patients p ON p.id = v.patient_id JOIN owners o ON o.id = p.owner_id
    WHERE v.grooming_needed='Y'
    """
    if not include_finished:
        q += " AND (v.grooming_status IS NULL OR v.grooming_status != 'Finished')"
    q += " ORDER BY v.date DESC"
    return [dict(r) for r in db.execute(q).fetchall()]


# ---------------------------------------------------------------------------
# Missed items — for the admin dashboard
# ---------------------------------------------------------------------------
def missed_items(db):
    out = []
    for f in followups(db, only_pending=True):
        if f["missed"]:
            out.append({"kind": "Follow-up", "visit_id": f["visit_id"], "animal_name": f["animal_name"],
                        "deadline": f["followup_date"], "responsible": f["doctor"] or f["created_by"]})
    for w in wellness_reminders(db):
        if w["missed"]:
            out.append({"kind": "Wellness", "visit_id": w["visit_id"], "animal_name": w["animal_name"],
                        "deadline": w["wellness_next_dose_date"], "responsible": w["doctor"] or w["created_by"]})

    today = date.today()
    rows = db.execute(
        "SELECT v.id, v.case_status_changed_at, v.doctor, v.created_by, p.animal_name FROM visits v "
        "JOIN patients p ON p.id=v.patient_id WHERE v.case_status='Lost to Follow Up'"
    ).fetchall()
    for r in rows:
        changed = parse_date(r["case_status_changed_at"]) if r["case_status_changed_at"] else None
        if changed and (today - changed).days >= MISSED_WINDOW_DAYS:
            out.append({"kind": "Lost to Follow Up", "visit_id": r["id"], "animal_name": r["animal_name"],
                        "deadline": fmt_date(changed), "responsible": r["doctor"] or r["created_by"]})
    return out


# ---------------------------------------------------------------------------
# Dashboard snapshot
# ---------------------------------------------------------------------------
def dashboard_snapshot(db):
    today = date.today()
    tomorrow = today + timedelta(days=1)

    total_patients = db.execute("SELECT COUNT(*) c FROM patients").fetchone()["c"]
    active_statuses = {"Ongoing", "Admitted to Inpatient", "Needs Filling"}
    all_visits = db.execute("SELECT case_status FROM visits").fetchall()
    active_cases = sum(1 for v in all_visits if v["case_status"] in active_statuses)
    admitted_now = db.execute("SELECT COUNT(*) c FROM inpatient_cases WHERE dismissed=0").fetchone()["c"]

    fu = followups(db, only_pending=True)
    due_today = [f for f in fu if parse_date(f["followup_date"]) == today]
    reminders_tomorrow = [f for f in fu if parse_date(f["followup_date"]) == tomorrow and f["followup_method"] == "Physical Visit"]

    wr = wellness_reminders(db, only_due=True)
    grooming = grooming_queue(db)

    inv = inventory_status(db)
    low_stock = [i for i in inv if i["stock_status"] == "LOW STOCK"]
    overdue_audit = [i for i in inv if i["audit_status"] in ("OVERDUE", "Never audited")]
    expiring = [i for i in inv if i["expiry_status"] in ("EXPIRING SOON", "EXPIRED")]

    return {
        "total_patients": total_patients, "active_cases": active_cases, "admitted_now": admitted_now,
        "due_today": due_today, "reminders_tomorrow": reminders_tomorrow, "wellness_due": wr,
        "grooming_queue": grooming, "low_stock": low_stock, "overdue_audit": overdue_audit, "expiring": expiring,
    }


def opex_reminder_due(db):
    """True in the last 3 days of the current month if that month's opex hasn't been entered."""
    today = date.today()
    last_day = calendar.monthrange(today.year, today.month)[1]
    if last_day - today.day > 2:
        return False
    month = today.strftime("%Y-%m")
    row = db.execute("SELECT 1 FROM monthly_opex WHERE month=?", (month,)).fetchone()
    return row is None


# ---------------------------------------------------------------------------
# Monthly / Yearly P&L (admin-only; enforced at the route level)
# ---------------------------------------------------------------------------
def _revenue_and_cogs_by_month(db, month=None):
    """
    Computes revenue and COGS from every transactional source (billing,
    retail sales, inpatient billing, boarding, refunds).

    With month=None (the original behavior, still used for full rebuilds),
    it scans every row ever recorded and returns one dict entry per month
    found. With month='YYYY-MM', every underlying query is scoped down to
    just that month at the SQL level, so the exact same per-row math runs
    but only over that month's rows — the returned dicts then have at most
    one key. This is what makes fast, targeted per-month recomputation
    possible: same formulas, just filtered, so results are guaranteed
    consistent with a full scan.
    """
    revenue_by_month = defaultdict(float)
    cost_by_item = {r["id"]: r["cost_price"] or 0 for r in db.execute("SELECT id, cost_price FROM inventory_list").fetchall()}
    cogs_by_month = defaultdict(float)
    month_like = (month + "%") if month else None

    # Automatic visit billing: cost basis comes from the snapshot taken at
    # Save time (visit_billing_lines) — this is what stops editing today's
    # prices from retroactively changing a past month's COGS. Revenue reads
    # the stored billing.total (kept in sync by
    # logic.refresh_visit_billing_total()) instead of re-deriving it, so
    # reports always agree with what the bill actually shows.
    billing_where = " WHERE date_billed::text LIKE ?" if month else ""
    billing_params = [month_like] if month else []
    for r in db.execute(
        "SELECT visit_id, billing_type, date_billed, total FROM billing" + billing_where,
        billing_params,
    ).fetchall():
        if not r["date_billed"]:
            continue
        mth = month_key(r["date_billed"])
        if r["billing_type"] != "Manual":
            for l in db.execute(
                "SELECT quantity, unit_cost FROM visit_billing_lines WHERE visit_id=?", (r["visit_id"],)
            ).fetchall():
                cogs_by_month[mth] += (l["unit_cost"] or 0) * l["quantity"]
        revenue_by_month[mth] += r["total"] or 0

    sales_where = " WHERE sale_date LIKE ?" if month else ""
    sales_params = [month_like] if month else []
    for r in db.execute("SELECT sale_date, total FROM sales" + sales_where, sales_params).fetchall():
        mth = r["sale_date"][:7]
        revenue_by_month[mth] += r["total"] or 0

    # Inpatient billing (procedures checked off during a stay). Each line has
    # its own timestamp, so revenue is attributed to the month each
    # procedure was actually logged, with the case's overall discount
    # applied proportionally to every line. Prefers the unit_price/unit_cost
    # snapshotted when the line was added; a NULL snapshot (added before
    # these columns existed, or the price_list item had no sale_price/
    # cost_price set at billing time) falls back to the live Price List join.
    case_discounts = {r["id"]: r["discount_percent"] or 0 for r in db.execute(
        "SELECT id, discount_percent FROM inpatient_cases").fetchall()}
    ib_where = " WHERE ib.timestamp LIKE ?" if month else ""
    ib_params = [month_like] if month else []
    for r in db.execute(
        "SELECT ib.case_id, ib.price_id, ib.quantity, ib.timestamp, ib.unit_price, ib.unit_cost, "
        "p.sale_price, p.cost_price FROM inpatient_billing ib "
        "JOIN price_list p ON p.id = ib.price_id" + ib_where, ib_params
    ).fetchall():
        mth = r["timestamp"][:7]
        unit_price = r["unit_price"] if r["unit_price"] is not None else (r["sale_price"] or 0)
        unit_cost = r["unit_cost"] if r["unit_cost"] is not None else (r["cost_price"] or 0)
        discount = case_discounts.get(r["case_id"], 0)
        revenue_by_month[mth] += (unit_price * r["quantity"]) * (1 - discount / 100)
        cogs_by_month[mth] += unit_cost * r["quantity"]

    # Boarding revenue is attributed to the month the stay started (entry_date).
    # No COGS — boarding is a service, same treatment as a Service price_list item.
    boarding_where = " AND entry_date::text LIKE ?" if month else ""
    boarding_params = [month_like] if month else []
    for r in db.execute(
        "SELECT entry_date, total FROM boarding_sessions WHERE total IS NOT NULL" + boarding_where, boarding_params
    ).fetchall():
        revenue_by_month[month_key(r["entry_date"])] += r["total"]

    # Retail COGS: cost basis comes from the snapshot taken at sale time
    # (sale_items.unit_cost) — falls back to the live Inventory Catalog
    # join only for a sale that predates this column.
    si_where = " WHERE s.sale_date LIKE ?" if month else ""
    si_params = [month_like] if month else []
    for r in db.execute(
        "SELECT si.item_id, si.quantity, si.unit_cost, s.sale_date FROM sale_items si "
        "JOIN sales s ON s.id=si.sale_id" + si_where, si_params
    ).fetchall():
        unit_cost = r["unit_cost"] if r["unit_cost"] is not None else cost_by_item.get(r["item_id"], 0)
        cogs_by_month[r["sale_date"][:7]] += r["quantity"] * unit_cost

    # Refunds reduce revenue in the month the refund itself was processed
    # (not the original sale/visit's month) — standard accounting practice,
    # and it means closed prior months never silently change. A restocked
    # retail refund also reverses the COGS that was booked on the original
    # sale, since the item's cost basis is back in inventory, not spent.
    refunds_where = " WHERE refund_date::text LIKE ?" if month else ""
    refunds_params = [month_like] if month else []
    for r in db.execute(
        "SELECT id, refund_type, refund_date, amount, restocked FROM refunds" + refunds_where, refunds_params
    ).fetchall():
        mth = month_key(r["refund_date"])
        revenue_by_month[mth] -= r["amount"]
        if r["refund_type"] == "retail" and r["restocked"]:
            for it in db.execute("SELECT item_id, quantity FROM refund_items WHERE refund_id=?", (r["id"],)).fetchall():
                cogs_by_month[mth] -= it["quantity"] * cost_by_item.get(it["item_id"], 0)

    return revenue_by_month, cogs_by_month


def recompute_month_summary(db, month):
    """
    Recomputes and upserts the monthly_financial_summary row for a single
    'YYYY-MM' month from current source data. Called right after any write
    that affects that month's revenue/COGS (new sale, new billing, a refund,
    an edited boarding total, etc.) so the summary table never drifts from
    the transactional tables. Does not commit — call sites include this in
    the same transaction/commit as the write that triggered it, so the
    summary and the underlying data change atomically together.
    """
    if not month:
        return
    revenue_by_month, cogs_by_month = _revenue_and_cogs_by_month(db, month=month)
    revenue = round(revenue_by_month.get(month, 0), 2)
    cogs = round(cogs_by_month.get(month, 0), 2)
    now_str = datetime.now().isoformat(timespec="seconds")
    db.execute(
        "INSERT INTO monthly_financial_summary (month, revenue, cogs, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT (month) DO UPDATE SET revenue=EXCLUDED.revenue, cogs=EXCLUDED.cogs, updated_at=EXCLUDED.updated_at",
        (month, revenue, cogs, now_str),
    )


def recompute_months_summary(db, months):
    """Convenience wrapper: recompute several months (deduplicated) in one go."""
    for month in sorted(set(m for m in months if m)):
        recompute_month_summary(db, month)


def recompute_full_summary(db):
    """
    Full rebuild of monthly_financial_summary from scratch, across every
    month that has ever had financial activity. This is the same full scan
    the reports used to do on every single page load — but with the summary
    table in place, it now only needs to run in the rare cases where a
    shared cost/price value changes (which can retroactively affect COGS or
    revenue for many past months at once, since billing/inpatient revenue
    and COGS are both looked up against *current* Price List / Inventory
    Catalog values, not a value frozen at transaction time — see
    _revenue_and_cogs_by_month), plus as a manual admin "Rebuild" action and
    a one-time backfill for historical data. Does not commit.
    """
    revenue_by_month, cogs_by_month = _revenue_and_cogs_by_month(db)
    months = set(revenue_by_month) | set(cogs_by_month)
    now_str = datetime.now().isoformat(timespec="seconds")
    db.execute("DELETE FROM monthly_financial_summary")
    for month in months:
        revenue = round(revenue_by_month.get(month, 0), 2)
        cogs = round(cogs_by_month.get(month, 0), 2)
        db.execute(
            "INSERT INTO monthly_financial_summary (month, revenue, cogs, updated_at) VALUES (?,?,?,?)",
            (month, revenue, cogs, now_str),
        )


def months_touched_by_inpatient_case(db, case_id):
    """Distinct 'YYYY-MM' months a given inpatient case has logged billing
    lines in — used to recompute every month a case's discount change could
    have affected, since a long stay can span more than one month."""
    rows = db.execute(
        "SELECT DISTINCT substr(timestamp, 1, 7) as m FROM inpatient_billing WHERE case_id=?", (case_id,)
    ).fetchall()
    return [r["m"] for r in rows if r["m"]]


def _ensure_summary_populated(db):
    """
    Self-healing: if monthly_financial_summary has never been populated
    (fresh deploy of this feature against an existing database, or a manual
    data import that bypassed the normal app routes), do one full rebuild so
    the reports never silently show zeros. Only runs when the table is
    genuinely empty, so it costs nothing on every normal page load.
    """
    has_any = db.execute("SELECT EXISTS(SELECT 1 FROM monthly_financial_summary) as e").fetchone()["e"]
    if has_any:
        return
    any_data = db.execute(
        "SELECT (EXISTS(SELECT 1 FROM billing) OR EXISTS(SELECT 1 FROM sales) OR "
        "EXISTS(SELECT 1 FROM inpatient_billing) OR EXISTS(SELECT 1 FROM boarding_sessions WHERE total IS NOT NULL) OR "
        "EXISTS(SELECT 1 FROM refunds)) as has_data"
    ).fetchone()["has_data"]
    if any_data:
        recompute_full_summary(db)
        db.commit()


def monthly_pl(db, months_back=12):
    today = date.today()
    months = []
    y, m = today.year, today.month
    for i in range(months_back - 1, -1, -1):
        mm = m - i
        yy = y
        while mm <= 0:
            mm += 12
            yy -= 1
        months.append(f"{yy:04d}-{mm:02d}")

    _ensure_summary_populated(db)
    summary_rows = {r["month"]: r for r in db.execute(
        "SELECT month, revenue, cogs FROM monthly_financial_summary").fetchall()}
    opex_rows = {r["month"]: dict(r) for r in db.execute("SELECT * FROM monthly_opex").fetchall()}

    out = []
    prior_net = None
    for month in months:
        row = summary_rows.get(month)
        revenue = round(row["revenue"], 2) if row else 0
        cogs = round(row["cogs"], 2) if row else 0
        gross_profit = round(revenue - cogs, 2)
        opex = opex_rows.get(month, {"rent": 0, "salaries": 0, "utilities": 0, "marketing": 0, "other": 0})
        total_opex = round(sum(opex.get(k, 0) or 0 for k in ("rent", "salaries", "utilities", "marketing", "other")), 2)
        net_profit = round(gross_profit - total_opex, 2)
        net_margin = round(net_profit / revenue, 4) if revenue else None

        mom_change = None
        if prior_net not in (None, 0):
            mom_change = round((net_profit - prior_net) / abs(prior_net) * 100, 1)
        prior_net = net_profit

        out.append({
            "month": month, "revenue": revenue, "cogs": cogs, "gross_profit": gross_profit,
            "rent": opex.get("rent", 0), "salaries": opex.get("salaries", 0), "utilities": opex.get("utilities", 0),
            "marketing": opex.get("marketing", 0), "other": opex.get("other", 0), "total_opex": total_opex,
            "net_profit": net_profit, "net_margin": net_margin, "mom_change": mom_change,
        })
    return out


def yearly_pl(db):
    """
    Every year that has ever had revenue/COGS or opex activity, oldest
    first. Reads straight from the materialized monthly summary + opex
    tables (each one row per month regardless of transaction volume), so
    this is cheap however many years of history exist — no fixed window.
    """
    _ensure_summary_populated(db)
    by_year = defaultdict(lambda: {"revenue": 0, "cogs": 0, "total_opex": 0})
    for r in db.execute("SELECT month, revenue, cogs FROM monthly_financial_summary").fetchall():
        y = r["month"][:4]
        by_year[y]["revenue"] += r["revenue"] or 0
        by_year[y]["cogs"] += r["cogs"] or 0
    for r in db.execute("SELECT * FROM monthly_opex").fetchall():
        y = r["month"][:4]
        by_year[y]["total_opex"] += sum((r[k] or 0) for k in ("rent", "salaries", "utilities", "marketing", "other"))

    out = []
    prior_net = None
    for y in sorted(by_year.keys()):
        d = by_year[y]
        gross_profit = round(d["revenue"] - d["cogs"], 2)
        net_profit = round(gross_profit - d["total_opex"], 2)
        net_margin = round(net_profit / d["revenue"], 4) if d["revenue"] else None
        yoy_change = None
        if prior_net not in (None, 0):
            yoy_change = round((net_profit - prior_net) / abs(prior_net) * 100, 1)
        prior_net = net_profit
        out.append({"year": y, "revenue": round(d["revenue"], 2), "cogs": round(d["cogs"], 2),
                    "gross_profit": gross_profit, "total_opex": round(d["total_opex"], 2),
                    "net_profit": net_profit, "net_margin": net_margin, "yoy_change": yoy_change})
    return out


# ---------------------------------------------------------------------------
# Refunds (Admin only)
# ---------------------------------------------------------------------------
def recent_refunds(db, limit=100, offset=0, date_filter=None):
    where = ""
    params = []
    if date_filter:
        where = "WHERE r.refund_date = ? "
        params.append(date_filter)
    rows = db.execute(
        "SELECT r.*, u.full_name AS processed_by_name FROM refunds r "
        "LEFT JOIN users u ON u.id = r.processed_by " + where +
        "ORDER BY r.refund_date DESC, r.id DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d["refund_type"] == "retail":
            d["refund_lines"] = db.execute(
                "SELECT ri.*, il.name FROM refund_items ri JOIN inventory_list il ON il.id = ri.item_id "
                "WHERE ri.refund_id=?",
                (r["id"],),
            ).fetchall()
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Owners / Patients
# ---------------------------------------------------------------------------
def search_patients(db, term):
    term = f"%{term}%"
    return db.execute(
        "SELECT p.*, o.name as owner_name, o.phone as owner_phone FROM patients p "
        "JOIN owners o ON o.id = p.owner_id "
        "WHERE p.animal_name ILIKE ? OR p.id ILIKE ? OR o.name ILIKE ? OR o.phone ILIKE ? "
        "ORDER BY p.animal_name LIMIT 25",
        (term, term, term, term),
    ).fetchall()


def patient_history(db, patient_id):
    visits = db.execute("SELECT * FROM visits WHERE patient_id=? ORDER BY date DESC", (patient_id,)).fetchall()
    cases = db.execute("SELECT * FROM inpatient_cases WHERE patient_id=? ORDER BY admission_date DESC", (patient_id,)).fetchall()
    events = []
    for v in visits:
        events.append({"kind": "Visit", "date": v["date"], "record": dict(v), "summary": v["complaint"] or v["visit_type"]})
    for c in cases:
        events.append({"kind": "Inpatient stay", "date": c["admission_date"], "record": dict(c),
                        "summary": c["complaint"] or "Inpatient stay"})
    events.sort(key=lambda e: e["date"] or date.min, reverse=True)
    return events


# ---------------------------------------------------------------------------
# Audit / login log pages
# ---------------------------------------------------------------------------
def changes_on_date(db, day_str):
    return db.execute("SELECT * FROM audit_log WHERE substr(timestamp,1,10)=? ORDER BY timestamp DESC", (day_str,)).fetchall()


def logins_on_date(db, day_str):
    rows = db.execute("SELECT * FROM login_log WHERE substr(timestamp,1,10)=? ORDER BY timestamp DESC", (day_str,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["device"] = authmod.describe_device(r["user_agent"])
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Point of sale (Retail only)
# ---------------------------------------------------------------------------
def sellable_items(db):
    return db.execute("SELECT id, name, barcode, cost_price, unit FROM inventory_list WHERE active=1 AND category='Retail' ORDER BY name").fetchall()


def item_sale_price(db, item_id):
    row = db.execute("SELECT sale_price FROM price_list WHERE linked_item_id=? AND active=1 LIMIT 1", (item_id,)).fetchone()
    return row["sale_price"] if row else None


# ---------------------------------------------------------------------------
# Distributor Ledger — manual bookkeeping for what you've been billed by a
# distributor and what you've paid them: a lump-sum charge per bill,
# payments recorded against it, balance always computed (never stored).
# Not linked to inventory, POS, or any report; every bill is a manual
# entry, same as the paper/memory tracking this replaces.
# ---------------------------------------------------------------------------
def distributor_bill_balance(bill, payments):
    """payments: list of payment rows for this bill."""
    paid = sum(p["amount"] for p in payments)
    balance = bill["total_amount"] - paid
    if balance <= 0:
        status = "Paid"
    elif paid > 0:
        status = "Partial"
    else:
        status = "Unpaid"
    return {"paid": paid, "balance": balance, "status": status}


def distributor_ledger(db, distributor_id):
    """Bills for a distributor, each annotated with paid/balance/status,
    plus distributor-level totals. Used by the distributor detail page."""
    bills = [dict(r) for r in db.execute(
        "SELECT * FROM distributor_bills WHERE distributor_id=? ORDER BY bill_date DESC, id DESC",
        (distributor_id,)
    ).fetchall()]
    all_payments = db.execute(
        "SELECT * FROM distributor_bill_payments WHERE bill_id IN "
        "(SELECT id FROM distributor_bills WHERE distributor_id=?) ORDER BY payment_date, id",
        (distributor_id,)
    ).fetchall()
    by_bill = {}
    for p in all_payments:
        by_bill.setdefault(p["bill_id"], []).append(dict(p))

    total_billed = total_paid = 0
    for b in bills:
        pmts = by_bill.get(b["id"], [])
        calc = distributor_bill_balance(b, pmts)
        b.update(calc)
        b["payments"] = pmts
        total_billed += b["total_amount"]
        total_paid += calc["paid"]

    return {
        "bills": bills,
        "total_billed": total_billed,
        "total_paid": total_paid,
        "total_outstanding": total_billed - total_paid,
    }


def distributor_outstanding_totals(db):
    """distributor_id -> outstanding balance, for the Distributors list column."""
    rows = db.execute(
        "SELECT b.distributor_id, b.id AS bill_id, b.total_amount, "
        "COALESCE(SUM(p.amount),0) AS paid "
        "FROM distributor_bills b "
        "LEFT JOIN distributor_bill_payments p ON p.bill_id = b.id "
        "GROUP BY b.distributor_id, b.id, b.total_amount"
    ).fetchall()
    totals = {}
    for r in rows:
        totals[r["distributor_id"]] = totals.get(r["distributor_id"], 0) + (r["total_amount"] - r["paid"])
    return totals


def distributor_payables_summary(db):
    """Rolled up across every distributor, for the Distributors list
    page's payables block — total owed, how many distributors have a
    balance, how many bills haven't seen a single payment yet, and a
    small ranked "who you owe most" table."""
    totals = distributor_outstanding_totals(db)  # {distributor_id: balance}
    names = {r["id"]: r["name"] for r in db.execute("SELECT id, name FROM distributors").fetchall()}

    with_balance = {did: bal for did, bal in totals.items() if bal > 0}
    ranked = sorted(
        ({"id": did, "name": names.get(did, did), "balance": bal} for did, bal in with_balance.items()),
        key=lambda r: r["balance"], reverse=True,
    )[:5]

    unpaid_bill_count = db.execute(
        "SELECT COUNT(*) AS n FROM distributor_bills b WHERE NOT EXISTS "
        "(SELECT 1 FROM distributor_bill_payments p WHERE p.bill_id = b.id)"
    ).fetchone()["n"]

    return {
        "total_outstanding": sum(with_balance.values()),
        "distributors_with_balance": len(with_balance),
        "unpaid_bill_count": unpaid_bill_count,
        "top_outstanding": ranked,
    }


# ---------------------------------------------------------------------------
# Consignment — a distributor's stock sitting on your shelf; they're owed
# cost_price per unit once it sells, you keep the markup. Consignment
# items are ordinary Retail inventory_list rows (ownership_type=
# 'Consignment'), so they already flow through pos_checkout / audit
# sessions / P&L exactly like owned stock with zero special-casing there.
# This section is the distributor-facing receiving/shrinkage/returns/
# settlement layer on top of that shared data.
# ---------------------------------------------------------------------------
def record_consignment_receipt(db, item_id, distributor_id, quantity, unit_cost_at_receipt,
                                received_date, delivery_reference, notes, received_by):
    """Logs stock a distributor drops off. Paired with an
    inventory_transactions row (+quantity), same pattern pos_checkout and
    refund restocking already use — this is what makes the new stock
    immediately visible on Inventory Status and the next audit walk with
    zero changes to inventory_status(). Does not commit."""
    now = datetime.now().isoformat(timespec="seconds")
    cur = db.execute(
        "INSERT INTO consignment_receipts (item_id, distributor_id, quantity, unit_cost_at_receipt, "
        "received_date, delivery_reference, notes, received_by, created_at) VALUES (?,?,?,?,?,?,?,?,?) RETURNING id",
        (item_id, distributor_id, quantity, unit_cost_at_receipt, received_date,
         delivery_reference, notes, received_by, now),
    )
    receipt_id = cur.fetchone()["id"]
    db.execute(
        "INSERT INTO inventory_transactions (item_id, change_qty, reason, ref_id, timestamp, user_id) "
        "VALUES (?,?,?,?,?,?)",
        (item_id, quantity, "consignment_receipt", str(receipt_id), now, received_by),
    )
    return receipt_id


def record_consignment_shrinkage(db, item_id, distributor_id, quantity, reason, liable_party,
                                  liability_overridden, notes, logged_by):
    """
    Writes off damaged/expired consignment stock before it ever sold.
    Locks the item's inventory_list row (SELECT ... FOR UPDATE) before
    checking quantity against current shelf stock — same race this app's
    POS checkout should also close (two concurrent write-offs could
    otherwise both read the same "before" stock and together take it
    negative). unit_cost is snapshotted from the item's current
    cost_price at the moment of write-off. Paired with an
    inventory_transactions row (-quantity), same shelf-count effect as a
    sale. Does not commit.

    Returns (ok, shrinkage_id_or_None, error_message_or_None).
    """
    db.execute("SELECT id FROM inventory_list WHERE id=? FOR UPDATE", (item_id,))
    status = inventory_status_by_id(db, item_id)
    # current_stock is None until this item has a confirmed audit — fail
    # closed rather than let `quantity > None` either silently pass or
    # raise a TypeError.
    if status and status["current_stock"] is None:
        return False, None, "This item hasn't been through an inventory audit yet — run an audit before writing off stock."
    current_stock = status["current_stock"] if status else 0
    if quantity > current_stock:
        return False, None, f"Only {current_stock:g} unit(s) on the shelf — can't write off {quantity:g}."
    item = db.execute("SELECT cost_price FROM inventory_list WHERE id=?", (item_id,)).fetchone()
    unit_cost = (item["cost_price"] or 0) if item else 0
    now = datetime.now().isoformat(timespec="seconds")
    cur = db.execute(
        "INSERT INTO consignment_shrinkage (item_id, distributor_id, quantity, reason, liable_party, "
        "liability_overridden, unit_cost, notes, logged_by, logged_at) VALUES (?,?,?,?,?,?,?,?,?,?) RETURNING id",
        (item_id, distributor_id, quantity, reason, liable_party,
         1 if liability_overridden else 0, unit_cost, notes, logged_by, now),
    )
    shrinkage_id = cur.fetchone()["id"]
    db.execute(
        "INSERT INTO inventory_transactions (item_id, change_qty, reason, ref_id, timestamp, user_id) "
        "VALUES (?,?,?,?,?,?)",
        (item_id, -quantity, "shrinkage", str(shrinkage_id), now, logged_by),
    )
    return True, shrinkage_id, None


def record_consignment_return(db, item_id, distributor_id, quantity, return_date, reason, notes, returned_by):
    """
    Logs unsold stock physically handed back to the distributor — mirror
    image of receiving. Same locking/capping pattern as
    record_consignment_shrinkage() above (can't return more than what's
    actually on the shelf). unit_cost_at_return is snapshotted from the
    item's current cost_price. Paired with an inventory_transactions row
    (-quantity). No revenue/COGS/settlement impact — nothing sold, so
    nothing owed either way; returns never appear in
    consignment_balance()'s formula. Does not commit.

    Returns (ok, return_id_or_None, error_message_or_None).
    """
    db.execute("SELECT id FROM inventory_list WHERE id=? FOR UPDATE", (item_id,))
    status = inventory_status_by_id(db, item_id)
    if status and status["current_stock"] is None:
        return False, None, "This item hasn't been through an inventory audit yet — run an audit before returning stock."
    current_stock = status["current_stock"] if status else 0
    if quantity > current_stock:
        return False, None, f"Only {current_stock:g} unit(s) on the shelf — can't return {quantity:g}."
    item = db.execute("SELECT cost_price FROM inventory_list WHERE id=?", (item_id,)).fetchone()
    unit_cost = (item["cost_price"] or 0) if item else 0
    now = datetime.now().isoformat(timespec="seconds")
    cur = db.execute(
        "INSERT INTO consignment_returns (item_id, distributor_id, quantity, unit_cost_at_return, "
        "return_date, reason, notes, returned_by, created_at) VALUES (?,?,?,?,?,?,?,?,?) RETURNING id",
        (item_id, distributor_id, quantity, unit_cost, return_date, reason, notes, returned_by, now),
    )
    return_id = cur.fetchone()["id"]
    db.execute(
        "INSERT INTO inventory_transactions (item_id, change_qty, reason, ref_id, timestamp, user_id) "
        "VALUES (?,?,?,?,?,?)",
        (item_id, -quantity, "consignment_return", str(return_id), now, returned_by),
    )
    return True, return_id, None


def consignment_balance(db, distributor_id):
    """
    The payable balance for one distributor, as of right now.

        amount_owed = residual_from_last_settlement + new_activity_since

    residual_from_last_settlement: (last_settlement.amount_owed -
    amount_paid) — 0 if there's no prior settlement, or it was paid in
    full. A confirmed partial settlement's unpaid remainder carries
    forward exactly this way into the next period.

    new_activity_since, restricted to this distributor's Consignment
    items, uses sale_items.unit_cost — snapshotted at sale time, so this
    can't retroactively change if a Price List / Inventory cost is edited
    later (unlike a live join to inventory_list.cost_price would):
        + units sold                 sale_items.quantity * unit_cost
        - sales reversed & restocked refund_items.quantity * cost
        + Clinic-liable shrinkage    consignment_shrinkage.quantity * unit_cost
    Distributor-liable shrinkage adds nothing — they absorb that loss
    directly, not you. Returns never appear here — no money changes
    hands on a return.

    The restock-reversal term is the one place this can't use a
    per-line snapshot: refund_items doesn't link back to the specific
    sale_items row it came from (only item_id + quantity), so it values
    the reversal at *current* inventory_list.cost_price rather than the
    original sale's cost. In practice this only matters if that item's
    cost_price changed between the original sale and the refund, which
    is a narrow window for something a clinic would return.

    period_start/period_end are exclusive/inclusive bounds matching
    consignment_settlements' own column semantics — a caller recording a
    new settlement should write this call's period_start/period_end
    straight into those columns.

    The sales/refund scan is additionally floored per item at
    inventory_list.consignment_since (when set) — GREATEST'd against
    period_start — so a sale from before that specific item was actually
    flagged Consignment (e.g. years of prior Retail sales on an item that
    only just got flagged) never counts toward what's owed. Without this,
    a brand-new distributor with no receiving logged yet has no
    period_start at all, and every historical sale of a newly-flagged
    item would be swept in.

    Returns a dict: residual, period_start, period_end, new_activity,
    amount_owed, units_sold_since, last_settlement_date.
    """
    last = db.execute(
        "SELECT * FROM consignment_settlements WHERE distributor_id=? ORDER BY created_at DESC LIMIT 1",
        (distributor_id,),
    ).fetchone()
    if last:
        residual = round((last["amount_owed"] or 0) - (last["amount_paid"] or 0), 2)
        period_start = last["period_end"]
        last_settlement_date = last["created_at"]
    else:
        # No prior settlement — start from this distributor's earliest
        # consignment activity of any kind (receiving is when their
        # stock first became sellable at all).
        earliest = db.execute(
            "SELECT MIN(x) AS m FROM ("
            "  SELECT MIN(created_at) AS x FROM consignment_receipts WHERE distributor_id=?"
            "  UNION ALL SELECT MIN(logged_at) FROM consignment_shrinkage WHERE distributor_id=?"
            "  UNION ALL SELECT MIN(created_at) FROM consignment_returns WHERE distributor_id=?"
            ") t",
            (distributor_id, distributor_id, distributor_id),
        ).fetchone()
        residual = 0
        period_start = earliest["m"] if earliest else None
        last_settlement_date = None

    period_end = datetime.now().isoformat(timespec="seconds")

    sold_where = (
        "WHERE i.ownership_type='Consignment' AND i.distributor_id=? "
        "AND s.sale_date > GREATEST(?, COALESCE(i.consignment_since, ''))"
    )
    sold_params = [distributor_id, period_start or ""]
    sold_row = db.execute(
        "SELECT COALESCE(SUM(si.quantity * COALESCE(si.unit_cost, 0)), 0) AS cost, "
        "COALESCE(SUM(si.quantity), 0) AS units "
        "FROM sale_items si JOIN sales s ON s.id = si.sale_id JOIN inventory_list i ON i.id = si.item_id "
        + sold_where,
        sold_params,
    ).fetchone()
    sold_cost = sold_row["cost"] or 0
    units_sold = sold_row["units"] or 0

    # refund_date is a DATE column (day precision only) while
    # period_start/consignment_since are full timestamps — comparing at
    # day granularity is the best this data actually supports; see the
    # docstring above.
    cost_by_item = {r["id"]: r["cost_price"] or 0 for r in db.execute("SELECT id, cost_price FROM inventory_list").fetchall()}
    restock_where = (
        "WHERE i.ownership_type='Consignment' AND i.distributor_id=? AND r.restocked=1 "
        "AND r.refund_date > GREATEST(?::date, COALESCE(i.consignment_since::date, '-infinity'::date))"
    )
    restock_params = [distributor_id, period_start[:10] if period_start else "-infinity"]
    restocked_rows = db.execute(
        "SELECT ri.item_id, ri.quantity FROM refund_items ri JOIN refunds r ON r.id=ri.refund_id "
        "JOIN inventory_list i ON i.id = ri.item_id " + restock_where,
        restock_params,
    ).fetchall()
    restocked_cost = sum(rr["quantity"] * cost_by_item.get(rr["item_id"], 0) for rr in restocked_rows)

    shrink_where = "WHERE distributor_id=? AND liable_party='Clinic'"
    shrink_params = [distributor_id]
    if period_start:
        shrink_where += " AND logged_at > ?"
        shrink_params.append(period_start)
    shrink_row = db.execute(
        "SELECT COALESCE(SUM(quantity * unit_cost), 0) AS cost FROM consignment_shrinkage " + shrink_where,
        shrink_params,
    ).fetchone()
    shrinkage_cost = shrink_row["cost"] or 0

    new_activity = round(sold_cost - restocked_cost + shrinkage_cost, 2)
    amount_owed = round(residual + new_activity, 2)

    return {
        "residual": residual, "period_start": period_start, "period_end": period_end,
        "new_activity": new_activity, "amount_owed": amount_owed,
        "units_sold_since": units_sold, "last_settlement_date": last_settlement_date,
    }


def consignment_distributors_overview(db):
    """
    One row per distributor with >=1 Consignment item: shelf stock (units
    + value at their cost), amount owed right now, last settlement date,
    units sold this month. Calls consignment_balance() per distributor —
    fine at the scale this screen is for (a clinic's number of
    distributor relationships, not its transaction volume).

    inventory_status(db) is computed exactly ONCE up front and looked up
    by item_id from a dict — NOT via inventory_status_by_id() per
    Consignment item, which would recompute the whole catalog's status
    just to return one row (O(distributors x items-per-distributor x
    full-catalog-size) — fine with a handful of test rows, severe on a
    clinic's real inventory history).
    """
    status_by_item = {s["item_id"]: s for s in inventory_status(db)}
    distributors = db.execute(
        "SELECT DISTINCT d.id, d.name FROM distributors d "
        "JOIN inventory_list i ON i.distributor_id = d.id "
        "WHERE i.ownership_type='Consignment' ORDER BY d.name"
    ).fetchall()
    this_month = date.today().isoformat()[:7]
    out = []
    for d in distributors:
        items = db.execute(
            "SELECT id, cost_price FROM inventory_list WHERE distributor_id=? AND ownership_type='Consignment'",
            (d["id"],),
        ).fetchall()
        shelf_units, shelf_value = 0, 0
        for it in items:
            status = status_by_item.get(it["id"])
            stock = (status["current_stock"] if status else 0) or 0
            shelf_units += stock
            shelf_value += stock * (it["cost_price"] or 0)
        month_units = db.execute(
            "SELECT COALESCE(SUM(si.quantity), 0) AS u FROM sale_items si "
            "JOIN sales s ON s.id=si.sale_id JOIN inventory_list i ON i.id=si.item_id "
            "WHERE i.distributor_id=? AND i.ownership_type='Consignment' AND s.sale_date LIKE ?",
            (d["id"], this_month + "%"),
        ).fetchone()["u"] or 0
        balance = consignment_balance(db, d["id"])
        out.append({
            "distributor_id": d["id"], "distributor_name": d["name"],
            "shelf_units": shelf_units, "shelf_value": round(shelf_value, 2),
            "amount_owed": balance["amount_owed"], "last_settlement_date": balance["last_settlement_date"],
            "units_sold_this_month": month_units,
        })
    return out


def consignment_sales_by_distributor(db, distributor_id=None, date_from=None, date_to=None):
    """
    Sales-by-distributor report. Uses sale_items.unit_cost — the
    price/cost as it actually stood at the moment of that specific sale
    (snapshotted at sale time) — not a live join to inventory_list, so
    this always agrees with consignment_balance()'s own settlement math
    on the same number for the same sale.
    """
    where = ["i.ownership_type = 'Consignment'"]
    params = []
    if distributor_id:
        where.append("d.id = ?")
        params.append(distributor_id)
    if date_from:
        where.append("s.sale_date >= ?")
        params.append(date_from)
    if date_to:
        where.append("s.sale_date < ?")
        params.append(date_to)
    rows = db.execute(
        "SELECT d.id AS distributor_id, d.name AS distributor_name, "
        "substring(s.sale_date, 1, 7) AS month, i.id AS item_id, i.name AS item_name, "
        "SUM(si.quantity) AS units_sold, SUM(si.line_total) AS revenue, "
        "SUM(si.quantity * COALESCE(si.unit_cost, 0)) AS owed_to_distributor, "
        "SUM(si.line_total) - SUM(si.quantity * COALESCE(si.unit_cost, 0)) AS your_markup "
        "FROM sale_items si JOIN sales s ON s.id = si.sale_id JOIN inventory_list i ON i.id = si.item_id "
        "JOIN distributors d ON d.id = i.distributor_id "
        "WHERE " + " AND ".join(where) +
        " GROUP BY d.id, d.name, month, i.id, i.name ORDER BY month DESC, d.name, i.name",
        params,
    ).fetchall()
    return rows


def consignment_item_locked(db, item_id):
    """
    True once a Consignment item has ever had a receipt, sale, or
    settlement-relevant activity against it — at that point its
    distributor_id becomes uneditable in the UI (an item never switches
    distributors mid-life; a supply-source change means a new
    inventory_list row, not a re-point of this one, so historical
    settlement math for the old distributor can't silently break).

    The sale_items check is deliberately scoped to items that are
    CURRENTLY ownership_type='Consignment' — consignment_balance() only
    ever sums a sale_items row into a distributor's balance while its
    item is presently Consignment (see that function's own query), so a
    plain Retail item's sale history from whenever it was Owned isn't
    "consignment-relevant activity" and shouldn't block it from being
    flagged as Consignment for the first time. Checking unconditionally
    would mean almost any actively-sold retail item could never be
    flagged at all. consignment_receipts/shrinkage/returns don't have
    this problem: those tables can only ever gain a row for an item that
    was Consignment at the moment it happened (each recording route
    itself requires ownership_type='Consignment' first), so an Owned item
    can never have false-positive rows there.
    """
    if db.execute("SELECT 1 FROM consignment_receipts WHERE item_id=?", (item_id,)).fetchone():
        return True
    item = db.execute("SELECT ownership_type FROM inventory_list WHERE id=?", (item_id,)).fetchone()
    if item and item["ownership_type"] == "Consignment":
        if db.execute("SELECT 1 FROM sale_items WHERE item_id=?", (item_id,)).fetchone():
            return True
    if db.execute("SELECT 1 FROM consignment_shrinkage WHERE item_id=?", (item_id,)).fetchone():
        return True
    if db.execute("SELECT 1 FROM consignment_returns WHERE item_id=?", (item_id,)).fetchone():
        return True
    return False


# ---------------------------------------------------------------------------
# Cash Register — unifies every place money actually changes hands: POS
# sales, Visit/Inpatient/Boarding payments, and refunds (negative, so they
# net out automatically). Distinct from every other billing table in this
# module, which tracks what's *owed*, not what's been *collected* — that
# distinction is the whole point of this feature: comparing what the
# system says came in against what's physically in the till, to catch
# poor documentation or leaking/stolen cash early.
#
# Distributor bill payments and consignment settlements are deliberately
# excluded — that's paying a supplier, not collecting from a customer. Cash
# paid to a supplier out of the till belongs in cash_register_payouts
# instead, logged explicitly with a reason, same as any other till payout.
# ---------------------------------------------------------------------------
CASH_REGISTER_METHODS = ("Cash", "Card", "Transfer")


def cash_register_ledger(db, day):
    """Every money-in/money-out ledger line for `day` ('YYYY-MM-DD').
    Returns a list of dicts: event_date, employee, subtotal,
    discount_percent, total, payment_method, event_type, ref_id."""
    rows = db.execute(
        "SELECT s.sale_date AS event_date, u.full_name AS employee, s.subtotal AS subtotal, "
        "s.discount_percent AS discount_percent, s.total AS total, s.payment_method AS payment_method, "
        "'POS Sale' AS event_type, s.id AS ref_id "
        "FROM sales s LEFT JOIN users u ON u.id = s.cashier_id WHERE s.sale_date LIKE ? "
        "UNION ALL "
        "SELECT p.date::text, u.full_name, p.amount, 0.0, p.amount, p.method, "
        "CASE WHEN p.visit_id IS NOT NULL THEN 'Visit Payment' "
        "     WHEN p.inpatient_case_id IS NOT NULL THEN 'Inpatient Payment' "
        "     WHEN p.boarding_id IS NOT NULL THEN 'Boarding Payment' "
        "     ELSE 'Payment' END, "
        "p.id "
        "FROM payments p LEFT JOIN users u ON u.id = p.user_id WHERE p.date = ? "
        "UNION ALL "
        "SELECT r.refund_date::text, u.full_name, -r.amount, 0.0, -r.amount, r.refund_method, "
        "CASE WHEN r.refund_type='retail' THEN 'Retail Refund' ELSE 'Service Refund' END, "
        "r.id "
        "FROM refunds r LEFT JOIN users u ON u.id = r.processed_by WHERE r.refund_date = ? "
        "ORDER BY event_date DESC",
        (day + "%", day, day),
    ).fetchall()
    return [dict(r) for r in rows]


def cash_register_totals(db, day):
    """Net Cash/Card/Transfer for `day`: sales + payments - refunds (by
    whichever method the money actually moved through), minus same-day
    cash payouts (always cash — see cash_register_payouts). A payment
    method outside Cash/Card/Transfer (blank, legacy, unexpected) lands in
    'other' rather than being silently dropped from the day's total."""
    ledger = cash_register_ledger(db, day)
    totals = {"Cash": 0.0, "Card": 0.0, "Transfer": 0.0, "other": 0.0}
    for row in ledger:
        bucket = row["payment_method"] if row["payment_method"] in CASH_REGISTER_METHODS else "other"
        totals[bucket] += row["total"] or 0
    payouts_total = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM cash_register_payouts WHERE payout_date=?", (day,)
    ).fetchone()["s"]
    totals["Cash"] = round(totals["Cash"] - payouts_total, 2)
    for k in ("Card", "Transfer", "other"):
        totals[k] = round(totals[k], 2)
    totals["all"] = round(totals["Cash"] + totals["Card"] + totals["Transfer"] + totals["other"], 2)
    return totals


def cash_register_payouts_for_day(db, day):
    return db.execute(
        "SELECT p.*, u.full_name AS logged_by_name FROM cash_register_payouts p "
        "LEFT JOIN users u ON u.id = p.logged_by WHERE p.payout_date=? ORDER BY p.id DESC",
        (day,),
    ).fetchall()


def cash_register_latest_audit(db, day):
    return db.execute(
        "SELECT a.*, u.full_name AS performed_by_name FROM cash_register_audits a "
        "LEFT JOIN users u ON u.id = a.performed_by WHERE a.audit_date=? ORDER BY a.id DESC LIMIT 1",
        (day,),
    ).fetchone()


def cash_register_last_30_days(db):
    """For Insights' Cash Register Health — the last 30 calendar days
    (today inclusive), each with its latest audit status, or 'Not
    Audited' if that day was never closed out. A gap here is exactly
    what this feature exists to surface: a day nobody ever audited is
    itself a documentation problem worth an admin's attention, same as a
    real Deficit."""
    out = []
    today = date.today()
    for i in range(30):
        d = (today - timedelta(days=i)).isoformat()
        audit = cash_register_latest_audit(db, d)
        out.append({
            "date": d,
            "status": audit["status"] if audit else "Not Audited",
            "difference": audit["difference"] if audit else None,
        })
    return out


# ---------------------------------------------------------------------------
# Appointments — dynamic slot grid
# ---------------------------------------------------------------------------
def generate_slots(db):
    start = get_setting(db, "appt_start_time", "09:00")
    end = get_setting(db, "appt_end_time", "18:00")
    try:
        minutes = int(get_setting(db, "appt_slot_minutes", "30"))
    except (TypeError, ValueError):
        minutes = 30
    if minutes <= 0:
        minutes = 30
    t0 = datetime.strptime(start, "%H:%M")
    t1 = datetime.strptime(end, "%H:%M")
    if t1 <= t0:
        return []
    slots = []
    cur = t0
    while cur < t1:
        nxt = cur + timedelta(minutes=minutes)
        start_str = cur.strftime("%H:%M")
        # The slot's own start time doubles as its identifier (label) — this is
        # what gets stored in appointments.slot_label. It's just as good a key
        # as an arbitrary letter would be for conflict-checking/grouping, and
        # it's self-explanatory if you ever look at the raw data.
        slots.append({"label": start_str, "start": start_str, "end": min(nxt, t1).strftime("%H:%M")})
        cur = nxt
    return slots


def week_dates(anchor_iso):
    anchor = parse_date(anchor_iso) or date.today()
    monday = anchor - timedelta(days=anchor.weekday())
    return [monday + timedelta(days=i) for i in range(7)]


def day_grid(db, day_iso):
    vets = db.execute("SELECT id, full_name FROM users WHERE role_id IN (SELECT id FROM roles WHERE is_vet_role=true) AND active=1 ORDER BY full_name").fetchall()
    slots = generate_slots(db)
    appts = db.execute("SELECT * FROM appointments WHERE appt_date=?", (day_iso,)).fetchall()

    by_cell = defaultdict(list)
    for a in appts:
        by_cell[(a["slot_label"], a["resource_type"], a["resource_id"])].append(dict(a))

    columns = [{"resource_type": "vet", "resource_id": v["id"], "label": v["full_name"]} for v in vets]
    columns.append({"resource_type": "grooming", "resource_id": None, "label": "Grooming"})

    grid = []
    for slot in slots:
        row = {"slot": slot, "cells": []}
        for col in columns:
            cell_appts = by_cell.get((slot["label"], col["resource_type"], col["resource_id"]), [])
            row["cells"].append({"column": col, "appointments": cell_appts})
        grid.append(row)
    return columns, grid


def slot_conflict(db, appt_date, slot_label, resource_type, resource_id):
    row = db.execute(
        "SELECT 1 FROM appointments WHERE appt_date=? AND slot_label=? AND resource_type=? AND "
        "(resource_id=? OR (resource_id IS NULL AND ?::text IS NULL))",
        (appt_date, slot_label, resource_type, resource_id, resource_id),
    ).fetchone()
    return bool(row)
# ---------------------------------------------------------------------------
# BI Insights & Retention (Admin only; enforced at the route level)
#
# All queries below are deliberately written as single set-based SQL
# statements (CTEs / unnest / window-style month math) rather than
# per-row Python loops, so they stay fast as billing/visit/sale history
# grows into the hundreds of thousands of rows — see
# migrate_add_bi_indexes_2026_08.py for the supporting indexes.
# ---------------------------------------------------------------------------
REVENUE_CATEGORIES = ["Service", "Medicine", "Retail", "Boarding"]

# Baghdad's work week is Sunday-Thursday (Friday/Saturday is the weekend).
# Postgres EXTRACT(DOW) already returns 0=Sunday..6=Saturday, i.e. this order.
WEEKDAY_LABELS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
WEEKDAY_IS_WEEKEND = [False, False, False, False, False, True, True]


def month_list(months_back):
    """Returns ['YYYY-MM', ...] for the last N months, oldest first (incl. current)."""
    today = date.today()
    months = []
    y, m = today.year, today.month
    for i in range(months_back - 1, -1, -1):
        mm = m - i
        yy = y
        while mm <= 0:
            mm += 12
            yy -= 1
        months.append(f"{yy:04d}-{mm:02d}")
    return months


def revenue_by_category(db, months_back=12):
    """
    Revenue per month per Price List category (Service/Medicine/Retail),
    plus a synthetic 'Boarding' category, net of that month's refunds.
    Mirrors the same revenue formulas as the Monthly P&L (logic.monthly_pl),
    just split out by category instead of collapsed into one number.
    """
    months = month_list(months_back)
    cutoff = months[0] + "-01"
    rows = db.execute(
        """
        WITH auto_lines AS (
          SELECT to_char(b.date_billed, 'YYYY-MM') AS month, vbl.category AS category,
                 vbl.unit_price * vbl.quantity * (1 - COALESCE(b.discount_percent,0)/100.0) AS amount
          FROM billing b
          JOIN visit_billing_lines vbl ON vbl.visit_id = b.visit_id
          WHERE b.billing_type = 'Automatic' AND b.date_billed IS NOT NULL
            AND b.date_billed >= ?
        ),
        manual_lines AS (
          SELECT to_char(b.date_billed, 'YYYY-MM') AS month, 'Service' AS category,
                 COALESCE(b.manual_amount,0) * (1 - COALESCE(b.discount_percent,0)/100.0) AS amount
          FROM billing b
          WHERE b.billing_type='Manual' AND b.date_billed IS NOT NULL AND b.date_billed >= ?
        ),
        retail_lines AS (
          SELECT substr(s.sale_date,1,7) AS month, 'Retail' AS category,
                 si.line_total * (1 - COALESCE(s.discount_percent,0)/100.0) AS amount
          FROM sale_items si JOIN sales s ON s.id = si.sale_id
          WHERE s.sale_date >= ?
        ),
        inpatient_lines AS (
          SELECT substr(ib.timestamp,1,7) AS month, pl.category AS category,
                 COALESCE(ib.unit_price, pl.sale_price) * ib.quantity * (1 - COALESCE(ic.discount_percent,0)/100.0) AS amount
          FROM inpatient_billing ib
          JOIN price_list pl ON pl.id = ib.price_id
          JOIN inpatient_cases ic ON ic.id = ib.case_id
          WHERE ib.timestamp >= ?
        ),
        boarding_lines AS (
          SELECT to_char(bs.entry_date, 'YYYY-MM') AS month, 'Boarding' AS category, COALESCE(bs.total,0) AS amount
          FROM boarding_sessions bs
          WHERE bs.total IS NOT NULL AND bs.entry_date >= ?
        ),
        refund_lines AS (
          SELECT to_char(r.refund_date, 'YYYY-MM') AS month,
                 CASE WHEN r.refund_type='retail' THEN 'Retail' ELSE 'Service' END AS category,
                 -r.amount AS amount
          FROM refunds r
          WHERE r.refund_date >= ?
        ),
        all_rev AS (
          SELECT * FROM auto_lines UNION ALL SELECT * FROM manual_lines UNION ALL SELECT * FROM retail_lines
          UNION ALL SELECT * FROM inpatient_lines UNION ALL SELECT * FROM boarding_lines UNION ALL SELECT * FROM refund_lines
        )
        SELECT month, category, SUM(amount) AS revenue
        FROM all_rev
        GROUP BY month, category
        """,
        (cutoff,) * 6,
    ).fetchall()

    grid = {(r["month"], r["category"]): round(r["revenue"] or 0, 2) for r in rows}
    return {
        "months": months,
        "categories": REVENUE_CATEGORIES,
        "grid": {m: {c: grid.get((m, c), 0) for c in REVENUE_CATEGORIES} for m in months},
        "totals_by_category": {c: round(sum(grid.get((m, c), 0) for m in months), 2) for c in REVENUE_CATEGORIES},
        "totals_by_month": {m: round(sum(grid.get((m, c), 0) for c in REVENUE_CATEGORIES), 2) for m in months},
    }


def vet_performance(db, months_back=12):
    """
    Per-vet visit count and total billings (after discount) over the
    trailing window, ranked by revenue. 'doctor' is free text on the visit
    (populated from a Vet-user dropdown), not a hard FK, matching how the
    rest of the app records it.
    """
    months = month_list(months_back)
    cutoff = months[0] + "-01"
    rows = db.execute(
        """
        WITH visit_totals AS (
          SELECT b.visit_id, b.billing_type, b.manual_amount, b.discount_percent,
                 COALESCE(SUM(vbl.unit_price * vbl.quantity), 0) AS auto_subtotal
          FROM billing b
          LEFT JOIN visit_billing_lines vbl ON vbl.visit_id = b.visit_id AND b.billing_type='Automatic'
          GROUP BY b.visit_id, b.billing_type, b.manual_amount, b.discount_percent
        )
        SELECT v.doctor, COUNT(DISTINCT v.id) AS visit_count,
               COALESCE(SUM(CASE WHEN vt.billing_type='Manual' THEN vt.manual_amount ELSE vt.auto_subtotal END
                            * (1-COALESCE(vt.discount_percent,0)/100.0)),0) AS revenue
        FROM visits v
        LEFT JOIN visit_totals vt ON vt.visit_id = v.id
        WHERE v.doctor IS NOT NULL AND v.doctor <> '' AND v.date >= ?
        GROUP BY v.doctor
        ORDER BY revenue DESC
        """,
        (cutoff,),
    ).fetchall()
    out = []
    for r in rows:
        revenue = round(r["revenue"] or 0, 2)
        visits = r["visit_count"] or 0
        out.append({
            "doctor": r["doctor"], "visit_count": visits, "revenue": revenue,
            "avg_revenue_per_visit": round(revenue / visits, 2) if visits else 0,
        })
    return out


def client_value(db, limit=20):
    """
    Lifetime spend per owner, from payments linked to that owner's visits,
    inpatient cases, or boarding stays (POS retail sales are anonymous
    walk-in transactions in this schema and have no owner link, so they're
    intentionally excluded from per-client figures).
    Returns (top_clients, average_spend_per_active_client, active_client_count).
    """
    rows = db.execute(
        """
        WITH owner_payments AS (
          SELECT pa.owner_id, p.amount
          FROM payments p JOIN visits v ON v.id = p.visit_id JOIN patients pa ON pa.id = v.patient_id
          WHERE p.visit_id IS NOT NULL
          UNION ALL
          SELECT pa.owner_id, p.amount
          FROM payments p JOIN inpatient_cases ic ON ic.id = p.inpatient_case_id JOIN patients pa ON pa.id = ic.patient_id
          WHERE p.inpatient_case_id IS NOT NULL
          UNION ALL
          SELECT pa.owner_id, p.amount
          FROM payments p JOIN boarding_sessions bs ON bs.id = p.boarding_id JOIN patients pa ON pa.id = bs.patient_id
          WHERE p.boarding_id IS NOT NULL
        )
        SELECT o.id, o.name, COUNT(*) AS payment_count, SUM(op.amount) AS total_paid
        FROM owner_payments op JOIN owners o ON o.id = op.owner_id
        GROUP BY o.id, o.name
        ORDER BY total_paid DESC
        """
    ).fetchall()
    active = [{"id": r["id"], "name": r["name"], "payment_count": r["payment_count"],
               "total_paid": round(r["total_paid"] or 0, 2)} for r in rows]
    avg_spend = round(sum(r["total_paid"] for r in active) / len(active), 2) if active else 0
    return active[:limit], avg_spend, len(active)


def appointment_weekday_load(db, months_back=12):
    """
    Scheduling demand by day of week (Baghdad work week: Sun-Thu, with
    Fri/Sat flagged as the weekend), plus a same-day visit count as a rough
    fulfillment signal. NOTE: appointments aren't linked to visits by ID in
    this schema (no visit_id on the appointments table), so "visits that
    day" is a date-level proxy for demand actually showing up — not a
    per-appointment no-show match. Framed as an approximation in the UI.
    """
    months = month_list(months_back)
    cutoff = months[0] + "-01"
    appt_rows = db.execute(
        "SELECT EXTRACT(DOW FROM appt_date::date)::int AS dow, COUNT(*) AS c "
        "FROM appointments WHERE appt_date >= ? GROUP BY 1",
        (cutoff,),
    ).fetchall()
    visit_rows = db.execute(
        "SELECT EXTRACT(DOW FROM date::date)::int AS dow, COUNT(*) AS c "
        "FROM visits WHERE date IS NOT NULL AND date >= ? GROUP BY 1",
        (cutoff,),
    ).fetchall()
    appt_by_dow = {r["dow"]: r["c"] for r in appt_rows}
    visit_by_dow = {r["dow"]: r["c"] for r in visit_rows}
    out = []
    for dow in range(7):
        appts = appt_by_dow.get(dow, 0)
        visits = visit_by_dow.get(dow, 0)
        out.append({
            "day": WEEKDAY_LABELS[dow], "is_weekend": WEEKDAY_IS_WEEKEND[dow],
            "appointments": appts, "visits_same_weekday": visits,
            "fulfillment_ratio": round(visits / appts, 2) if appts else None,
        })
    return out


def inpatient_boarding_occupancy(db, months_back=12):
    """
    Active inpatient cases and active boarding stays as of the first day of
    each of the last N months (bounded to N x row-count comparisons, so it
    stays cheap no matter how long a case's stay is), plus avg length of
    stay and admissions-per-month for each.
    """
    months_sql = db.execute(
        """
        WITH months AS (
          SELECT to_char(date_trunc('month', current_date) - (g || ' months')::interval, 'YYYY-MM') AS month,
                 (date_trunc('month', current_date) - (g || ' months')::interval)::date AS month_start,
                 (date_trunc('month', current_date) - (g || ' months')::interval + interval '1 month' - interval '1 day')::date AS month_end
          FROM generate_series(0, ?) g
        )
        SELECT m.month,
               (SELECT COUNT(*) FROM inpatient_cases ic
                WHERE ic.admission_date <= m.month_end
                  AND (ic.dismissal_date IS NULL OR ic.dismissal_date >= m.month_start)) AS active_inpatient,
               (SELECT COUNT(*) FROM boarding_sessions bs
                WHERE bs.entry_date <= m.month_end
                  AND (bs.dismissal_date IS NULL OR bs.dismissal_date >= m.month_start)) AS active_boarding
        FROM months m
        ORDER BY m.month
        """,
        (months_back - 1,),
    ).fetchall()

    avg_stay = db.execute(
        "SELECT AVG(dismissal_date - admission_date) AS d FROM inpatient_cases "
        "WHERE dismissed=1 AND dismissal_date IS NOT NULL"
    ).fetchone()["d"]
    avg_boarding_stay = db.execute(
        "SELECT AVG(dismissal_date - entry_date) AS d FROM boarding_sessions "
        "WHERE dismissed=1 AND dismissal_date IS NOT NULL"
    ).fetchone()["d"]

    return {
        "by_month": [dict(r) for r in months_sql],
        "avg_inpatient_stay_days": round(float(avg_stay), 1) if avg_stay is not None else None,
        "avg_boarding_stay_days": round(float(avg_boarding_stay), 1) if avg_boarding_stay is not None else None,
    }


def cohort_retention_grid(db, max_offset=11):
    """
    Classic cohort/retention grid: each row is the cohort of patients whose
    FIRST visit fell in that month; each column is 'N months after their
    first visit'; each cell is the % of that cohort with >=1 visit in that
    offset month. Columns are capped at max_offset (keeps the grid a fixed
    width no matter how long the clinic has been open); rows are NOT
    capped here — returns every cohort month on record, newest first, and
    it's the caller's job to paginate for display.
    """
    rows = db.execute(
        """
        WITH first_visit AS (
          SELECT patient_id, MIN(date) AS first_date
          FROM visits WHERE date IS NOT NULL
          GROUP BY patient_id
        ),
        cohorts AS (
          SELECT patient_id, to_char(first_date, 'YYYY-MM') AS cohort_month, first_date
          FROM first_visit
        ),
        cohort_sizes AS (
          SELECT cohort_month, COUNT(*) AS cohort_size FROM cohorts GROUP BY cohort_month
        ),
        visit_offsets AS (
          SELECT c.cohort_month, c.patient_id,
            ( (EXTRACT(YEAR FROM v.date) - EXTRACT(YEAR FROM c.first_date)) * 12
              + (EXTRACT(MONTH FROM v.date) - EXTRACT(MONTH FROM c.first_date)) )::int AS month_offset
          FROM visits v
          JOIN cohorts c ON c.patient_id = v.patient_id
          WHERE v.date IS NOT NULL
        ),
        retained AS (
          SELECT cohort_month, month_offset, COUNT(DISTINCT patient_id) AS retained_count
          FROM visit_offsets
          WHERE month_offset BETWEEN 0 AND ?
          GROUP BY cohort_month, month_offset
        )
        SELECT r.cohort_month, r.month_offset, r.retained_count, cs.cohort_size
        FROM retained r JOIN cohort_sizes cs ON cs.cohort_month = r.cohort_month
        ORDER BY r.cohort_month, r.month_offset
        """,
        (max_offset,),
    ).fetchall()

    by_cohort = defaultdict(dict)
    cohort_size = {}
    for r in rows:
        by_cohort[r["cohort_month"]][r["month_offset"]] = r["retained_count"]
        cohort_size[r["cohort_month"]] = r["cohort_size"]

    cohort_months = sorted(by_cohort.keys(), reverse=True)
    grid = []
    for cm in cohort_months:
        size = cohort_size[cm]
        row = {"cohort_month": cm, "cohort_size": size, "cells": []}
        for offset in range(max_offset + 1):
            retained = by_cohort[cm].get(offset)
            # Only show a cell once that much time has actually elapsed since the cohort started.
            months_elapsed = (
                (date.today().year - int(cm[:4])) * 12 + (date.today().month - int(cm[5:7]))
            )
            if offset > months_elapsed:
                row["cells"].append(None)
            else:
                pct = round(100 * (retained or 0) / size) if size else 0
                row["cells"].append(pct)
        grid.append(row)

    return {"cohort_months": cohort_months, "offsets": list(range(max_offset + 1)), "grid": grid}


