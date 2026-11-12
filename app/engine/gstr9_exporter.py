"""
GSTR-9 Annual Return Working Paper Exporter (.xlsx).
Generates a CA-ready 8-sheet Excel workbook covering:
1. GSTR-9 Executive Dashboard
2. Tables 4 & 5 - Outward Recon (Books vs GSTR-1 vs GSTR-3B)
3. Tables 6, 7 & 8 - ITC Bridge & Reversals
4. Tables 9 & 10-14 - Tax Paid, Rule 88A & Prior-FY Cutoff
5. Tables 17 & 18 - HSN Summary & Validation
6. Sec 17(5) & Reversal Schedule
7. Table 8D Vendor Drilldown
8. GSTR-9C Audit Tie-Out
"""

from __future__ import annotations

from io import BytesIO
from typing import Any, Dict, List

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter


NAVY_FILL = PatternFill(start_color="0F172A", end_color="0F172A", fill_type="solid")
BLUE_HDR_FILL = PatternFill(start_color="1E40AF", end_color="1E40AF", fill_type="solid")
SUB_HDR_FILL = PatternFill(start_color="EFF6FF", end_color="EFF6FF", fill_type="solid")
TOTAL_FILL = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid")
ZEBRA_FILL = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")
OK_FILL = PatternFill(start_color="DCFCE7", end_color="DCFCE7", fill_type="solid")
WARN_FILL = PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid")
ERR_FILL = PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid")

FONT_TITLE = Font(name="Calibri", size=14, bold=True, color="FFFFFF")
FONT_SUB = Font(name="Calibri", size=10, italic=True, color="475569")
FONT_HDR = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
FONT_SUBHDR = Font(name="Calibri", size=10, bold=True, color="1E3A8A")
FONT_BODY = Font(name="Calibri", size=10, color="0F172A")
FONT_BOLD = Font(name="Calibri", size=10, bold=True, color="0F172A")
FONT_WARN = Font(name="Calibri", size=10, bold=True, color="B45309")
FONT_ERR = Font(name="Calibri", size=10, bold=True, color="B91C1C")
FONT_OK = Font(name="Calibri", size=10, bold=True, color="15803D")

THIN_SIDE = Side(style="thin", color="CBD5E1")
BORDER_ALL = Border(left=THIN_SIDE, right=THIN_SIDE, top=THIN_SIDE, bottom=THIN_SIDE)
INR_FMT = '#,##0.00'


def _style_banner(ws, last_col: str, title: str, subtitle: str) -> None:
    ws.views.sheetView[0].showGridLines = True
    ws.merge_cells(f"A1:{last_col}1")
    c1 = ws["A1"]
    c1.value = title
    c1.font = FONT_TITLE
    c1.fill = BLUE_HDR_FILL
    c1.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[1].height = 34

    ws.merge_cells(f"A2:{last_col}2")
    c2 = ws["A2"]
    c2.value = subtitle
    c2.font = FONT_SUB
    c2.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[2].height = 20


def _write_table_header(ws, row_idx: int, headers: List[str]) -> None:
    ws.row_dimensions[row_idx].height = 24
    for col_idx, text in enumerate(headers, start=1):
        cell = ws.cell(row=row_idx, column=col_idx, value=text)
        cell.font = FONT_HDR
        cell.fill = NAVY_FILL
        cell.border = BORDER_ALL
        cell.alignment = Alignment(horizontal="center" if col_idx > 2 else "left", vertical="center", wrap_text=True)


def _auto_fit_columns(ws, min_width: int = 12, max_width: int = 44) -> None:
    for col in ws.columns:
        col_letter = get_column_letter(col[0].column)
        longest = 0
        for cell in col[2:]:  # skip banner rows
            val = str(cell.value or "")
            if val.startswith("="):
                longest = max(longest, 14)
            else:
                longest = max(longest, len(val))
        ws.column_dimensions[col_letter].width = max(min_width, min(max_width, longest + 4))


def export_gstr9_excel(gstr9_data: Dict[str, Any]) -> BytesIO:
    """Build and return an 8-sheet GSTR-9 Working Paper workbook in memory."""
    wb = openpyxl.Workbook()
    summary = gstr9_data.get("summary", {})
    tables = gstr9_data.get("tables", {})
    bridge = gstr9_data.get("gstr9c_bridge", {})
    reversals = gstr9_data.get("reversals_schedule", [])
    drilldown = gstr9_data.get("table_8d_drilldown", [])
    fy = summary.get("financial_year", "2025-26")

    # ------------------------------------------------------------------
    # SHEET 1: GSTR-9 Executive Dashboard
    # ------------------------------------------------------------------
    ws1 = wb.active
    ws1.title = "GSTR-9 Executive Dashboard"
    _style_banner(
        ws1,
        "D",
        f"TALLYWALLS GSTR-9 ANNUAL RETURN WORKING PAPER (FY {fy})",
        f"Sec 16(4) Cutoff Window: {summary.get('cutoff_window', 'Apr - Nov 30')} | HSN Threshold: {'> Rs. 5 Cr (6-Digit)' if summary.get('turnover_above_5cr') else '<= Rs. 5 Cr (4-Digit)'}",
    )
    _write_table_header(ws1, 4, ["Statutory Section / Metric", "Amount / Count", "Threshold / Reference", "Audit Status"])

    kpis = [
        ("Books Annual Turnover (Table 5N)", summary.get("books_turnover", 0.0), "Sales Register Net", "INFO"),
        ("GSTR-1 Declared Turnover", summary.get("gstr1_turnover", 0.0), "Annual GSTR-1 Total", "INFO"),
        ("GSTR-3B Reported Outward Turnover", summary.get("gstr3b_turnover", 0.0), "Table 3.1(a)+(b)+(c)+(e)", "INFO"),
        ("Outward Turnover Variance (Books vs Portal)", summary.get("turnover_variance", 0.0), "Target: 0.00 (GSTR-9C Table 5)", "WARN" if abs(summary.get("turnover_variance", 0.0)) > 1 else "OK"),
        ("Gross ITC Availed in GSTR-3B (Table 6O)", summary.get("table_6o_gross_itc", 0.0), "GSTR-3B Table 4A + Reclaimed", "INFO"),
        ("Total ITC Reversals (Table 7I)", summary.get("table_7i_reversals", 0.0), "Rule 37 + Rule 42/43 + Sec 17(5)", "WARN" if summary.get("table_7i_reversals", 0.0) > 0 else "OK"),
        ("Net Eligible ITC Available (Table 7J)", summary.get("table_7j_net_itc", 0.0), "Table 6O - Table 7I", "OK"),
        ("Consolidated GSTR-2B ITC (Table 8A)", summary.get("table_8a_2b_itc", 0.0), "Includes Cutoff Window", "INFO"),
        ("FY ITC Claimed in Next FY Cutoff (Table 8C)", summary.get("table_8c_next_fy_itc", 0.0), "Apr - Nov Next FY", "INFO"),
        ("Table 8D Difference [8A - (8B + 8C)]", summary.get("table_8d_diff_itc", 0.0), "Explained in Table 8D Drilldown", "WARN" if abs(summary.get("table_8d_diff_itc", 0.0)) > 1 else "OK"),
        ("Sec 17(5) & Personal Blocked Credit (Table 7E)", summary.get("sec_17_5_blocked_tax", 0.0), "Excluded from Net Eligible ITC", "INFO"),
        ("Rule 37 (>180 Days Unpaid Creditor Reversal)", summary.get("rule_37_unpaid_tax", 0.0), "Table 7A Reversal", "WARN" if summary.get("rule_37_unpaid_tax", 0.0) > 0 else "OK"),
        ("Rule 42 Exempt Turnover Reversal (Table 7C)", summary.get("rule_42_reversal_tax", 0.0), f"Exempt Ratio: {summary.get('rule_42_exempt_ratio_pct', 0.0)}%", "INFO"),
        ("Total Annual Tax Payable (Table 9)", summary.get("tax_payable_total", 0.0), "GSTR-9 Outward + RCM", "INFO"),
        ("Total Tax Paid (Cash + ITC in 3B Table 6.1)", summary.get("tax_paid_total", 0.0), "Discharged in GSTR-3B", "INFO"),
        ("Tax Shortfall / (Excess) Payable (Table 9)", summary.get("tax_shortfall_excess", 0.0), "Target: <= 0.00", "ERR" if summary.get("tax_shortfall_excess", 0.0) > 1 else "OK"),
        ("Interest & Late Fee Diff (Books vs Portal)", summary.get("interest_late_fee_diff", 0.0), f"Portal Paid: Rs. {summary.get('portal_interest_late_fee', 0.0):,.2f}", "WARN" if abs(summary.get("interest_late_fee_diff", 0.0)) > 1 else "OK"),
        ("HSN Validation Issues (Tables 17 & 18)", summary.get("hsn_total_errors", 0), "Digit / Rate / UQC Alerts", "WARN" if summary.get("hsn_total_errors", 0) > 0 else "OK"),
    ]

    for idx, (label, val, ref, status) in enumerate(kpis, start=5):
        ws1.row_dimensions[idx].height = 21
        c_lbl = ws1.cell(row=idx, column=1, value=label)
        c_val = ws1.cell(row=idx, column=2, value=val)
        c_ref = ws1.cell(row=idx, column=3, value=ref)
        c_st = ws1.cell(row=idx, column=4, value=status if status in ("OK", "WARN", "ERR") else "REVIEW")
        for c in (c_lbl, c_val, c_ref, c_st):
            c.border = BORDER_ALL
            c.font = FONT_BODY
        c_lbl.font = FONT_BOLD
        if isinstance(val, float):
            c_val.number_format = INR_FMT
            c_val.alignment = Alignment(horizontal="right", vertical="center")
        else:
            c_val.alignment = Alignment(horizontal="right", vertical="center")
        if status == "OK":
            c_st.fill = OK_FILL
            c_st.font = FONT_OK
        elif status == "WARN":
            c_st.fill = WARN_FILL
            c_st.font = FONT_WARN
        elif status == "ERR":
            c_st.fill = ERR_FILL
            c_st.font = FONT_ERR
        c_st.alignment = Alignment(horizontal="center", vertical="center")

    _auto_fit_columns(ws1)

    # ------------------------------------------------------------------
    # SHEET 2: Tables 4 & 5 - Outward Recon
    # ------------------------------------------------------------------
    ws2 = wb.create_sheet("Tables 4 & 5 - Outward")
    _style_banner(ws2, "L", f"GSTR-9 TABLES 4 & 5 — OUTWARD SUPPLIES RECONCILIATION (FY {fy})", "3-Way Tie-Out: Books Sales Register vs GSTR-1 vs GSTR-9 Statutory Reporting")
    _write_table_header(
        ws2,
        4,
        [
            "Table Code", "Nature of Supplies",
            "Books Taxable", "Books IGST", "Books CGST", "Books SGST",
            "GSTR-1 Taxable", "GSTR-1 IGST", "GSTR-1 CGST", "GSTR-1 SGST",
            "Variance Taxable", "Variance Tax",
        ],
    )
    r_idx = 5
    for row in tables.get("table_4", []):
        bk = row["books"]
        g1 = row["gstr1"]
        vals = [
            row["code"], row["desc"],
            bk["taxable"], bk["igst"], bk["cgst"], bk["sgst"],
            g1["taxable"], g1["igst"], g1["cgst"], g1["sgst"],
            f"=C{r_idx}-G{r_idx}",
            f"=(D{r_idx}+E{r_idx}+F{r_idx})-(H{r_idx}+I{r_idx}+J{r_idx})",
        ]
        is_tot = bool(row.get("is_total"))
        for c_i, v in enumerate(vals, start=1):
            cell = ws2.cell(row=r_idx, column=c_i, value=v)
            cell.border = BORDER_ALL
            cell.font = FONT_BOLD if is_tot else FONT_BODY
            if is_tot:
                cell.fill = TOTAL_FILL
            if c_i >= 3:
                cell.number_format = INR_FMT
                cell.alignment = Alignment(horizontal="right", vertical="center")
        r_idx += 1

    r_idx += 1
    _write_table_header(ws2, r_idx, ["Table 5", "Exempt / Nil / Non-GST & Total Turnover", "Books Taxable", "GSTR-1 Taxable", "GSTR-3B Taxable", "Books vs GSTR-1 Diff", "", "", "", "", "", ""])
    r_idx += 1
    for row in tables.get("table_5", []):
        vals = [
            row["code"], row["desc"],
            row["books_taxable"], row["gstr1_taxable"], row["gstr3b_taxable"],
            f"=C{r_idx}-D{r_idx}",
        ]
        is_tot = bool(row.get("is_total") or row.get("is_subtotal"))
        for c_i, v in enumerate(vals, start=1):
            cell = ws2.cell(row=r_idx, column=c_i, value=v)
            cell.border = BORDER_ALL
            cell.font = FONT_BOLD if is_tot else FONT_BODY
            if is_tot:
                cell.fill = TOTAL_FILL
            if c_i >= 3:
                cell.number_format = INR_FMT
                cell.alignment = Alignment(horizontal="right", vertical="center")
        r_idx += 1
    ws2.freeze_panes = "C5"
    _auto_fit_columns(ws2)

    # ------------------------------------------------------------------
    # SHEET 3: Tables 6, 7 & 8 - ITC Bridge
    # ------------------------------------------------------------------
    ws3 = wb.create_sheet("Tables 6, 7 & 8 - ITC Bridge")
    _style_banner(ws3, "G", f"GSTR-9 TABLES 6, 7 & 8 — INPUT TAX CREDIT BRIDGE (FY {fy})", "ITC Availed (GSTR-3B Table 4) -> Rule Reversals -> Consolidated GSTR-2B Comparison")
    r_idx = 4
    for sec_title, rows_key in [
        ("TABLE 6: DETAILS OF ITC AVAILED DURING THE FINANCIAL YEAR", "table_6"),
        ("TABLE 7: DETAILS OF ITC REVERSED & INELIGIBLE ITC", "table_7"),
        ("TABLE 8: OTHER ITC RELATED INFORMATION (COMPARISON WITH GSTR-2B)", "table_8"),
    ]:
        ws3.merge_cells(f"A{r_idx}:G{r_idx}")
        hdr = ws3[f"A{r_idx}"]
        hdr.value = sec_title
        hdr.font = FONT_SUBHDR
        hdr.fill = SUB_HDR_FILL
        r_idx += 1
        _write_table_header(ws3, r_idx, ["Code", "Description", "IGST", "CGST", "SGST", "Cess", "Total ITC"])
        r_idx += 1
        for row in tables.get(rows_key, []):
            vals = [row["code"], row["desc"], row["igst"], row["cgst"], row["sgst"], row["cess"], f"=SUM(C{r_idx}:F{r_idx})"]
            is_tot = bool(row.get("is_total") or row.get("is_subtotal"))
            for c_i, v in enumerate(vals, start=1):
                cell = ws3.cell(row=r_idx, column=c_i, value=v)
                cell.border = BORDER_ALL
                cell.font = FONT_BOLD if is_tot else FONT_BODY
                if is_tot:
                    cell.fill = TOTAL_FILL
                if c_i >= 3:
                    cell.number_format = INR_FMT
                    cell.alignment = Alignment(horizontal="right", vertical="center")
            r_idx += 1
        r_idx += 1
    _auto_fit_columns(ws3)

    # ------------------------------------------------------------------
    # SHEET 4: Tables 9 & 10-14 - Tax & Cutoff
    # ------------------------------------------------------------------
    ws4 = wb.create_sheet("Tables 9 & 10-14 - Tax & Cutoff")
    _style_banner(ws4, "G", f"GSTR-9 TABLES 9 & 10-14 — TAX PAID & NEXT-FY CUTOFF (FY {fy})", summary.get("rule88a_message", ""))
    _write_table_header(ws4, 4, ["Tax Head", "Code", "Tax Payable", "Paid Through Cash", "Paid Through ITC", "Total Paid", "Shortfall / (Excess)"])
    r_idx = 5
    for row in tables.get("table_9", []):
        vals = [row["head"], row["code"], row["tax_payable"], row["paid_through_cash"], row["paid_through_itc"], f"=D{r_idx}+E{r_idx}", f"=C{r_idx}-F{r_idx}"]
        for c_i, v in enumerate(vals, start=1):
            cell = ws4.cell(row=r_idx, column=c_i, value=v)
            cell.border = BORDER_ALL
            cell.font = FONT_BODY
            if c_i >= 3:
                cell.number_format = INR_FMT
                cell.alignment = Alignment(horizontal="right", vertical="center")
        r_idx += 1

    r_idx += 1
    _write_table_header(ws4, r_idx, ["Table", "Next-FY Cutoff Adjustments (Apr - Nov)", "Taxable", "IGST", "CGST", "SGST", "Cess"])
    r_idx += 1
    for row in tables.get("tables_10_14", []):
        vals = [row["code"], row["desc"], row["taxable"], row["igst"], row["cgst"], row["sgst"], row["cess"]]
        for c_i, v in enumerate(vals, start=1):
            cell = ws4.cell(row=r_idx, column=c_i, value=v)
            cell.border = BORDER_ALL
            cell.font = FONT_BODY
            if c_i >= 3:
                cell.number_format = INR_FMT
                cell.alignment = Alignment(horizontal="right", vertical="center")
        r_idx += 1
    _auto_fit_columns(ws4)

    # ------------------------------------------------------------------
    # SHEET 5: Tables 17 & 18 - HSN Summary
    # ------------------------------------------------------------------
    ws5 = wb.create_sheet("Tables 17 & 18 - HSN Summary")
    _style_banner(ws5, "K", f"GSTR-9 TABLES 17 & 18 — HSN / SAC COMPLIANCE SUMMARY (FY {fy})", "Validates 4-digit vs 6-digit CBIC HSN rules, UQC presence, and Rate% accuracy")
    r_idx = 4
    for title_lbl, key in [("TABLE 17: HSN WISE SUMMARY OF OUTWARD SUPPLIES", "table_17"), ("TABLE 18: HSN WISE SUMMARY OF INWARD SUPPLIES", "table_18")]:
        ws5.merge_cells(f"A{r_idx}:K{r_idx}")
        h = ws5[f"A{r_idx}"]
        h.value = title_lbl
        h.font = FONT_SUBHDR
        h.fill = SUB_HDR_FILL
        r_idx += 1
        _write_table_header(ws5, r_idx, ["HSN / SAC", "UQC", "Total Qty", "Rate (%)", "Taxable Value", "IGST", "CGST", "SGST", "Cess", "Status", "Validation Remarks"])
        r_idx += 1
        for row in tables.get(key, []):
            vals = [row["hsn"], row["uqc"], row["qty"], row["rate"], row["taxable"], row["igst"], row["cgst"], row["sgst"], row["cess"], row["status"], row["issues"]]
            for c_i, v in enumerate(vals, start=1):
                cell = ws5.cell(row=r_idx, column=c_i, value=v)
                cell.border = BORDER_ALL
                cell.font = FONT_ERR if (c_i == 10 and v == "WARNING") else FONT_BODY
                if c_i == 10 and v == "WARNING":
                    cell.fill = WARN_FILL
                if 3 <= c_i <= 9:
                    cell.number_format = INR_FMT
                    cell.alignment = Alignment(horizontal="right", vertical="center")
            r_idx += 1
        r_idx += 1
    ws5.freeze_panes = "A6"
    _auto_fit_columns(ws5)

    # ------------------------------------------------------------------
    # SHEET 6: Sec 17(5) & Reversal Schedule
    # ------------------------------------------------------------------
    ws6 = wb.create_sheet("Sec 17(5) & Reversal Schedule")
    _style_banner(ws6, "H", f"ITC REVERSALS & INELIGIBLE SCHEDULE (FY {fy})", "Invoice-level backup for Table 7A (Rule 37), 7C (Rule 42), 7E (Sec 17(5) / Sec 17(1))")
    _write_table_header(ws6, 4, ["Statutory Rule", "Counterparty", "GSTIN", "Invoice No", "Date", "Taxable Value", "Tax Amount", "Audit Reason"])
    for idx, item in enumerate(reversals, start=5):
        vals = [item["rule"], item["party"], item["gstin"], item["inv_no"], item["doc_date"], item["taxable"], item["tax_amount"], item["reason"]]
        for c_i, v in enumerate(vals, start=1):
            cell = ws6.cell(row=idx, column=c_i, value=v)
            cell.border = BORDER_ALL
            cell.font = FONT_BODY
            if idx % 2 == 0:
                cell.fill = ZEBRA_FILL
            if c_i in (6, 7):
                cell.number_format = INR_FMT
                cell.alignment = Alignment(horizontal="right", vertical="center")
    ws6.freeze_panes = "A5"
    _auto_fit_columns(ws6)

    # ------------------------------------------------------------------
    # SHEET 7: Table 8D Vendor Drilldown
    # ------------------------------------------------------------------
    ws7 = wb.create_sheet("Table 8D Vendor Drilldown")
    _style_banner(ws7, "H", f"GSTR-9 TABLE 8D — GSTR-2B vs BOOKS VARIANCE DRILLDOWN (FY {fy})", "Explains Unbooked 2B (8E), Vendor Defaults (Missing in 2B), Value Mismatches & Blocked Items")
    _write_table_header(ws7, 4, ["Variance Category", "Supplier Name", "GSTIN", "Invoice No", "Books Tax", "GSTR-2B Tax", "Difference", "Recommended Action"])
    for idx, item in enumerate(drilldown, start=5):
        vals = [item["category"], item["party"], item["gstin"], item["inv_no"], item["books_tax"], item["portal_tax"], item["diff_tax"], item["action"]]
        for c_i, v in enumerate(vals, start=1):
            cell = ws7.cell(row=idx, column=c_i, value=v)
            cell.border = BORDER_ALL
            cell.font = FONT_BODY
            if idx % 2 == 0:
                cell.fill = ZEBRA_FILL
            if c_i in (5, 6, 7):
                cell.number_format = INR_FMT
                cell.alignment = Alignment(horizontal="right", vertical="center")
    ws7.freeze_panes = "A5"
    _auto_fit_columns(ws7)

    # ------------------------------------------------------------------
    # SHEET 8: GSTR-9C Audit Tie-Out
    # ------------------------------------------------------------------
    ws8 = wb.create_sheet("GSTR-9C Audit Tie-Out")
    _style_banner(ws8, "C", f"FORM GSTR-9C RECONCILIATION TIE-OUT PREVIEW (FY {fy})", "Prepared for Statutory Audit Sign-Off: Table 5 Turnover & Table 12 ITC Bridge")
    _write_table_header(ws8, 4, ["GSTR-9C Line Reference", "Particulars", "Amount (Rs.)"])
    t5b = bridge.get("table_5_turnover", {})
    t12b = bridge.get("table_12_itc", {})
    rows_9c = [
        ("Table 5A", "Turnover as per Audited Financial Statements (Books Sales Register)", t5b.get("books_annual_turnover", 0.0)),
        ("Table 5Q", "Annual Turnover as declared in GSTR-9 (Table 5N)", t5b.get("gstr9_declared_turnover", 0.0)),
        ("Table 5R", "Unreconciled Turnover Difference (5Q - 5A)", "=C6-C5"),
        ("Table 12A", "ITC availed as per Audited Books of Accounts (Net of Blocked/Rule 37)", t12b.get("books_net_eligible_itc", 0.0)),
        ("Table 12E", "ITC claimed in Annual Return GSTR-9 (Table 7J)", t12b.get("gstr9_table_7j_net_itc", 0.0)),
        ("Table 12F", "Unreconciled ITC Difference (12A - 12E)", "=C8-C9"),
    ]
    for idx, (ref, desc, amt) in enumerate(rows_9c, start=5):
        ws8.row_dimensions[idx].height = 22
        c1 = ws8.cell(row=idx, column=1, value=ref)
        c2 = ws8.cell(row=idx, column=2, value=desc)
        c3 = ws8.cell(row=idx, column=3, value=amt)
        is_diff = ref in ("Table 5R", "Table 12F")
        for c in (c1, c2, c3):
            c.border = BORDER_ALL
            c.font = FONT_BOLD if is_diff else FONT_BODY
            if is_diff:
                c.fill = TOTAL_FILL
        c3.number_format = INR_FMT
        c3.alignment = Alignment(horizontal="right", vertical="center")
    _auto_fit_columns(ws8)

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf

