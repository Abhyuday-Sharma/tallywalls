"""
Excel Report Exporter using openpyxl.
Generates an executive-grade, styled, multi-sheet Excel reconciliation workbook:
1. Executive Summary & KPIs
2. Reconciled Ledger (Full Detail with color-coded status)
3. Vendor Follow-up Sheet (ITC at Risk)
4. Unbooked Invoices (Missing in Books)
"""

from io import BytesIO
from typing import Dict, Any, List
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter


# Color palette for openpyxl
COLORS = {
    "primary_dark": "0F172A",
    "primary_blue": "1E3A8A",
    "header_fill": "1E40AF",
    "zebra_fill": "F8FAFC",
    "border_gray": "CBD5E1",
    # Status fills
    "EXACT_MATCH": {"fill": "DCFCE7", "font": "15803D"},        # Light green, dark green text
    "NORMALIZED_MATCH": {"fill": "FEF3C7", "font": "B45309"},   # Light yellow, dark amber text
    "PROBABLE_TYPO": {"fill": "FEF08A", "font": "A16207"},      # Warm yellow
    "VALUE_MISMATCH": {"fill": "FFEDD5", "font": "C2410C"},     # Orange
    "MISSING_IN_2B": {"fill": "FEE2E2", "font": "B91C1C"},      # Red
    "MISSING_IN_BOOKS": {"fill": "E0F2FE", "font": "0369A1"},   # Blue
}


def build_border():
    thin = Side(style="thin", color=COLORS["border_gray"])
    return Border(left=thin, right=thin, top=thin, bottom=thin)


def _normalize_results_from_recon(recon_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Accept either legacy nested `results` or slimmed flat `ledger` payloads
    so /api/reconcile and /api/export avoid duplicating rows in HTTP JSON.
    """
    raw_results = recon_data.get("results")
    if isinstance(raw_results, list) and len(raw_results) > 0:
        return raw_results

    ledger = recon_data.get("ledger") or []
    normalized: List[Dict[str, Any]] = []
    status_labels = {
        "EXACT_MATCH": "Exact Match",
        "NORMALIZED_MATCH": "Normalized Match",
        "PROBABLE_TYPO": "Probable Typo (Verify)",
        "TYPO_MATCH": "Probable Typo (Verify)",
        "VALUE_MISMATCH": "Value Mismatch",
        "MISSING_IN_2B": "Missing in 2B (ITC at Risk)",
        "MISSING_IN_BOOKS": "Missing in Books (Unbooked)",
    }
    for row in ledger:
        raw_status = row.get("status") or row.get("match_category") or "EXACT_MATCH"
        canonical_status = "PROBABLE_TYPO" if raw_status == "TYPO_MATCH" else raw_status

        has_books = any([
            row.get("taxable_books") is not None,
            row.get("tax_amount_books") is not None,
            row.get("inv_num_books"),
            row.get("gstin_books"),
        ])
        has_portal = any([
            row.get("taxable_2b") is not None,
            row.get("tax_amount_2b") is not None,
            row.get("inv_num_2b"),
            row.get("gstin_2b"),
        ])

        b_taxable = float(row.get("taxable_books") or 0.0)
        b_tax = float(row.get("tax_amount_books") or 0.0)
        p_taxable = float(row.get("taxable_2b") or 0.0)
        p_tax = float(row.get("tax_amount_2b") or 0.0)

        books = {
            "gstin": row.get("gstin_books") or "",
            "party_name": row.get("supplier_name_books") or "",
            "raw_inv": row.get("inv_num_books") or "",
            "inv_date": row.get("inv_date_books") or "",
            "taxable": b_taxable,
            "total_tax": b_tax,
            "total_inv_val": float(row.get("total_inv_val_books")) if row.get("total_inv_val_books") is not None else round(b_taxable + b_tax, 2),
        } if has_books else None

        portal = {
            "gstin": row.get("gstin_2b") or "",
            "party_name": row.get("supplier_name_2b") or "",
            "raw_inv": row.get("inv_num_2b") or "",
            "inv_date": row.get("inv_date_2b") or "",
            "taxable": p_taxable,
            "total_tax": p_tax,
            "total_inv_val": float(row.get("total_inv_val_2b")) if row.get("total_inv_val_2b") is not None else round(p_taxable + p_tax, 2),
        } if has_portal else None

        normalized.append({
            "status": canonical_status,
            "status_label": row.get("status_label") or status_labels.get(canonical_status, canonical_status),
            "books": books,
            "portal": portal,
            "books_line_count": row.get("books_line_count", 1 if books else 0),
            "portal_line_count": row.get("portal_line_count", 1 if portal else 0),
            "books_slabs": row.get("books_slabs", ""),
            "portal_slabs": row.get("portal_slabs", ""),
            "itc_ineligible": bool(row.get("itc_ineligible", False)),
            "is_rcm": bool(row.get("is_rcm", False)),
            "tax_head_mismatch": bool(row.get("tax_head_mismatch", False)),
            "taxable_diff": row.get("taxable_diff", 0.0),
            "tax_diff": row.get("tax_diff", 0.0),
            "remarks": row.get("audit_remark") or "",
        })
    return normalized


def export_reconciliation_excel(recon_data: Dict[str, Any]) -> BytesIO:
    """
    Builds a professional, multi-tab Excel workbook from reconciliation results.
    Returns in-memory BytesIO buffer.
    """
    wb = openpyxl.Workbook()
    summary = recon_data.get("summary", {})
    results: List[Dict[str, Any]] = _normalize_results_from_recon(recon_data)

    # -------------------------------------------------------------
    # SHEET 1: Executive Summary & Dashboard
    # -------------------------------------------------------------
    ws_sum = wb.active
    ws_sum.title = "Executive Summary"
    ws_sum.views.sheetView[0].showGridLines = True

    # Title Banner
    ws_sum.merge_cells("A1:G1")
    title_cell = ws_sum["A1"]
    title_cell.value = "GST RECONCILIATION & ITC AUDIT REPORT"
    title_cell.font = Font(name="Calibri", size=16, bold=True, color="FFFFFF")
    title_cell.fill = PatternFill(start_color=COLORS["primary_blue"], end_color=COLORS["primary_blue"], fill_type="solid")
    title_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws_sum.row_dimensions[1].height = 40

    ws_sum.merge_cells("A2:G2")
    sub_cell = ws_sum["A2"]
    sub_cell.value = f"Comparison of GSTR-2B Portal Data vs. Purchase Register Books (Tolerance: ± ₹{summary.get('tolerance', 1.00):.2f})"
    sub_cell.font = Font(name="Calibri", size=10, italic=True, color="64748B")
    sub_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws_sum.row_dimensions[2].height = 22

    # KPI Table Header
    headers_kpi = ["Financial Metric / Indicator", "Books (Tally/ERP)", "Portal (GSTR-2B)", "Variance / Impact", "Audit Status"]
    for col_idx, h in enumerate(headers_kpi, start=1):
        cell = ws_sum.cell(row=4, column=col_idx, value=h)
        cell.font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        cell.fill = PatternFill(start_color=COLORS["primary_dark"], end_color=COLORS["primary_dark"], fill_type="solid")
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws_sum.row_dimensions[4].height = 26

    kpi_rows = [
        ("Total Invoices Count", summary.get("total_books_invoices", 0), summary.get("total_2b_invoices", 0), summary.get("total_books_invoices", 0) - summary.get("total_2b_invoices", 0), "Volume check"),
        ("Total Taxable Value (₹)", summary.get("books_taxable_total", 0.0), summary.get("portal_taxable_total", 0.0), round(summary.get("books_taxable_total", 0.0) - summary.get("portal_taxable_total", 0.0), 2), "Tax Base"),
        ("Total GST / ITC (₹)", summary.get("books_tax_total", 0.0), summary.get("portal_tax_total", 0.0), round(summary.get("books_tax_total", 0.0) - summary.get("portal_tax_total", 0.0), 2), "Input Credit"),
        ("Reconciled & Eligible ITC (₹)", summary.get("matched_tax", 0.0), summary.get("matched_tax", 0.0), 0.00, "100% Claimable"),
        ("ITC at Risk (Missing in 2B) (₹)", summary.get("itc_at_risk_tax", 0.0), 0.00, summary.get("itc_at_risk_tax", 0.0), "VENDOR DEFAULT!"),
        ("Unbooked Purchases ITC (₹)", 0.00, summary.get("unbooked_tax", 0.0), summary.get("unbooked_tax", 0.0), "PENDING BOOKING"),
        ("Reconciliation Success Rate (%)", f"{summary.get('reconciliation_rate', 0.0)}%", "-", "-", "Benchmark: 95%+"),
    ]
    if summary.get("ineligible_tax", 0.0) > 0:
        kpi_rows.append(("Ineligible / Blocked ITC Excluded (₹)", summary.get("ineligible_tax", 0.0), summary.get("ineligible_tax", 0.0), 0.00, "DO NOT CLAIM (Sec 17(5))"))
    if summary.get("rcm_tax", 0.0) > 0:
        kpi_rows.append(("Reverse Charge (RCM) Supplies Tax (₹)", summary.get("rcm_tax", 0.0), summary.get("rcm_tax", 0.0), 0.00, "RCM LIABILITY / ITC"))
    if summary.get("multi_rate_matched_count", 0) > 0:
        kpi_rows.append(("Multi-Rate Split Bills Consolidated", summary.get("multi_rate_matched_count", 0), summary.get("multi_rate_matched_count", 0), 0, "Multi-rate resolved"))

    border = build_border()
    for row_idx, r_data in enumerate(kpi_rows, start=5):
        ws_sum.row_dimensions[row_idx].height = 22
        for col_idx, val in enumerate(r_data, start=1):
            cell = ws_sum.cell(row=row_idx, column=col_idx, value=val)
            cell.border = border
            cell.font = Font(name="Calibri", size=10)
            if col_idx in (2, 3, 4) and isinstance(val, (int, float)):
                cell.number_format = "#,##0.00"
                cell.alignment = Alignment(horizontal="right", vertical="center")
            else:
                cell.alignment = Alignment(horizontal="left", vertical="center")

            # Highlighting specific rows
            if "VENDOR DEFAULT" in str(r_data[4]) or "DO NOT CLAIM" in str(r_data[4]):
                cell.fill = PatternFill(start_color=COLORS["MISSING_IN_2B"]["fill"], end_color=COLORS["MISSING_IN_2B"]["fill"], fill_type="solid")
            elif "PENDING BOOKING" in str(r_data[4]):
                cell.fill = PatternFill(start_color=COLORS["MISSING_IN_BOOKS"]["fill"], end_color=COLORS["MISSING_IN_BOOKS"]["fill"], fill_type="solid")

    # Match Breakdown Table
    brk_start_row = 5 + len(kpi_rows) + 2
    ws_sum.cell(row=brk_start_row, column=1, value="Classification Breakdown").font = Font(name="Calibri", size=12, bold=True, color=COLORS["primary_blue"])
    breakdown_headers = ["Classification Status", "Invoice Count", "Percentage of Total", "Color Tag"]
    for col_idx, h in enumerate(breakdown_headers, start=1):
        cell = ws_sum.cell(row=brk_start_row + 1, column=col_idx, value=h)
        cell.font = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
        cell.fill = PatternFill(start_color=COLORS["header_fill"], end_color=COLORS["header_fill"], fill_type="solid")
    ws_sum.row_dimensions[brk_start_row + 1].height = 22

    total_invs = max(summary.get("total_books_invoices", 1), 1)
    breakdown_data = [
        ("Exact Match (100% Matched)", summary.get("exact_count", 0), f"{(summary.get('exact_count', 0)/total_invs)*100:.1f}%", "Green"),
        ("Normalized Match (Format Resolved)", summary.get("normalized_count", 0), f"{(summary.get('normalized_count', 0)/total_invs)*100:.1f}%", "Yellow"),
        ("Probable Typo (Verify)", summary.get("typo_count", 0), f"{(summary.get('typo_count', 0)/total_invs)*100:.1f}%", "Yellow"),
        ("Value / Tax Mismatch", summary.get("value_mismatch_count", 0), f"{(summary.get('value_mismatch_count', 0)/total_invs)*100:.1f}%", "Orange"),
        ("Missing in 2B (ITC at Risk)", summary.get("missing_in_2b_count", 0), f"{(summary.get('missing_in_2b_count', 0)/total_invs)*100:.1f}%", "Red"),
        ("Missing in Books (Unbooked)", summary.get("missing_in_books_count", 0), "-", "Blue"),
    ]

    for row_idx, b_row in enumerate(breakdown_data, start=brk_start_row + 2):
        ws_sum.row_dimensions[row_idx].height = 20
        for col_idx, val in enumerate(b_row, start=1):
            cell = ws_sum.cell(row=row_idx, column=col_idx, value=val)
            cell.border = border
            cell.font = Font(name="Calibri", size=10)
            cell.alignment = Alignment(horizontal="left", vertical="center")

    for col in ws_sum.columns:
        col_letter = get_column_letter(col[0].column)
        ws_sum.column_dimensions[col_letter].width = 30

    # -------------------------------------------------------------
    # SHEET 2: Reconciled Ledger (Full Detail)
    # -------------------------------------------------------------
    ws_rec = wb.create_sheet(title="Reconciled Ledger")
    ws_rec.views.sheetView[0].showGridLines = True

    rec_headers = [
        "Match Status", "Supplier GSTIN", "Supplier / Trade Name",
        "Books Inv No", "Portal 2B Inv No", "Invoice Date",
        "Books Taxable (₹)", "2B Taxable (₹)", "Taxable Diff (₹)",
        "Books Tax (₹)", "2B Tax (₹)", "Tax Diff (₹)",
        "Audit Remarks"
    ]

    ws_rec.row_dimensions[1].height = 28
    for col_idx, h in enumerate(rec_headers, start=1):
        cell = ws_rec.cell(row=1, column=col_idx, value=h)
        cell.font = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
        cell.fill = PatternFill(start_color=COLORS["primary_blue"], end_color=COLORS["primary_blue"], fill_type="solid")
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for row_idx, r in enumerate(results, start=2):
        ws_rec.row_dimensions[row_idx].height = 20
        status_key = r.get("status", "EXACT_MATCH")
        books = r.get("books") or {}
        portal = r.get("portal") or {}

        row_values = [
            r.get("status_label", ""),
            books.get("gstin") or portal.get("gstin") or "",
            books.get("party_name") or portal.get("party_name") or "",
            books.get("raw_inv", ""),
            portal.get("raw_inv", ""),
            books.get("inv_date") or portal.get("inv_date") or "",
            books.get("taxable", 0.0) if books else None,
            portal.get("taxable", 0.0) if portal else None,
            r.get("taxable_diff", 0.0),
            books.get("total_tax", 0.0) if books else None,
            portal.get("total_tax", 0.0) if portal else None,
            r.get("tax_diff", 0.0),
            r.get("remarks", "")
        ]

        style_cfg = COLORS.get(status_key, {"fill": "FFFFFF", "font": "000000"})
        for col_idx, val in enumerate(row_values, start=1):
            cell = ws_rec.cell(row=row_idx, column=col_idx, value=val)
            cell.border = border
            cell.font = Font(name="Calibri", size=9)

            # Highlight status column specifically
            if col_idx == 1:
                cell.fill = PatternFill(start_color=style_cfg["fill"], end_color=style_cfg["fill"], fill_type="solid")
                cell.font = Font(name="Calibri", size=9, bold=True, color=style_cfg["font"])
                cell.alignment = Alignment(horizontal="center", vertical="center")
            elif col_idx in (7, 8, 9, 10, 11, 12):
                if val is not None:
                    cell.number_format = "#,##0.00"
                cell.alignment = Alignment(horizontal="right", vertical="center")
            else:
                cell.alignment = Alignment(horizontal="left", vertical="center")

    ws_rec.auto_filter.ref = ws_rec.dimensions

    # -------------------------------------------------------------
    # SHEET 3: Vendor Follow-up Sheet (ITC at Risk)
    # -------------------------------------------------------------
    ws_risk = wb.create_sheet(title="Vendor Follow-up (ITC at Risk)")
    ws_risk.views.sheetView[0].showGridLines = True

    risk_headers = [
        "Supplier Name", "Supplier GSTIN", "Invoice Number", "Invoice Date",
        "Invoice Value (₹)", "Taxable Value (₹)", "GST Amount at Risk (₹)", "Action Required"
    ]
    ws_risk.row_dimensions[1].height = 28
    for col_idx, h in enumerate(risk_headers, start=1):
        cell = ws_risk.cell(row=1, column=col_idx, value=h)
        cell.font = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
        cell.fill = PatternFill(start_color="B91C1C", end_color="B91C1C", fill_type="solid")
        cell.alignment = Alignment(horizontal="center", vertical="center")

    risk_rows = [r for r in results if r.get("status") == "MISSING_IN_2B"]
    for row_idx, r in enumerate(risk_rows, start=2):
        ws_risk.row_dimensions[row_idx].height = 20
        b = r.get("books", {})
        vals = [
            b.get("party_name", ""),
            b.get("gstin", ""),
            b.get("raw_inv", ""),
            b.get("inv_date", ""),
            b.get("total_inv_val", 0.0),
            b.get("taxable", 0.0),
            b.get("total_tax", 0.0),
            "Hold payment or request vendor to file GSTR-1 immediately."
        ]
        for col_idx, val in enumerate(vals, start=1):
            cell = ws_risk.cell(row=row_idx, column=col_idx, value=val)
            cell.border = border
            cell.font = Font(name="Calibri", size=9)
            if col_idx in (5, 6, 7):
                cell.number_format = "#,##0.00"
                cell.alignment = Alignment(horizontal="right", vertical="center")
            else:
                cell.alignment = Alignment(horizontal="left", vertical="center")
            if col_idx == 7:
                cell.fill = PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid")
                cell.font = Font(name="Calibri", size=9, bold=True, color="B91C1C")

    ws_risk.auto_filter.ref = ws_risk.dimensions

    # -------------------------------------------------------------
    # SHEET 4: Unbooked Invoices (2B Only)
    # -------------------------------------------------------------
    ws_unb = wb.create_sheet(title="Unbooked Invoices")
    ws_unb.views.sheetView[0].showGridLines = True

    unb_headers = [
        "Supplier Name", "Supplier GSTIN", "Portal Invoice No", "Invoice Date",
        "Invoice Value (₹)", "Taxable Value (₹)", "Unbooked GST (₹)", "Action Required"
    ]
    ws_unb.row_dimensions[1].height = 28
    for col_idx, h in enumerate(unb_headers, start=1):
        cell = ws_unb.cell(row=1, column=col_idx, value=h)
        cell.font = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
        cell.fill = PatternFill(start_color="0284C7", end_color="0284C7", fill_type="solid")
        cell.alignment = Alignment(horizontal="center", vertical="center")

    unb_rows = [r for r in results if r.get("status") == "MISSING_IN_BOOKS"]
    for row_idx, r in enumerate(unb_rows, start=2):
        ws_unb.row_dimensions[row_idx].height = 20
        p = r.get("portal", {})
        vals = [
            p.get("party_name", ""),
            p.get("gstin", ""),
            p.get("raw_inv", ""),
            p.get("inv_date", ""),
            p.get("total_inv_val", 0.0),
            p.get("taxable", 0.0),
            p.get("total_tax", 0.0),
            "Enter missing bill in Tally/ERP purchase register to claim ITC."
        ]
        for col_idx, val in enumerate(vals, start=1):
            cell = ws_unb.cell(row=row_idx, column=col_idx, value=val)
            cell.border = border
            cell.font = Font(name="Calibri", size=9)
            if col_idx in (5, 6, 7):
                cell.number_format = "#,##0.00"
                cell.alignment = Alignment(horizontal="right", vertical="center")
            else:
                cell.alignment = Alignment(horizontal="left", vertical="center")
            if col_idx == 7:
                cell.fill = PatternFill(start_color="E0F2FE", end_color="E0F2FE", fill_type="solid")
                cell.font = Font(name="Calibri", size=9, bold=True, color="0284C7")

    ws_unb.auto_filter.ref = ws_unb.dimensions

    # Auto-adjust column widths for sheets 2, 3, 4
    for ws in [ws_rec, ws_risk, ws_unb]:
        for col in ws.columns:
            max_len = max(len(str(cell.value or "")) for cell in col[:50])
            col_letter = get_column_letter(col[0].column)
            ws.column_dimensions[col_letter].width = max(max_len + 3, 14)

    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return output
