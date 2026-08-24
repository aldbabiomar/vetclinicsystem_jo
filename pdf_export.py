"""
PDF exports — patient clinical file, patient billing history, POS sale
receipts, visit exports, and inpatient exports.
Built with reportlab (pure Python, no external binary dependency).
"""
import io
from xml.sax.saxutils import escape as _xml_escape
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
)

from decimal import Decimal
import logic
import attachments

PRIMARY = colors.black
LINE = colors.HexColor("#BFBFBF")
ACCENT_TINT = colors.HexColor("#F2F2F2")
MUTED_TEXT = colors.HexColor("#595959")


def X(v):
    """
    Escapes a value for safe interpolation into a reportlab Paragraph.
    Paragraph text is parsed as a small XML/HTML-like markup language, so
    any free-text field from the database (clinical notes, names,
    addresses, ...) that happens to contain '<', '>', or '&' would
    otherwise break the parser and crash the whole export. Every place
    that interpolates a database/user-supplied value into a Paragraph
    string should route it through this first. Static markup written by
    this file itself (e.g. "<b>...</b>") is intentionally NOT passed
    through this — only the variable content.
    """
    if v is None:
        return ""
    return _xml_escape(str(v))


def _styles():
    ss = getSampleStyleSheet()
    ss.add(ParagraphStyle(name="H1", parent=ss["Heading1"], textColor=PRIMARY, fontSize=18, spaceAfter=4))
    ss.add(ParagraphStyle(name="H2", parent=ss["Heading2"], textColor=PRIMARY, fontSize=12, spaceBefore=14, spaceAfter=6))
    ss.add(ParagraphStyle(name="Small", parent=ss["Normal"], fontSize=9, textColor=MUTED_TEXT))
    ss.add(ParagraphStyle(name="Body", parent=ss["Normal"], fontSize=10, leading=14))
    return ss


def _attachments_note(ss, files):
    """
    A short, honest note that attachments exist on the system but are not
    included in this PDF — with filenames and dates so whoever's printing
    this knows exactly what else to go pull up and print separately.
    """
    if not files:
        return []
    flow = [Paragraph("Attachments on file (not included in this PDF)", ss["H2"])]
    for f in files:
        flow.append(Paragraph(f"\u2022 {X(f['original_name'])} — uploaded {X(f['uploaded_at'][:10])}", ss["Body"]))
    flow.append(Paragraph(
        "These files are stored in the system but are not embedded in this export. "
        "Open the record on-screen to view them, and print them separately if needed.",
        ss["Small"]))
    flow.append(Spacer(1, 8))
    return flow


def _section_table(rows, col_widths):
    t = Table(rows, colWidths=col_widths)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), PRIMARY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, LINE),
        ("ALIGN", (-1, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, ACCENT_TINT]),
    ]))
    return t


def export_patient_file(db, patient_id):
    """Date / visit details / visit procedures / inpatient details / inpatient procedures."""
    ss = _styles()
    patient = db.execute(
        "SELECT p.*, o.name as owner_name, o.phone as owner_phone FROM patients p "
        "JOIN owners o ON o.id=p.owner_id WHERE p.id=?", (patient_id,)
    ).fetchone()
    visits = db.execute("SELECT * FROM visits WHERE patient_id=? ORDER BY date", (patient_id,)).fetchall()
    cases = db.execute("SELECT * FROM inpatient_cases WHERE patient_id=? ORDER BY admission_date", (patient_id,)).fetchall()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=18 * mm, bottomMargin=18 * mm,
                             leftMargin=18 * mm, rightMargin=18 * mm)
    story = [
        Paragraph(f"Patient file — {X(patient['animal_name'])}", ss["H1"]),
        Paragraph(f"{X(patient_id)} \u00b7 {X(patient['species'] or '')} \u00b7 Owner: {X(patient['owner_name'])}"
                  f"{' (' + X(patient['owner_phone']) + ')' if patient['owner_phone'] else ''}", ss["Small"]),
        Spacer(1, 10),
    ]

    story.append(Paragraph("Outpatient / visit history", ss["H2"]))
    if visits:
        for v in visits:
            summary = logic.visit_billing_summary(db, v["id"])
            proc_names = ", ".join(l["name"] for l in summary["lines"]) or "\u2014"
            story.append(Paragraph(f"<b>{X(v['date'] or '')}</b> — {X(v['visit_type'] or '')} \u00b7 Dr. {X(v['doctor'] or '\u2014')} \u00b7 Status: {X(v['case_status'] or '\u2014')}", ss["Body"]))
            story.append(Paragraph(f"Complaint: {X(v['complaint'] or '\u2014')}", ss["Body"]))
            if v["exam"]:
                story.append(Paragraph(f"Exam/diagnostics: {X(v['exam'])}", ss["Body"]))
            if v["treatment"]:
                story.append(Paragraph(f"Treatment: {X(v['treatment'])}", ss["Body"]))
            story.append(Paragraph(f"Procedures billed: {X(proc_names)}", ss["Body"]))
            story.append(Spacer(1, 8))
    else:
        story.append(Paragraph("No outpatient visits on file.", ss["Small"]))

    story.append(Paragraph("Inpatient stays", ss["H2"]))
    if cases:
        for c in cases:
            bsum = logic.inpatient_billing_summary(db, c["id"])
            proc_names = ", ".join(l["name"] for l in bsum["lines"]) or "\u2014"
            story.append(Paragraph(
                f"<b>Admitted {X(c['admission_date'])}</b>"
                f"{' — Discharged ' + X(c['dismissal_date']) if c['dismissal_date'] else ' — Currently admitted'}",
                ss["Body"]))
            story.append(Paragraph(f"Complaint: {X(c['complaint'] or '\u2014')}", ss["Body"]))
            if c["exam_findings"]:
                story.append(Paragraph(f"Exam findings: {X(c['exam_findings'])}", ss["Body"]))
            story.append(Paragraph(f"Procedures billed: {X(proc_names)}", ss["Body"]))
            story.append(Spacer(1, 8))
    else:
        story.append(Paragraph("No inpatient stays on file.", ss["Small"]))

    all_files = []
    for v in visits:
        all_files.extend(attachments.list_attachments(db, "visit", v["id"]))
    for c in cases:
        all_files.extend(attachments.list_attachments(db, "inpatient", c["id"]))
    story.extend(_attachments_note(ss, all_files))

    doc.build(story)
    buf.seek(0)
    return buf


def export_patient_billing(db, patient_id):
    """Date / services individually / prices individually / total / status."""
    ss = _styles()
    patient = db.execute(
        "SELECT p.*, o.name as owner_name FROM patients p JOIN owners o ON o.id=p.owner_id WHERE p.id=?",
        (patient_id,),
    ).fetchone()
    visits = db.execute("SELECT * FROM visits WHERE patient_id=? ORDER BY date", (patient_id,)).fetchall()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=18 * mm, bottomMargin=18 * mm,
                             leftMargin=18 * mm, rightMargin=18 * mm)
    story = [
        Paragraph(f"Billing history — {X(patient['animal_name'])}", ss["H1"]),
        Paragraph(f"{X(patient_id)} \u00b7 Owner: {X(patient['owner_name'])}", ss["Small"]),
        Spacer(1, 10),
    ]

    grand_total = 0
    for v in visits:
        summary = logic.visit_billing_summary(db, v["id"])
        if not summary["lines"]:
            continue
        story.append(Paragraph(f"<b>{X(v['date'] or '')}</b> \u2014 {X(v['id'])}", ss["H2"]))
        data = [["Service", "Price (JOD)"]]
        for l in summary["lines"]:
            amount = l.get("line_total", l["price"])
            label = l["name"] if not l.get("quantity") or l["quantity"] == 1 else f"{l['name']} × {l['quantity']:g}"
            data.append([label, f"{amount:.3f}"])
        pre_cleanup_total = round(summary["subtotal"] * (1 - summary["discount_percent"] / Decimal(100)), 3)
        data.append(["Subtotal", f"{summary['subtotal']:.3f}"])
        if summary["discount_percent"]:
            data.append([f"Discount ({summary['discount_percent']:.0f}%)", f"-{summary['subtotal'] - pre_cleanup_total:.3f}"])
        if summary["cleanup_amount"]:
            data.append(["Clean Up", f"-{summary['cleanup_amount']:.3f}"])
        data.append(["Total", f"{summary['total']:.3f}"])
        data.append(["Paid", f"{summary['paid']:.3f}"])
        data.append(["Status", summary["status"]])

        t = Table(data, colWidths=[110 * mm, 40 * mm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), PRIMARY),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.5, LINE),
            ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ("FONTNAME", (0, -4), (-1, -1), "Helvetica-Bold"),
        ]))
        story.append(t)
        story.append(Spacer(1, 12))
        grand_total += summary["total"]

    story.append(Paragraph(f"<b>Grand total across all visits: {grand_total:.3f} JOD</b>", ss["Body"]))
    doc.build(story)
    buf.seek(0)
    return buf


def export_sale_receipt(db, sale_id):
    """One POS sale — line items, prices, quantities, total, cashier, and timestamp."""
    ss = _styles()
    sale = db.execute(
        "SELECT s.*, u.full_name AS cashier_name FROM sales s LEFT JOIN users u ON u.id=s.cashier_id WHERE s.id=?",
        (sale_id,),
    ).fetchone()
    lines = db.execute(
        "SELECT si.*, il.name FROM sale_items si JOIN inventory_list il ON il.id=si.item_id WHERE si.sale_id=?",
        (sale_id,),
    ).fetchall()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=18 * mm, bottomMargin=18 * mm,
                             leftMargin=18 * mm, rightMargin=18 * mm)
    story = [
        Paragraph("Sale receipt", ss["H1"]),
        Paragraph(f"Sale #{X(sale_id)} \u00b7 {X(sale['sale_date'])} \u00b7 Sold by {X(sale['cashier_name'] or '\u2014')}", ss["Small"]),
        Spacer(1, 12),
    ]

    data = [["Item", "Unit Price (JOD)", "Qty", "Line Total (JOD)"]]
    for l in lines:
        data.append([l["name"], f"{l['unit_price']:,.3f}", f"{l['quantity']:g}", f"{l['line_total']:,.3f}"])
    t = _section_table(data, [70 * mm, 35 * mm, 20 * mm, 40 * mm])
    story.append(t)
    story.append(Spacer(1, 14))

    pre_cleanup_total = round(sale["subtotal"] * (1 - sale["discount_percent"] / Decimal(100)), 3)
    summary_rows = [["Subtotal", f"{sale['subtotal']:,.3f} JOD"]]
    if sale["discount_percent"]:
        summary_rows.append([f"Discount ({sale['discount_percent']:.0f}%)", f"-{sale['subtotal'] - pre_cleanup_total:,.3f} JOD"])
    if sale["cleanup_amount"]:
        summary_rows.append(["Clean Up", f"-{sale['cleanup_amount']:,.3f} JOD"])
    summary_rows.append(["Total", f"{sale['total']:,.3f} JOD"])
    summary_rows.append(["Payment Method", sale["payment_method"] or "\u2014"])
    st = Table(summary_rows, colWidths=[110 * mm, 55 * mm])
    st.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("FONTNAME", (0, -2), (-1, -2), "Helvetica-Bold"),
        ("LINEABOVE", (0, -2), (-1, -2), 0.75, PRIMARY),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(st)

    doc.build(story)
    buf.seek(0)
    return buf


def export_visit_pdf(db, visit_id):
    """Full visit export, styled to match the Patient File export: a compact
    header line followed by flowing paragraphs rather than boxed tables for
    patient/owner/visit details — only the billing breakdown stays tabular,
    since that's genuinely tabular data the Patient File export doesn't need
    to show in this level of detail."""
    ss = _styles()
    v = db.execute(
        "SELECT vi.*, p.animal_name, p.species, p.sex, p.age_note, o.name AS owner_name, o.phone AS owner_phone, "
        "o.address AS owner_address FROM visits vi JOIN patients p ON p.id=vi.patient_id "
        "JOIN owners o ON o.id=p.owner_id WHERE vi.id=?",
        (visit_id,),
    ).fetchone()
    summary = logic.visit_billing_summary(db, visit_id)
    files = attachments.list_attachments(db, "visit", visit_id)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=18 * mm, bottomMargin=18 * mm,
                             leftMargin=18 * mm, rightMargin=18 * mm)
    story = [
        Paragraph(f"Visit record \u2014 {X(v['animal_name'])}", ss["H1"]),
        Paragraph(f"{X(visit_id)} \u00b7 {X(v['species'] or '')} \u00b7 Owner: {X(v['owner_name'])}"
                  f"{' (' + X(v['owner_phone']) + ')' if v['owner_phone'] else ''}"
                  f"{' \u00b7 ' + X(v['owner_address']) if v['owner_address'] else ''}", ss["Small"]),
        Spacer(1, 10),
    ]

    story.append(Paragraph("Visit Details", ss["H2"]))
    story.append(Paragraph(
        f"<b>{X(v['date'] or '')}</b> \u2014 {X(v['visit_type'] or '')} \u00b7 Dr. {X(v['doctor'] or '\u2014')} "
        f"\u00b7 Status: {X(v['case_status'] or '\u2014')}", ss["Body"]))
    if v["weight_kg"] is not None or v["bcs"] is not None:
        bits = []
        if v["weight_kg"] is not None:
            bits.append(f"Weight: {v['weight_kg']:g} kg")
        if v["bcs"] is not None:
            bits.append(f"BCS: {v['bcs']}/9")
        story.append(Paragraph(" \u00b7 ".join(bits), ss["Body"]))
    story.append(Paragraph(f"Complaint: {X(v['complaint'] or '\u2014')}", ss["Body"]))
    if v["history"]:
        story.append(Paragraph(f"History: {X(v['history'])}", ss["Body"]))
    if v["exam"]:
        story.append(Paragraph(f"Exam/diagnostics: {X(v['exam'])}", ss["Body"]))
    if v["treatment"]:
        story.append(Paragraph(f"Treatment: {X(v['treatment'])}", ss["Body"]))
    story.append(Spacer(1, 8))

    if v["followup_needed"] == "Y":
        story.append(Paragraph("Follow-Up", ss["H2"]))
        story.append(Paragraph(
            f"{X(v['followup_method'] or '\u2014')} \u2014 {X(v['followup_reason'] or '')} "
            f"(due {X(v['followup_date'] or '\u2014')}, status: {X(v['followup_status'] or '\u2014')})", ss["Body"]))

    if v["wellness_needed"] == "Y":
        story.append(Paragraph("Wellness", ss["H2"]))
        story.append(Paragraph(
            f"{X(v['wellness_type'] or '\u2014')} \u2014 next dose {X(v['wellness_next_dose_date'] or '\u2014')}", ss["Body"]))

    if v["grooming_needed"] == "Y":
        story.append(Paragraph("Grooming", ss["H2"]))
        story.append(Paragraph(f"{X(v['grooming_services'] or '\u2014')} \u2014 status: {X(v['grooming_status'] or '\u2014')}", ss["Body"]))

    story.append(Paragraph("Billing", ss["H2"]))
    if summary["lines"]:
        data = [["Item", "Price (JOD)"]]
        for l in summary["lines"]:
            amount = l.get("line_total", l["price"])
            label = l["name"] if not l.get("quantity") or l["quantity"] == 1 else f"{l['name']} × {l['quantity']:g}"
            data.append([label, f"{amount:,.3f}"])
        story.append(_section_table(data, [120 * mm, 45 * mm]))
        story.append(Spacer(1, 6))
    pre_cleanup_total = round(summary["subtotal"] * (1 - summary["discount_percent"] / Decimal(100)), 3)
    bill_rows = [["Subtotal", f"{summary['subtotal']:,.3f} JOD"]]
    if summary["discount_percent"]:
        bill_rows.append([f"Discount ({summary['discount_percent']:.0f}%)", f"-{summary['subtotal'] - pre_cleanup_total:,.3f} JOD"])
    if summary["cleanup_amount"]:
        bill_rows.append(["Clean Up", f"-{summary['cleanup_amount']:,.3f} JOD"])
    bill_rows.append(["Total", f"{summary['total']:,.3f} JOD"])
    bill_rows.append(["Paid", f"{summary['paid']:,.3f} JOD"])
    bill_rows.append(["Status", summary["status"]])
    bt = Table(bill_rows, colWidths=[120 * mm, 45 * mm])
    bt.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10), ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("FONTNAME", (0, -3), (-1, -3), "Helvetica-Bold"), ("LINEABOVE", (0, -3), (-1, -3), 0.75, PRIMARY),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(bt)

    story.extend(_attachments_note(ss, files))

    doc.build(story)
    buf.seek(0)
    return buf


def export_inpatient_pdf(db, case_id):
    """Full inpatient export, styled to match Patient File / Visit exports."""
    ss = _styles()
    c = db.execute(
        "SELECT ic.*, p.animal_name, p.species, p.sex, p.age_note, o.name AS owner_name, o.phone AS owner_phone, "
        "o.address AS owner_address, uatt.full_name AS attending_name, usup.full_name AS supervising_name "
        "FROM inpatient_cases ic JOIN patients p ON p.id=ic.patient_id JOIN owners o ON o.id=p.owner_id "
        "LEFT JOIN users uatt ON uatt.id=ic.attending_vet_id LEFT JOIN users usup ON usup.id=ic.supervising_vet_id "
        "WHERE ic.id=?",
        (case_id,),
    ).fetchone()
    updates = db.execute("SELECT iu.*, u.full_name FROM inpatient_updates iu LEFT JOIN users u ON u.id=iu.user_id "
                          "WHERE iu.case_id=? ORDER BY iu.timestamp", (case_id,)).fetchall()
    summary = logic.inpatient_billing_summary(db, case_id)
    files = attachments.list_attachments(db, "inpatient", case_id)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=18 * mm, bottomMargin=18 * mm,
                             leftMargin=18 * mm, rightMargin=18 * mm)
    story = [
        Paragraph(f"Inpatient record \u2014 {X(c['animal_name'])}", ss["H1"]),
        Paragraph(f"Case #{X(case_id)} \u00b7 {X(c['species'] or '')} \u00b7 Owner: {X(c['owner_name'])}"
                  f"{' (' + X(c['owner_phone']) + ')' if c['owner_phone'] else ''}"
                  f"{' \u00b7 ' + X(c['owner_address']) if c['owner_address'] else ''}", ss["Small"]),
        Spacer(1, 10),
    ]

    story.append(Paragraph("Inpatient Stay", ss["H2"]))
    story.append(Paragraph(
        f"<b>Admitted {X(c['admission_date'])}</b>"
        f"{' \u2014 Discharged ' + X(c['dismissal_date']) if c['dismissal_date'] else ' \u2014 Currently admitted'}"
        f" \u00b7 Attending: {X(c['attending_name'] or '\u2014')} \u00b7 Supervising: {X(c['supervising_name'] or '\u2014')}",
        ss["Body"]))
    if c["weight_kg"] is not None or c["bcs"] is not None:
        bits = []
        if c["weight_kg"] is not None:
            bits.append(f"Weight: {c['weight_kg']:g} kg")
        if c["bcs"] is not None:
            bits.append(f"BCS: {c['bcs']}/9")
        story.append(Paragraph(" \u00b7 ".join(bits), ss["Body"]))
    story.append(Spacer(1, 8))

    story.append(Paragraph("Clinical Notes", ss["H2"]))
    story.append(Paragraph(f"<b>Presenting Complaint:</b> {X(c['complaint'] or '\u2014')}", ss["Body"]))
    story.append(Paragraph(f"<b>Exam Findings:</b> {X(c['exam_findings'] or '\u2014')}", ss["Body"]))
    story.append(Paragraph(f"<b>Admitted Items:</b> {X(c['admitted_items'] or '\u2014')}", ss["Body"]))

    story.append(Paragraph("Daily Updates", ss["H2"]))
    if updates:
        for u in updates:
            story.append(Paragraph(f"<b>{X(u['timestamp'])}</b> ({X(u['full_name'] or '\u2014')}): {X(u['note'])}", ss["Body"]))
    else:
        story.append(Paragraph("No daily updates logged.", ss["Small"]))

    story.append(Paragraph("Billing", ss["H2"]))
    if summary["lines"]:
        data = [["Procedure", "Qty", "Unit Price (JOD)", "Line Total (JOD)"]]
        for l in summary["lines"]:
            data.append([l["name"], f"{l['quantity']:g}", f"{l['unit_price']:,.3f}", f"{l['line_total']:,.3f}"])
        story.append(_section_table(data, [70 * mm, 20 * mm, 35 * mm, 40 * mm]))
        story.append(Spacer(1, 6))
    pre_cleanup_total = round(summary["subtotal"] * (1 - summary["discount_percent"] / Decimal(100)), 3)
    bill_rows = [["Subtotal", f"{summary['subtotal']:,.3f} JOD"]]
    if summary["discount_percent"]:
        bill_rows.append([f"Discount ({summary['discount_percent']:.0f}%)", f"-{summary['subtotal'] - pre_cleanup_total:,.3f} JOD"])
    if summary["cleanup_amount"]:
        bill_rows.append(["Clean Up", f"-{summary['cleanup_amount']:,.3f} JOD"])
    bill_rows.append(["Total", f"{summary['total']:,.3f} JOD"])
    bill_rows.append(["Paid", f"{summary['paid']:,.3f} JOD"])
    bill_rows.append(["Status", summary["status"]])
    bt = Table(bill_rows, colWidths=[125 * mm, 40 * mm])
    bt.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10), ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("FONTNAME", (0, -3), (-1, -3), "Helvetica-Bold"), ("LINEABOVE", (0, -3), (-1, -3), 0.75, PRIMARY),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(bt)

    story.extend(_attachments_note(ss, files))

    doc.build(story)
    buf.seek(0)
    return buf


def export_boarding_pdf(db, boarding_id):
    """Full boarding export: patient, owner, stay details, incident log, and billing."""
    ss = _styles()
    b = db.execute(
        "SELECT bs.*, p.animal_name, p.species, p.sex, p.age_note, o.name AS owner_name, "
        "o.phone AS owner_phone, o.address AS owner_address FROM boarding_sessions bs "
        "JOIN patients p ON p.id=bs.patient_id JOIN owners o ON o.id=p.owner_id WHERE bs.id=?",
        (boarding_id,),
    ).fetchone()
    incidents = db.execute(
        "SELECT bi.*, u.full_name FROM boarding_incidents bi LEFT JOIN users u ON u.id=bi.user_id "
        "WHERE bi.boarding_id=? ORDER BY bi.timestamp",
        (boarding_id,),
    ).fetchall()
    summary = logic.boarding_billing_summary(db, boarding_id)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=18 * mm, bottomMargin=18 * mm,
                             leftMargin=18 * mm, rightMargin=18 * mm)
    story = [
        Paragraph(f"Boarding record \u2014 {X(b['animal_name'])}", ss["H1"]),
        Paragraph(f"Booking #{X(boarding_id)} \u00b7 {X(b['species'] or '')} \u00b7 Owner: {X(b['owner_name'])}"
                  f"{' (' + X(b['owner_phone']) + ')' if b['owner_phone'] else ''}"
                  f"{' \u00b7 ' + X(b['owner_address']) if b['owner_address'] else ''}", ss["Small"]),
        Spacer(1, 10),
    ]

    story.append(Paragraph("Boarding Stay", ss["H2"]))
    story.append(Paragraph(
        f"<b>Entry {X(b['entry_date'])}</b>"
        f"{' \u2014 Dismissal ' + X(b['dismissal_date']) if b['dismissal_date'] else (' \u2014 Picked up' if b['dismissed'] else ' \u2014 Currently boarding')}"
        f" \u00b7 Room: {X(b['room'] or '\u2014')}", ss["Body"]))
    story.append(Paragraph(f"Admitted Items: {X(b['admitted_items'] or '\u2014')}", ss["Body"]))
    story.append(Paragraph(
        f"Special Needs: {X((b['special_needs_notes'] or 'Yes, no details given') if b['special_needs'] else 'None reported')}",
        ss["Body"]))
    story.append(Spacer(1, 8))

    story.append(Paragraph("Something's Wrong \u2014 Incident Log", ss["H2"]))
    if incidents:
        for i in incidents:
            contact_bit = f" \u2014 Contacted owner via {X(i['contact_method'])}" if i["contacted"] == "Y" else " \u2014 Owner not contacted"
            story.append(Paragraph(f"<b>{X(i['timestamp'])}</b> ({X(i['full_name'] or '\u2014')}): {X(i['issue'])}{contact_bit}", ss["Body"]))
            if i["response"]:
                story.append(Paragraph(f"Response: {X(i['response'])}", ss["Body"]))
    else:
        story.append(Paragraph("No incidents logged.", ss["Small"]))

    story.append(Paragraph("Billing", ss["H2"]))
    bill_rows = [
        ["Price per Day", f"{b['price_per_day']:,.3f} JOD" if b["price_per_day"] is not None else "\u2014"],
    ]
    pre_cleanup_total = round(summary["subtotal"] * (1 - summary["discount_percent"] / Decimal(100)), 3)
    if summary["discount_percent"] or summary["cleanup_amount"]:
        bill_rows.append(["Subtotal", f"{summary['subtotal']:,.3f} JOD"])
    if summary["discount_percent"]:
        bill_rows.append([f"Discount ({summary['discount_percent']:.0f}%)", f"-{summary['subtotal'] - pre_cleanup_total:,.3f} JOD"])
    if summary["cleanup_amount"]:
        bill_rows.append(["Clean Up", f"-{summary['cleanup_amount']:,.3f} JOD"])
    bill_rows.append(["Total", f"{summary['total']:,.3f} JOD"])
    bill_rows.append(["Paid", f"{summary['paid']:,.3f} JOD"])
    bill_rows.append(["Status", summary["status"]])
    bt2 = Table(bill_rows, colWidths=[125 * mm, 40 * mm])
    bt2.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10), ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("FONTNAME", (0, -3), (-1, -3), "Helvetica-Bold"), ("LINEABOVE", (0, -3), (-1, -3), 0.75, PRIMARY),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(bt2)

    doc.build(story)
    buf.seek(0)
    return buf


def export_consignment_settlement_pdf(db, settlement_id):
    """One consignment settlement — what was owed, what was paid, any
    residual carried forward, and the period it covers. A paper trail a
    distributor can be handed alongside payment."""
    ss = _styles()
    s = db.execute(
        "SELECT cs.*, d.name AS distributor_name, d.contact_person, d.phone, u.full_name AS settled_by_name "
        "FROM consignment_settlements cs JOIN distributors d ON d.id=cs.distributor_id "
        "LEFT JOIN users u ON u.id=cs.settled_by WHERE cs.id=?",
        (settlement_id,),
    ).fetchone()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=18 * mm, bottomMargin=18 * mm,
                             leftMargin=18 * mm, rightMargin=18 * mm)
    residual = round((s["amount_owed"] or 0) - (s["amount_paid"] or 0), 2)
    contact_bits = " · ".join(x for x in [s["contact_person"], s["phone"]] if x)
    story = [
        Paragraph("Consignment settlement", ss["H1"]),
        Paragraph(f"Settlement #{X(settlement_id)} · {X(s['distributor_name'])}"
                  f"{' · ' + X(contact_bits) if contact_bits else ''}", ss["Small"]),
        Paragraph(f"Period: {X(s['period_start'] or 'start')} — {X(s['period_end'])}", ss["Small"]),
        Paragraph(f"Recorded by {X(s['settled_by_name'] or '—')} on {X(s['created_at'])}", ss["Small"]),
        Spacer(1, 14),
    ]

    rows = [
        ["Amount Owed", f"{s['amount_owed']:,.3f} JOD"],
        ["Amount Paid", f"{s['amount_paid']:,.3f} JOD"],
        ["Payment Method", s["payment_method"] or "—"],
    ]
    if residual > 0:
        rows.append(["Carried Forward to Next Settlement", f"{residual:,.3f} JOD"])
    t = Table(rows, colWidths=[110 * mm, 55 * mm])
    t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("FONTNAME", (0, 0), (-1, 1), "Helvetica-Bold"),
        ("LINEBELOW", (0, 1), (-1, 1), 0.75, PRIMARY),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(t)

    if s["notes"]:
        story.append(Spacer(1, 12))
        story.append(Paragraph("Notes", ss["H2"]))
        story.append(Paragraph(X(s["notes"]), ss["Body"]))

    doc.build(story)
    buf.seek(0)
    return buf


def export_distributor_ledger(db, distributor_id):
    """One distributor — every bill, its payments, and running totals."""
    ss = _styles()
    dist = db.execute("SELECT * FROM distributors WHERE id=?", (distributor_id,)).fetchone()
    ledger = logic.distributor_ledger(db, distributor_id)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=18 * mm, bottomMargin=18 * mm,
                             leftMargin=18 * mm, rightMargin=18 * mm)
    story = [
        Paragraph(f"Distributor ledger — {X(dist['name'])}", ss["H1"]),
        Paragraph(f"{X(distributor_id)} · {X(dist['phone'] or '—')}", ss["Small"]),
        Spacer(1, 10),
        Paragraph(
            f"<b>Total Billed:</b> {ledger['total_billed']:,.3f} JOD &nbsp;&nbsp; "
            f"<b>Total Paid:</b> {ledger['total_paid']:,.3f} JOD &nbsp;&nbsp; "
            f"<b>Outstanding:</b> {ledger['total_outstanding']:,.3f} JOD",
            ss["Body"],
        ),
        Spacer(1, 14),
    ]

    for bill in ledger["bills"]:
        header = f"<b>{X(bill['id'])}</b>"
        if bill["bill_reference"]:
            header += f" · {X(bill['bill_reference'])}"
        header += f" · {X(bill['bill_date'])} · {X(bill['status'])}"
        story.append(Paragraph(header, ss["H2"]))

        data = [["Payment Date", "Amount (JOD)", "Method", "Notes"]]
        for p in bill["payments"]:
            data.append([p["payment_date"], f"{p['amount']:,.3f}", p["method"] or "—", p["notes"] or ""])
        data.append(["", "", "", ""])
        data.append(["Bill Total", f"{bill['total_amount']:,.3f}", "", ""])
        data.append(["Paid", f"{bill['paid']:,.3f}", "", ""])
        data.append(["Balance", f"{bill['balance']:,.3f}", "", ""])

        t = _section_table(data, [35 * mm, 30 * mm, 35 * mm, 65 * mm])
        story.append(t)
        story.append(Spacer(1, 12))

    if not ledger["bills"]:
        story.append(Paragraph("No bills logged for this distributor.", ss["Body"]))

    doc.build(story)
    buf.seek(0)
    return buf
