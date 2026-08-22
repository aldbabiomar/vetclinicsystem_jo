"""
Database builder for Jordan Referral Center (v3 schema).
Run once with:  python3 import_seed.py
Builds the Postgres schema and loads any starter data from seed_data.json
(currently empty - fill it in, or just enter data through the app once it's running).
Refuses to run if the database already has data in it, so it can't
accidentally wipe anything — see main() below.
"""
import json
import re
import os
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

import auth
import db as dbmod

BASE_DIR = os.path.dirname(__file__)
SCHEMA_PATH = os.path.join(BASE_DIR, "schema_postgres.sql")
SEED_PATH = os.path.join(BASE_DIR, "seed_data.json")


# Old free-text Price List categories -> Service / Medicine / Retail.
# Best-effort auto-mapping; review it in Price List once the app is running.
PRICE_CATEGORY_MAP = {
    "Boarding": "Service", "Grooming": "Service", "Pet Relocation": "Service",
    "Medications & Eye/Ear Drops": "Medicine", "Preventatives": "Medicine",
    "Diagnostics": "Service", "Biopsy": "Service", "Oral Procedures": "Service",
    "Wound Procedures": "Service", "Avian/Exotic": "Service", "Breeding": "Service",
    "Euthanasia": "Service", "Disease Packages": "Service", "Orthopedics": "Service",
    "Surgeries": "Service",
}

# Old free-text Inventory categories -> Medical / Retail.
INVENTORY_CATEGORY_MAP = {
    "Non-Medical Supplies": "Retail",
}

CASE_STATUS_MAP = {
    "Ongoing": "Ongoing", "Admitted": "Admitted to Inpatient", "Resolved": "Resolved",
    "Lost to follow-up": "Lost to Follow Up", "Deceased": "Deceased/Euthanized", "Referred": "Referred",
}

INPATIENT_CASE_STATUSES_CLOSED = {"Resolved", "Deceased/Euthanized", "Lost to Follow Up", "Referred"}


def clean_date(v):
    if not v:
        return None
    v = str(v).strip()
    if v == "" or v.lower() == "none":
        return None
    return v.split(" ")[0]


def s(v):
    if v is None:
        return None
    v = str(v).strip()
    return v if v else None


def main():
    con = dbmod.connect()
    existing = con.execute("SELECT COUNT(*) AS n FROM owners").fetchone()
    if existing and existing["n"]:
        raise SystemExit(
            "Postgres already has data in it — import_seed.py refuses to run "
            "against a non-empty database to avoid wiping real clinic data. "
            "If you really want to start over, wipe the Postgres data volume first."
        )
    dbmod.run_script(con, open(SCHEMA_PATH).read())
    auth.seed_default_roles_and_permissions(con)
    cur = con

    data = json.load(open(SEED_PATH))

    # ---------------- Users (seed one admin account) ----------------
    admin_id = auth.new_user_id()
    admin_role_id = cur.execute("SELECT id FROM roles WHERE name='Admin'").fetchone()["id"]
    cur.execute(
        "INSERT INTO users (id,username,password_hash,full_name,role_id,active,must_change_password,created_at) "
        "VALUES (?,?,?,?,?,true,true,?)",
        (admin_id, "admin", auth.hash_password("admin123"), "Clinic Admin", admin_role_id,
         datetime.now().isoformat(timespec="seconds")),
    )
    print("Seeded admin account -> username: admin / password: admin123 (must be changed on first login)")

    # ---------------- Owners + Patients (dedupe owners by name+phone) ----------------
    owner_map, owner_n, patient_n = {}, 0, 0
    for row in data["patients"]:
        pid = s(row[0])
        if not pid or not re.match(r"^PT\d+$", pid):
            continue
        owner_name, owner_phone = s(row[1]), s(row[2])
        key = (owner_name or "", owner_phone or "")
        if key not in owner_map:
            owner_n += 1
            oid = f"OW{owner_n:03d}"
            owner_map[key] = oid
            cur.execute("INSERT INTO owners (id,name,phone) VALUES (?,?,?)", (oid, owner_name, owner_phone))
        owner_id = owner_map[key]
        cur.execute(
            "INSERT INTO patients (id,owner_id,animal_name,species,sex,age_note,repro_status,housing,notes) "
            "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT (id) DO NOTHING",
            (pid, owner_id, s(row[3]), s(row[4]), s(row[5]), s(row[6]), s(row[7]), s(row[8]), s(row[9])),
        )
        patient_n += 1
    print("owners:", owner_n, "  patients:", patient_n)

    # ---------------- Price List (remap to Service/Medicine/Retail) ----------------
    price_rows, n = [], 0
    for row in data["price_list"]:
        pid = s(row[0])
        if not pid or not re.match(r"^P\d+$", pid):
            continue
        new_cat = PRICE_CATEGORY_MAP.get(s(row[2]), "Service")
        price_rows.append((pid, s(row[1]), new_cat, row[3], row[4], s(row[6])))

    for pid, name, cat, cost, sale, notes in price_rows:
        cur.execute(
            "INSERT INTO price_list (id,name,category,cost_price,sale_price,notes,active) VALUES (?,?,?,?,?,?,1) "
            "ON CONFLICT (id) DO NOTHING",
            (pid, name, cat, cost, sale, notes),
        )
        n += 1
    print("price_list:", n)

    # ---------------- Inventory List (remap to Medical/Retail) ----------------
    n = 0
    inv_name_lookup = {}
    for row in data["inventory_list"]:
        iid = s(row[0])
        if not iid or not re.match(r"^INV\d+$", iid):
            continue
        old_cat = s(row[2])
        new_cat = INVENTORY_CATEGORY_MAP.get(old_cat, "Medical")
        track_expiry = 1 if s(row[4]) == "Y" else 0
        active = 1 if s(row[7]) != "N" else 0
        name = s(row[1])
        cur.execute(
            "INSERT INTO inventory_list (id,name,category,unit,track_expiry,cost_price,distributor_id,active,barcode,notes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT (id) DO NOTHING",
            (iid, name, new_cat, s(row[3]), track_expiry, row[5], None, active, None, s(row[8])),
        )
        if name:
            inv_name_lookup[name.strip().lower()] = iid
        n += 1
    print("inventory_list:", n)

    linked = 0
    for pid, name, cat, cost, sale, notes in price_rows:
        if cat == "Retail" and name and name.strip().lower() in inv_name_lookup:
            cur.execute("UPDATE price_list SET linked_item_id=? WHERE id=?", (inv_name_lookup[name.strip().lower()], pid))
            linked += 1
    print("price_list rows auto-linked to inventory:", linked)

    # ---------------- Visits (+ spin off Inpatient cases) ----------------
    n_visits = n_cases = 0
    for row in data["visits"]:
        vid = s(row[0])
        if not vid or not re.match(r"^V\d+$", vid):
            continue
        patient_id = s(row[1])
        visit_type = s(row[4])
        visit_date = clean_date(row[5])
        admission_date = clean_date(row[11])
        discharge_date = clean_date(row[12])
        old_case_status = s(row[13])
        case_status = CASE_STATUS_MAP.get(old_case_status, "Needs Filling")

        cur.execute(
            """INSERT INTO visits
            (id,patient_id,visit_type,date,doctor,complaint,history,exam,treatment,
             case_status,case_status_changed_at,updates_log,
             followup_needed,followup_method,followup_reason,followup_date,followup_status,
             wellness_needed,grooming_needed,payment_status,created_by)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (id) DO NOTHING""",
            (
                vid, patient_id, visit_type, visit_date, s(row[6]), s(row[7]), s(row[8]), s(row[9]), s(row[10]),
                case_status, visit_date, s(row[14]), s(row[15]) or "N", s(row[16]), s(row[17]), clean_date(row[18]),
                s(row[19]) or "N/A", "N", "N", s(row[21]) or "N/A", None,
            ),
        )
        n_visits += 1

        if visit_type == "Inpatient" and admission_date:
            dismissed = 1 if (old_case_status in ("Resolved", "Deceased", "Lost to follow-up", "Referred") or discharge_date) else 0
            cur.execute(
                """INSERT INTO inpatient_cases
                (patient_id, visit_id, complaint, exam_findings, admission_date, admitted_items,
                 dismissed, dismissal_date, attending_vet_id, supervising_vet_id, discount_percent, created_by)
                VALUES (?,?,?,?,?,?,?,?,?,?,0,?)""",
                (patient_id, vid, s(row[7]), s(row[9]), admission_date, None, dismissed, discharge_date, None, None, None),
            )
            n_cases += 1
    print("visits:", n_visits, "  inpatient_cases (auto-created from Inpatient visits):", n_cases)

    # ---------------- Billing (Automatic, since source data used price codes) ----------------
    n = 0
    for row in data["billing"]:
        vid = s(row[0])
        if not vid or not re.match(r"^V\d+$", vid):
            continue
        r = cur.execute("SELECT date FROM visits WHERE id=?", (vid,)).fetchone()
        date_billed = r["date"] if r else None
        cur.execute(
            "INSERT INTO billing (visit_id,billing_type,codes,date_billed,discount_percent,notes) "
            "VALUES (?,'Automatic',?,?,0,?) ON CONFLICT (visit_id) DO NOTHING",
            (vid, s(row[1]), date_billed, s(row[5])),
        )
        n += 1
    print("billing:", n)

    # ---------------- Audit history -> one Confirmed audit session per historical date ----------------
    name_to_id = {row["name"]: row["id"] for row in cur.execute("SELECT id, name FROM inventory_list").fetchall()}

    sessions_by_date = {}
    n = 0
    for row in data["audit_history"]:
        audit_date = clean_date(row[0])
        item_name = s(row[1])
        if not audit_date or not re.match(r"^\d{4}-\d{2}-\d{2}$", audit_date):
            continue
        item_id = name_to_id.get(item_name)
        if not item_id:
            continue
        if audit_date not in sessions_by_date:
            now_ts = datetime.now().isoformat(timespec="seconds")
            sessions_by_date[audit_date] = cur.execute(
                "INSERT INTO audit_sessions (audit_date, performed_by, status, created_at, confirmed_at) "
                "VALUES (?,?,'Confirmed',?,?) RETURNING id",
                (audit_date, admin_id, now_ts, now_ts),
            ).fetchone()["id"]
        session_id = sessions_by_date[audit_date]

        critical = None
        if s(row[6]) is not None:
            critical = 1 if s(row[6]) == "Y" else 0
        cur.execute(
            """INSERT INTO audit_session_lines
            (session_id,item_id,stock_counted,received_since_prior,reorder_threshold,
             critical_item,target_coverage_days,nearest_expiry_date,notes)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (session_id, item_id, row[3] or 0, row[4] or 0, row[5], critical, row[7], clean_date(row[8]), s(row[9])),
        )
        n += 1
    print("audit_session_lines (from historical example data):", n, " across", len(sessions_by_date), "confirmed session(s)")

    # ---------------- Default settings ----------------
    defaults = {
        "clinic_name": "Jordan Referral Center",
        "clinic_location": "Amman, Jordan",
        "currency": "JOD",
        "audit_overdue_days": "35",
        "expiry_soon_days": "60",
        "opening_date": "2024-12-28",
        "appt_start_time": "09:00",
        "appt_end_time": "18:00",
        "appt_slot_minutes": "30",
    }
    for k, v in defaults.items():
        cur.execute("INSERT INTO settings (key,value) VALUES (?,?) ON CONFLICT (key) DO NOTHING", (k, v))

    # Prime the atomic ID counters from the highest numeric suffix actually
    # seeded for each prefix. Without this, next_id() would start every
    # prefix fresh at 001 the moment anyone creates a new record through the
    # app — immediately colliding with whatever this seed data already used.
    id_prefixes = {
        "owners": ("id", "OW"), "patients": ("id", "PT"), "distributors": ("id", "D"),
        "price_list": ("id", "P"), "inventory_list": ("id", "INV"), "visits": ("id", "V"),
    }
    for table, (id_col, prefix) in id_prefixes.items():
        rows = cur.execute(f"SELECT {id_col} FROM {table} WHERE {id_col} LIKE ?", (prefix + "%",)).fetchall()
        max_n = 0
        for r in rows:
            suffix = r[id_col][len(prefix):]
            if suffix.isdigit():
                max_n = max(max_n, int(suffix))
        if max_n:
            dbmod.seed_counter(cur, prefix, max_n)
    print("id_counters primed for:", ", ".join(id_prefixes[t][1] for t in id_prefixes))

    con.commit()
    con.close()
    print("\nDatabase built in PostgreSQL.")


if __name__ == "__main__":
    main()
