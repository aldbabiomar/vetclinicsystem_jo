"""
Generates a large, internally-consistent manufactured dataset for
stress-testing Jordan Referral Center (used to validate the Insights / Retention BI
tabs against ~1.7M rows). Pure set-based SQL (generate_series + array
lookups) — no per-row Python inserts — so it can build the whole dataset
in well under a minute even at this scale.

    python3 generate_test_data.py                  # uses DATABASE_URL from .env
    python3 generate_test_data.py --confirm-wipe    # skips the interactive prompt

WARNING: wipes and rebuilds every table in the target database. Never run
this against a real clinic's data — it's for a scratch/test database only.
"""
import sys
import time

from dotenv import load_dotenv
load_dotenv()

import db as dbmod

ROW_COUNTS = {
    "owners": 6147, "patients": 7755, "distributors": 6, "price_list": 187,
    "inventory_list": 66, "users": 11, "visits": 135111, "billing": 118086,
    "payments": 96413, "inpatient_cases": 13598, "inpatient_updates": 86932,
    "inpatient_contact_log": 27341, "inpatient_billing": 54196,
    "boarding_sessions": 8002, "boarding_incidents": 1405, "sales": 80070,
    "sale_items": 240512, "inventory_transactions": 248512, "refunds": 3200,
    "refund_items": 1572, "audit_sessions": 1201, "audit_session_lines": 79266,
    "monthly_opex": 1201, "appointments": 102644, "login_log": 235445,
    "audit_log": 80000, "backup_log": 28146,
}

# Months of visit/billing/sales history to spread activity across, ending today.
HISTORY_MONTHS = 30


def run(con, label, sql):
    t0 = time.time()
    con.execute(sql)
    print(f"  {label}: {time.time()-t0:.1f}s")


def main():
    if "--confirm-wipe" not in sys.argv:
        print("This wipes every table in the target database and refills it with")
        print("manufactured test data. Re-run with --confirm-wipe to proceed.")
        sys.exit(1)

    con = dbmod.connect()
    print("Wiping existing data...")
    con.execute("""
        TRUNCATE owners, patients, distributors, price_list, inventory_list, users,
        visits, billing, payments, inpatient_cases, inpatient_updates,
        inpatient_contact_log, inpatient_billing, boarding_sessions,
        boarding_incidents, sales, sale_items, inventory_transactions, refunds,
        refund_items, audit_sessions, audit_session_lines, monthly_opex,
        appointments, login_log, audit_log, backup_log, id_counters
        RESTART IDENTITY CASCADE
    """)
    con.commit()

    n = ROW_COUNTS

    # ---------------------------------------------------------------- users
    run(con, "users", f"""
        INSERT INTO users (id, username, password_hash, full_name, role_id, active, must_change_password, created_at)
        SELECT 'U' || lpad(g::text,3,'0'),
               'user' || g,
               '$2b$12$placeholderplaceholderplaceholderplaceholderplaceholde',
               (ARRAY['Ahmed Jassim','Zainab Kareem','Mustafa Hadi','Noor Salim','Hussein Ali',
                      'Rania Fadhil','Yousif Kamal','Sara Adnan','Omar Fawzi','Maha Rasheed',
                      'Layla Sabah'])[g],
               (SELECT id FROM roles WHERE name = CASE WHEN g=1 THEN 'Admin' WHEN g<=6 THEN 'Vet' ELSE 'Reception' END),
               true, false, now()::text
        FROM generate_series(1,{n['users']}) g
    """)

    # ------------------------------------------------------------ distributors
    run(con, "distributors", f"""
        INSERT INTO distributors (id, name, contact_person, phone, email, catalog_link, lead_time_days, payment_terms, notes)
        SELECT 'D' || g, 'Distributor ' || g, 'Contact ' || g, '+962' || (700000000 + g),
               'dist' || g || '@example.com', NULL, 3 + (g % 10), 'Net 30', NULL
        FROM generate_series(1,{n['distributors']}) g
    """)

    # -------------------------------------------------------------- owners
    run(con, "owners", f"""
        INSERT INTO owners (id, name, phone, address, notes)
        SELECT 'O' || lpad(g::text,7,'0'),
               (ARRAY['Ahmed','Zainab','Mustafa','Noor','Hussein','Rania','Yousif','Sara','Omar','Maha',
                      'Layla','Karim','Fatima','Ali','Huda','Bilal','Rana','Salam','Dina','Firas'])[1 + (g % 20)]
                 || ' ' ||
               (ARRAY['Al-Sudani','Al-Jubouri','Al-Bayati','Al-Khafaji','Al-Tamimi','Al-Rubaie','Al-Musawi',
                      'Al-Hilali','Al-Zubaidi','Al-Anbari'])[1 + (g % 10)],
               '+962' || (700000000 + g),
               'Baghdad, ' || (ARRAY['Karrada','Mansour','Zayouna','Jadriya','Adhamiyah','Kadhimiya',
                      'Harthiya','Yarmouk','Ghazaliya','Zawra'])[1 + (g % 10)],
               NULL
        FROM generate_series(1,{n['owners']}) g
    """)

    # ------------------------------------------------------------- patients
    run(con, "patients", f"""
        INSERT INTO patients (id, owner_id, animal_name, species, sex, age_note, repro_status, housing, notes)
        SELECT 'PT' || lpad(g::text,7,'0'),
               oid[1 + floor(random()*array_length(oid,1))::int],
               (ARRAY['Simba','Luna','Rex','Bella','Max','Lucy','Milo','Coco','Rocky','Nala',
                      'Leo','Mia','Zeus','Lola','Tiger'])[1 + floor(random()*15)::int],
               (ARRAY['Dog','Cat','Bird','Rabbit'])[1 + floor(random()*4)::int],
               (ARRAY['M','F'])[1 + floor(random()*2)::int],
               (floor(random()*14)+1)::text || ' years',
               (ARRAY['Intact','Neutered','Spayed'])[1 + floor(random()*3)::int],
               (ARRAY['Indoor','Outdoor','Mixed'])[1 + floor(random()*3)::int],
               NULL
        FROM generate_series(1,{n['patients']}) g,
             (SELECT array_agg(id) AS oid FROM owners) o
    """)

    # ------------------------------------------------------------ price_list
    run(con, "price_list", f"""
        INSERT INTO price_list (id, name, category, cost_price, sale_price, notes, active, linked_item_id, can_discount)
        SELECT 'PL' || g,
               'Price Item ' || g,
               (ARRAY['Service','Medicine','Retail'])[1 + (g % 3)],
               (5000 + (g*137 % 40000))::float,
               (10000 + (g*271 % 80000))::float,
               NULL, 1, NULL, CASE WHEN g % 10 = 0 THEN 0 ELSE 1 END
        FROM generate_series(1,{n['price_list']}) g
    """)

    # --------------------------------------------------------- inventory_list
    run(con, "inventory_list", f"""
        INSERT INTO inventory_list (id, name, category, unit, track_expiry, cost_price, distributor_id, active, barcode, notes)
        SELECT 'IN' || g,
               'Inventory Item ' || g,
               (ARRAY['Medical','Retail'])[1 + (g % 2)],
               'unit', 1, (2000 + (g*91 % 20000))::float,
               did[1 + floor(random()*array_length(did,1))::int],
               1, 'BC' || lpad(g::text,8,'0'), NULL
        FROM generate_series(1,{n['inventory_list']}) g,
             (SELECT array_agg(id) AS did FROM distributors) d
    """)

    # ------------------------------------------------------------------ visits
    run(con, "visits", f"""
        INSERT INTO visits (id, patient_id, visit_type, date, doctor, weight_kg, bcs, complaint, history, exam,
                             treatment, case_status, case_status_changed_at, updates_log, followup_needed,
                             followup_method, followup_reason, followup_date, followup_status,
                             wellness_needed, wellness_type, wellness_next_dose_date, wellness_contacted,
                             wellness_contact_method, grooming_needed, grooming_services, grooming_notes,
                             grooming_admitted_items, grooming_status, grooming_contacted,
                             payment_status, created_by)
        SELECT 'V' || lpad(g::text,7,'0'),
               pid[1 + floor(random()*array_length(pid,1))::int],
               CASE WHEN random() < 0.9 THEN 'Outpatient' ELSE 'Inpatient' END,
               (current_date - (floor(random()*{HISTORY_MONTHS}*30))::int),
               vname[1 + floor(random()*array_length(vname,1))::int],
               round((1 + random()*40)::numeric, 1)::float,
               1 + floor(random()*9)::int,
               'Routine check', NULL, NULL, NULL,
               (ARRAY['Resolved','Ongoing','Needs Filling','Lost to Follow Up','Referred','Deceased/Euthanized'])[1 + floor(random()*6)::int],
               NULL, NULL,
               (ARRAY['Y','N'])[1 + floor(random()*2)::int], 'Phone Call', NULL, NULL, 'Pending',
               'N', NULL, NULL, 'N', NULL,
               'N', NULL, NULL, NULL, NULL, 'N',
               NULL, uid[1 + floor(random()*array_length(uid,1))::int]
        FROM generate_series(1,{n['visits']}) g,
             (SELECT array_agg(id) AS pid FROM patients) p,
             (SELECT array_agg(full_name) AS vname FROM users WHERE role_id=(SELECT id FROM roles WHERE name='Vet')) v,
             (SELECT array_agg(id) AS uid FROM users) u
    """)
    run(con, "visits date index refresh", "ANALYZE visits")

    # ---------------------------------------------------------------- billing
    # billing.visit_id is the PK, so pick a distinct subset of visit ids.
    run(con, "billing", f"""
        INSERT INTO billing (visit_id, billing_type, manual_amount, date_billed,
                              discount_percent, discount_applied_by, notes)
        SELECT v.id,
               CASE WHEN random() < 0.85 THEN 'Automatic' ELSE 'Manual' END,
               round((10000 + random()*150000)::numeric, 0)::float,
               v.date,
               CASE WHEN random() < 0.2 THEN round((random()*20)::numeric,0)::float ELSE 0 END,
               NULL, NULL
        FROM (SELECT id, date FROM visits ORDER BY random() LIMIT {n['billing']}) v
    """)

    # Automatic bills get 1-3 line items each, snapshotted from a random
    # Price List row at "billing time" — mirrors what visit_billing_save()
    # actually writes via logic.save_visit_billing_lines().
    run(con, "visit_billing_lines", """
        INSERT INTO visit_billing_lines (visit_id, price_id, name, category, quantity, unit_price, unit_cost, created_at)
        SELECT x.visit_id, pl.id, pl.name, pl.category, x.quantity, pl.sale_price, pl.cost_price, now()::text
        FROM (
            SELECT b.visit_id, plid[1 + floor(random()*array_length(plid,1))::int] AS price_id,
                   1 + floor(random()*3)::int AS quantity
            FROM billing b, generate_series(1, 1 + floor(random()*3)::int),
                 (SELECT array_agg(id) AS plid FROM price_list) p
            WHERE b.billing_type = 'Automatic'
        ) x
        JOIN price_list pl ON pl.id = x.price_id
    """)

    # billing.total is normally kept in sync by logic.refresh_visit_billing_total()
    # every time a real save happens — backfilled here in one pass since this
    # script writes rows directly, bypassing the app. Two separate set-based
    # UPDATEs (not one UPDATE with a per-row correlated subquery) — at
    # 100K+ billing rows, a correlated subquery re-scans visit_billing_lines
    # once per row and can take many minutes; joining a single pre-aggregated
    # subtotal-per-visit CTE is a single pass over each table.
    run(con, "billing total backfill (manual)", """
        UPDATE billing b
        SET total = round((COALESCE(b.manual_amount, 0) * (1 - COALESCE(b.discount_percent, 0) / 100.0))::numeric, 2)
        WHERE b.billing_type = 'Manual'
    """)
    run(con, "billing total backfill (automatic)", """
        UPDATE billing b
        SET total = round((vbl_sum.subtotal * (1 - COALESCE(b.discount_percent, 0) / 100.0))::numeric, 2)
        FROM (SELECT visit_id, SUM(unit_price * quantity) AS subtotal FROM visit_billing_lines GROUP BY visit_id) vbl_sum
        WHERE b.billing_type != 'Manual' AND vbl_sum.visit_id = b.visit_id
    """)

    # ------------------------------------------------------------ inpatient_cases
    run(con, "inpatient_cases", f"""
        INSERT INTO inpatient_cases (id, patient_id, visit_id, complaint, exam_findings, weight_kg, bcs,
                                      admission_date, admitted_items, dismissed, dismissal_date,
                                      attending_vet_id, supervising_vet_id, discount_percent,
                                      discount_applied_by, created_by)
        SELECT g,
               pid[1 + floor(random()*array_length(pid,1))::int],
               NULL, 'Admission', NULL,
               round((1 + random()*40)::numeric,1)::float, 1 + floor(random()*9)::int,
               (current_date - (floor(random()*{HISTORY_MONTHS}*30))::int),
               NULL,
               CASE WHEN random() < 0.85 THEN 1 ELSE 0 END,
               NULL,
               vid[1 + floor(random()*array_length(vid,1))::int],
               vid[1 + floor(random()*array_length(vid,1))::int],
               0, NULL,
               uid[1 + floor(random()*array_length(uid,1))::int]
        FROM generate_series(1,{n['inpatient_cases']}) g,
             (SELECT array_agg(id) AS pid FROM patients) p,
             (SELECT array_agg(id) AS vid FROM users WHERE role_id=(SELECT id FROM roles WHERE name='Vet')) v,
             (SELECT array_agg(id) AS uid FROM users) u
    """)
    # backfill a plausible dismissal_date (admission + 1..10 days) for dismissed cases
    run(con, "inpatient_cases dismissal backfill", """
        UPDATE inpatient_cases
        SET dismissal_date = admission_date + (1 + floor(random()*10))::int
        WHERE dismissed = 1
    """)

    # -------------------------------------------------------- inpatient_updates
    run(con, "inpatient_updates", f"""
        INSERT INTO inpatient_updates (case_id, timestamp, note, user_id)
        SELECT cid[1 + floor(random()*array_length(cid,1))::int],
               now()::text, 'Update note', uid[1 + floor(random()*array_length(uid,1))::int]
        FROM generate_series(1,{n['inpatient_updates']}) g,
             (SELECT array_agg(id) AS cid FROM inpatient_cases) c,
             (SELECT array_agg(id) AS uid FROM users) u
    """)

    # ----------------------------------------------------- inpatient_contact_log
    run(con, "inpatient_contact_log", f"""
        INSERT INTO inpatient_contact_log (case_id, timestamp, picked_up, staff_user_id, notes)
        SELECT cid[1 + floor(random()*array_length(cid,1))::int],
               now()::text, (random()<0.5)::int, uid[1 + floor(random()*array_length(uid,1))::int], NULL
        FROM generate_series(1,{n['inpatient_contact_log']}) g,
             (SELECT array_agg(id) AS cid FROM inpatient_cases) c,
             (SELECT array_agg(id) AS uid FROM users) u
    """)

    # --------------------------------------------------------- inpatient_billing
    run(con, "inpatient_billing", f"""
        INSERT INTO inpatient_billing (case_id, price_id, quantity, unit_price, unit_cost, logged_by, timestamp)
        SELECT x.case_id, x.price_id, x.quantity, pl.sale_price, pl.cost_price, x.logged_by, x.timestamp
        FROM (
            SELECT cid[1 + floor(random()*array_length(cid,1))::int] AS case_id,
                   plid[1 + floor(random()*array_length(plid,1))::int] AS price_id,
                   1 + floor(random()*3)::int AS quantity,
                   uid[1 + floor(random()*array_length(uid,1))::int] AS logged_by,
                   (current_date - (floor(random()*{HISTORY_MONTHS}*30))::int)::text AS timestamp
            FROM generate_series(1,{n['inpatient_billing']}) g,
                 (SELECT array_agg(id) AS cid FROM inpatient_cases) c,
                 (SELECT array_agg(id) AS plid FROM price_list) pl,
                 (SELECT array_agg(id) AS uid FROM users) u
        ) x
        JOIN price_list pl ON pl.id = x.price_id
    """)

    # inpatient_cases.total is normally kept in sync by
    # logic.refresh_inpatient_total() every time a real save happens —
    # backfilled here in one pass since this script writes rows directly.
    run(con, "inpatient_cases total backfill", """
        UPDATE inpatient_cases ic
        SET total = round((ib_sum.subtotal * (1 - COALESCE(ic.discount_percent, 0) / 100.0))::numeric, 2)
        FROM (SELECT case_id, SUM(unit_price * quantity) AS subtotal FROM inpatient_billing GROUP BY case_id) ib_sum
        WHERE ib_sum.case_id = ic.id
    """)

    # -------------------------------------------------------------- boarding
    run(con, "boarding_sessions", f"""
        INSERT INTO boarding_sessions (id, patient_id, entry_date, dismissal_date, admitted_items,
                                        special_needs, special_needs_notes, room, price_per_day, total,
                                        dismissed, created_by)
        SELECT g,
               pid[1 + floor(random()*array_length(pid,1))::int],
               (current_date - (floor(random()*{HISTORY_MONTHS}*30))::int),
               NULL, NULL,
               (random()<0.1)::int, NULL,
               'Room ' || (1 + g % 12), 15000,
               15000 * (1 + floor(random()*10)),
               (random()<0.85)::int,
               uid[1 + floor(random()*array_length(uid,1))::int]
        FROM generate_series(1,{n['boarding_sessions']}) g,
             (SELECT array_agg(id) AS pid FROM patients) p,
             (SELECT array_agg(id) AS uid FROM users) u
    """)
    run(con, "boarding dismissal backfill", """
        UPDATE boarding_sessions
        SET dismissal_date = entry_date + (1 + floor(random()*10))::int
        WHERE dismissed = 1
    """)
    run(con, "boarding_incidents", f"""
        INSERT INTO boarding_incidents (boarding_id, timestamp, issue, contacted, contact_method, response, user_id)
        SELECT bid[1 + floor(random()*array_length(bid,1))::int],
               now()::text, 'Incident note', 'N', NULL, NULL,
               uid[1 + floor(random()*array_length(uid,1))::int]
        FROM generate_series(1,{n['boarding_incidents']}) g,
             (SELECT array_agg(id) AS bid FROM boarding_sessions) b,
             (SELECT array_agg(id) AS uid FROM users) u
    """)

    # ---------------------------------------------------------------- sales / POS
    run(con, "sales", f"""
        INSERT INTO sales (id, sale_date, cashier_id, subtotal, discount_percent, discount_applied_by, total, payment_method)
        SELECT g,
               (current_date - (floor(random()*{HISTORY_MONTHS}*30))::int)::text,
               uid[1 + floor(random()*array_length(uid,1))::int],
               0, CASE WHEN random()<0.15 THEN round((random()*15)::numeric,0)::float ELSE 0 END, NULL, 0,
               (ARRAY['Cash','Card'])[1 + (random()<0.85)::int]
        FROM generate_series(1,{n['sales']}) g,
             (SELECT array_agg(id) AS uid FROM users) u
    """)
    run(con, "sale_items", f"""
        INSERT INTO sale_items (sale_id, item_id, quantity, unit_price, line_total, unit_cost)
        SELECT x.sale_id, x.item_id, x.qty, x.price, x.qty*x.price, il.cost_price
        FROM (
            SELECT sid[1 + floor(random()*array_length(sid,1))::int] AS sale_id,
                   iid[1 + floor(random()*array_length(iid,1))::int] AS item_id,
                   (1+floor(random()*3))::float AS qty,
                   round((5000+random()*30000)::numeric,0)::float AS price
            FROM generate_series(1,{n['sale_items']}) g,
                 (SELECT array_agg(id) AS sid FROM sales) s,
                 (SELECT array_agg(id) AS iid FROM inventory_list) i
        ) x
        JOIN inventory_list il ON il.id = x.item_id
    """)
    run(con, "sales totals backfill", """
        UPDATE sales s SET subtotal = t.sub, total = round((t.sub * (1 - s.discount_percent/100.0))::numeric, 0)::float
        FROM (SELECT sale_id, SUM(line_total) AS sub FROM sale_items GROUP BY sale_id) t
        WHERE t.sale_id = s.id
    """)

    # --------------------------------------------------------- inventory_transactions
    run(con, "inventory_transactions", f"""
        INSERT INTO inventory_transactions (item_id, change_qty, reason, ref_id, timestamp, user_id)
        SELECT iid[1 + floor(random()*array_length(iid,1))::int],
               (CASE WHEN random()<0.5 THEN -1 ELSE 1 END) * (1+floor(random()*5)),
               (ARRAY['sale','manual_adjustment'])[1 + (random()<0.9)::int],
               NULL, now()::text, uid[1 + floor(random()*array_length(uid,1))::int]
        FROM generate_series(1,{n['inventory_transactions']}) g,
             (SELECT array_agg(id) AS iid FROM inventory_list) i,
             (SELECT array_agg(id) AS uid FROM users) u
    """)

    # -------------------------------------------------------------------- refunds
    run(con, "refunds", f"""
        INSERT INTO refunds (refund_type, refund_date, amount, restocked, visit_id, inpatient_case_id, reason, processed_by, created_at)
        SELECT rtype,
               (current_date - (floor(random()*{HISTORY_MONTHS}*30))::int),
               round((5000+random()*50000)::numeric,0)::float,
               (rtype='retail' AND random()<0.5)::int,
               CASE WHEN rtype='service' AND random()<0.7 THEN vid[1 + floor(random()*array_length(vid,1))::int] ELSE NULL END,
               NULL, NULL,
               uid[1 + floor(random()*array_length(uid,1))::int], now()::text
        FROM generate_series(1,{n['refunds']}) g,
             (SELECT array_agg(id) AS vid FROM visits) v,
             (SELECT array_agg(id) AS uid FROM users) u,
             LATERAL (SELECT (ARRAY['retail','service'])[1 + (random()<0.5)::int] AS rtype) r
    """)
    run(con, "refund_items", f"""
        INSERT INTO refund_items (refund_id, item_id, quantity, unit_price, line_total)
        SELECT rid[1 + floor(random()*array_length(rid,1))::int],
               iid[1 + floor(random()*array_length(iid,1))::int],
               qty, price, qty*price
        FROM generate_series(1,{n['refund_items']}) g,
             (SELECT array_agg(id) AS rid FROM refunds WHERE refund_type='retail') r,
             (SELECT array_agg(id) AS iid FROM inventory_list) i,
             LATERAL (SELECT (1+floor(random()*2))::float AS qty, round((5000+random()*20000)::numeric,0)::float AS price) x
    """)

    # -------------------------------------------------------------- audit sessions
    run(con, "audit_sessions", f"""
        INSERT INTO audit_sessions (audit_date, performed_by, status, created_at, confirmed_at)
        SELECT (current_date - (floor(random()*{HISTORY_MONTHS}*30))::int),
               uid[1 + floor(random()*array_length(uid,1))::int],
               (ARRAY['Draft','Confirmed'])[1 + (random()<0.8)::int],
               now()::text, now()::text
        FROM generate_series(1,{n['audit_sessions']}) g,
             (SELECT array_agg(id) AS uid FROM users) u
    """)
    run(con, "audit_session_lines", f"""
        INSERT INTO audit_session_lines (session_id, item_id, stock_counted, received_since_prior,
                                          reorder_threshold, critical_item, target_coverage_days, nearest_expiry_date, notes)
        SELECT sid[1 + floor(random()*array_length(sid,1))::int],
               iid[1 + floor(random()*array_length(iid,1))::int],
               floor(random()*100), floor(random()*20),
               10, (random()<0.2)::int, 14, NULL, NULL
        FROM generate_series(1,{n['audit_session_lines']}) g,
             (SELECT array_agg(id) AS sid FROM audit_sessions) s,
             (SELECT array_agg(id) AS iid FROM inventory_list) i
    """)

    # -------------------------------------------------------------------- opex
    run(con, "monthly_opex", f"""
        INSERT INTO monthly_opex (month, rent, salaries, utilities, marketing, other)
        SELECT to_char(date_trunc('month', current_date) - (g || ' months')::interval, 'YYYY-MM'),
               500000, 2000000, 150000, 100000, 50000
        FROM generate_series(0,{n['monthly_opex']-1}) g
    """)

    # -------------------------------------------------------------------- payments
    # Weighted across visit / inpatient_case / boarding linkage, matching how the app records them.
    run(con, "payments", f"""
        INSERT INTO payments (visit_id, inpatient_case_id, boarding_id, amount, method, date, user_id, notes)
        SELECT
            CASE WHEN kind < 0.7 THEN bvid[1 + floor(random()*array_length(bvid,1))::int] ELSE NULL END,
            CASE WHEN kind >= 0.7 AND kind < 0.9 THEN cid[1 + floor(random()*array_length(cid,1))::int] ELSE NULL END,
            CASE WHEN kind >= 0.9 THEN bid[1 + floor(random()*array_length(bid,1))::int] ELSE NULL END,
            round((5000+random()*100000)::numeric,0)::float,
            (ARRAY['Cash','Card'])[1 + (random()<0.85)::int],
            (current_date - (floor(random()*{HISTORY_MONTHS}*30))::int),
            uid[1 + floor(random()*array_length(uid,1))::int], NULL
        FROM generate_series(1,{n['payments']}) g,
             (SELECT array_agg(visit_id) AS bvid FROM billing) bv,
             (SELECT array_agg(id) AS cid FROM inpatient_cases) c,
             (SELECT array_agg(id) AS bid FROM boarding_sessions) b,
             (SELECT array_agg(id) AS uid FROM users) u,
             LATERAL (SELECT random() AS kind) k
    """)

    # ---------------------------------------------------------------- appointments
    run(con, "appointments", f"""
        INSERT INTO appointments (appt_date, slot_label, resource_type, resource_id, pet_name, owner_name,
                                   appointment_type, reason, created_by, created_at)
        SELECT (current_date - {HISTORY_MONTHS*30} + floor(random()*({HISTORY_MONTHS*30}+21))::int),
               (ARRAY['09:00','09:30','10:00','10:30','11:00','14:00','14:30','15:00'])[1 + floor(random()*8)::int],
               rtype,
               CASE WHEN rtype='vet' THEN vid[1 + floor(random()*array_length(vid,1))::int] ELSE NULL END,
               'Pet ' || g, 'Owner ' || g,
               CASE WHEN rtype='vet' THEN 'Medical' ELSE 'Grooming' END,
               NULL, uid[1 + floor(random()*array_length(uid,1))::int], now()::text
        FROM generate_series(1,{n['appointments']}) g,
             (SELECT array_agg(id) AS vid FROM users WHERE role_id=(SELECT id FROM roles WHERE name='Vet')) v,
             (SELECT array_agg(id) AS uid FROM users) u,
             LATERAL (SELECT (ARRAY['vet','grooming'])[1 + (random()<0.75)::int] AS rtype) r
    """)

    # --------------------------------------------------------------------- logs
    run(con, "login_log", f"""
        INSERT INTO login_log (user_id, username, success, timestamp, ip, user_agent)
        SELECT uid[1 + floor(random()*array_length(uid,1))::int], 'user', (random()<0.95)::int,
               now()::text, '127.0.0.1', 'test'
        FROM generate_series(1,{n['login_log']}) g,
             (SELECT array_agg(id) AS uid FROM users) u
    """)
    run(con, "audit_log", f"""
        INSERT INTO audit_log (user_id, username, timestamp, action, table_name, record_id, field, old_value, new_value)
        SELECT uid[1 + floor(random()*array_length(uid,1))::int], 'user', now()::text,
               (ARRAY['create','update','delete'])[1 + floor(random()*3)::int],
               'visits', g::text, NULL, NULL, NULL
        FROM generate_series(1,{n['audit_log']}) g,
             (SELECT array_agg(id) AS uid FROM users) u
    """)
    run(con, "backup_log", f"""
        INSERT INTO backup_log (started_at, finished_at, status, filepath, filesize_bytes, error)
        SELECT now()::text, now()::text,
               (ARRAY['success','success','success','failed','running'])[1 + floor(random()*5)::int],
               '/backups/f' || g, 1000000, NULL
        FROM generate_series(1,{n['backup_log']}) g
    """)

    con.commit()
    print("Committed. Row counts:")
    for t in ROW_COUNTS:
        c = con.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
        print(f"  {t}: {c}")
    con.close()


if __name__ == "__main__":
    main()
