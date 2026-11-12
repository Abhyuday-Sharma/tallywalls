"""
Comprehensive automated tests for the GSTR-9 Annual Return Hub (v3.0):
1. Outward 3-way tie-out (Tables 4, 5, 10 & 11) + Sec 16(4) cutoff window
2. Annual ITC Bridge (Tables 6, 7 & 8: 6O - 7I == 7J, 8A - (8B + 8C) == 8D)
   including Sec 17(5), Sec 17(1), Rule 37 (>180d), and Rule 42 proportionality
3. Rule 88A Cross-Utilization warning & Interest / Late Fee reconciliation (Table 9)
4. HSN Summary Validation (Tables 17 & 18: 4-digit vs 6-digit threshold & rate checks)
5. FastAPI /api/gstr9/sample, /api/gstr9/compute (including .gz payload), and /api/gstr9/export (8-sheet openpyxl verification)
6. Zero-emoji check across GSTR-9 modules & frontend
"""

import gzip
import io
import json
import re
from pathlib import Path

import openpyxl
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.engine.gstr9_engine import compute_gstr9_return
from app.engine.gstr9_exporter import export_gstr9_excel
from app.engine.gstr9_parsers import (
    classify_period_bucket,
    empty_gstr3b_structure,
    parse_gstr1_files,
    parse_sales_register_df,
)
from app.engine.gstr9_sample import run_sample_gstr9_fy2526
from app.main import app


client = TestClient(app)


def _row_by_code(rows, code):
    for r in rows:
        if r.get("code") == code:
            return r
    raise AssertionError(f"Row code {code} not found")


def _head_sum(row):
    return round(
        float(row.get("igst", 0))
        + float(row.get("cgst", 0))
        + float(row.get("sgst", 0))
        + float(row.get("cess", 0)),
        2,
    )


def test_sec_16_4_period_bucket_classification():
    # In-FY Apr 2025 - Mar 2026
    assert classify_period_bucket("12-04-2025", "", "2025-26", 11) == "IN_FY"
    assert classify_period_bucket("31-03-2026", "", "2025-26", 11) == "IN_FY"
    # Invoice dated in FY 2025-26, reported in next-FY Apr-Nov window
    assert classify_period_bucket("28-03-2026", "10-07-2026", "2025-26", 11) == "NEXT_FY_CUTOFF"
    assert classify_period_bucket("28-03-2026", "25-11-2026", "2025-26", 11) == "NEXT_FY_CUTOFF"
    # Cutoff Oct 31 rejects Nov reporting as time-barred
    assert classify_period_bucket("28-03-2026", "25-11-2026", "2025-26", 10) == "TIME_BARRED"
    assert classify_period_bucket("28-03-2026", "15-12-2026", "2025-26", 11) == "TIME_BARRED"


def test_sample_outward_itc_and_table_identities():
    res = run_sample_gstr9_fy2526(turnover_above_5cr=True, cutoff_month=11)
    assert res["status"] == "success"
    s = res["summary"]
    tables = res["tables"]

    # Outward 3-way check: B2C has deliberate Rs. 25,000 variance between Books & GSTR-1
    t4n = _row_by_code(tables["table_4"], "4N")
    t5n = _row_by_code(tables["table_5"], "5N")
    assert round(t4n["variance_books_vs_gstr1"], 2) == 25000.0
    assert s["turnover_variance"] == 25000.0
    assert t5n["books_taxable"] == s["books_turnover"]

    # Table 10 must capture the Next-FY Jul 2026 Rs. 250,000 amendment
    t10 = _row_by_code(tables["tables_10_14"], "10")
    assert round(t10["taxable"], 2) == 250000.0
    assert round(t10["igst"], 2) == 45000.0

    # Table 6, 7, 8 core accounting identities: 6O - 7I == 7J and 8A - (8B + 8C) == 8D
    t6o = _row_by_code(tables["table_6"], "6O")
    t7i = _row_by_code(tables["table_7"], "7I")
    t7j = _row_by_code(tables["table_7"], "7J")
    assert round(_head_sum(t6o) - _head_sum(t7i), 2) == round(_head_sum(t7j), 2)
    assert round(s["table_7j_net_itc"], 2) == round(_head_sum(t7j), 2)

    t8a = _row_by_code(tables["table_8"], "8A")
    t8b = _row_by_code(tables["table_8"], "8B")
    t8c = _row_by_code(tables["table_8"], "8C")
    t8d = _row_by_code(tables["table_8"], "8D")
    assert round(_head_sum(t8a) - (_head_sum(t8b) + _head_sum(t8c)), 2) == round(_head_sum(t8d), 2)

    # Reversal math checks:
    # Rule 37 (>180d) invoice = Rs. 84,000
    assert round(s["rule_37_unpaid_tax"], 2) == 84000.0
    # Sec 17(5) Car repair Rs. 21,600 + Sec 17(1) Personal Catering Rs. 4,000 = Rs. 25,600
    assert round(s["sec_17_5_blocked_tax"], 2) == 25600.0
    # Rule 42 Common Credit Rs. 108,000 * (Exempt Rs. 600,000 / Total Books Turnover)
    assert s["rule_42_reversal_tax"] > 0
    assert s["rule_42_exempt_ratio_pct"] > 0

    # Next-FY Apr-Nov claimed ITC Rs. 90,000 -> Table 8C & Table 13
    assert round(s["table_8c_next_fy_itc"], 2) == 90000.0

    # Rule 88A warning & Books vs Portal Interest variance (7000 - 4500 = 2500)
    assert s["rule88a_warning"] is True
    assert "Rule 88A" in s["rule88a_message"]
    assert round(s["interest_late_fee_diff"], 2) == 2500.0


def test_hsn_threshold_toggle_4_vs_6_digits():
    # Sample has HSN "9018" (4-digit). Should FAIL when >5 Cr, and PASS when <=5 Cr.
    above = run_sample_gstr9_fy2526(turnover_above_5cr=True, cutoff_month=11)
    under = run_sample_gstr9_fy2526(turnover_above_5cr=False, cutoff_month=11)

    t17_above = {r["hsn"]: r for r in above["tables"]["table_17"]}
    t17_under = {r["hsn"]: r for r in under["tables"]["table_17"]}
    assert "9018" in t17_above
    assert t17_above["9018"]["status"] != "VALID"
    assert t17_under["9018"]["status"] == "VALID"
    assert above["summary"]["hsn_total_errors"] > under["summary"]["hsn_total_errors"]


def test_rule42_explicit_exempt_ratio_and_table88a_clean():
    sales_df = pd.DataFrame([
        {
            "Party Name": "Alpha Ltd", "GSTIN": "27AAACR5055K1Z5", "Invoice No": "S-1",
            "Date": "10-08-2025", "Voucher Type": "Sales", "Supply Category": "B2B",
            "HSN": "84713010", "UQC": "NOS", "Qty": 10, "Rate (%)": 18,
            "Taxable": 900000.0, "IGST": 162000.0, "CGST": 0.0, "SGST": 0.0, "Cess": 0.0,
        },
        {
            "Party Name": "Exempt Co", "GSTIN": "", "Invoice No": "E-1",
            "Date": "12-08-2025", "Voucher Type": "Sales", "Supply Category": "Exempt",
            "HSN": "490110", "UQC": "NOS", "Qty": 10, "Rate (%)": 0,
            "Taxable": 100000.0, "IGST": 0.0, "CGST": 0.0, "SGST": 0.0, "Cess": 0.0,
        },
    ])
    pur_df = pd.DataFrame([
        {
            "Party Name": "Cloud Services", "GST No": "27AAACC4120F1Z2", "Invoice no": "P-1",
            "Date": "15-08-2025", "HSN": "998314", "UQC": "OTH", "Qty": 1, "Rate (%)": 18,
            "Taxable": 100000.0, "IGST": 18000.0, "CGST": 0.0, "SGST": 0.0, "Cess": 0.0,
            "ITC Category": "Common Credit Input Services",
        },
    ])
    g2b_df = pd.DataFrame([
        {
            "GSTIN of supplier": "27AAACC4120F1Z2", "Trade/Legal name": "Cloud Services",
            "Invoice number": "P-1", "Invoice Date": "15/08/2025", "Place of supply": "27-Maharashtra",
            "Supply Attract Reverse Charge": "N", "Rate (%)": 18,
            "Taxable Value (₹)": 100000.0, "Integrated Tax (₹)": 18000.0,
            "Central Tax (₹)": 0.0, "State/UT Tax (₹)": 0.0, "Cess (₹)": 0.0, "ITC Availability": "Yes",
        },
    ])
    g3b = empty_gstr3b_structure()
    g3b["outward_3_1"]["a_taxable"] = {"taxable": 900000.0, "igst": 162000.0, "cgst": 0.0, "sgst": 0.0, "cess": 0.0}
    g3b["outward_3_1"]["c_nil_exempt"] = {"taxable": 100000.0, "igst": 0.0, "cgst": 0.0, "sgst": 0.0, "cess": 0.0}
    g3b["itc_4"]["4A_5_all_other"] = {"igst": 18000.0, "cgst": 0.0, "sgst": 0.0, "cess": 0.0}
    # IGST ITC 16200 fully utilized first -> Rule 88A compliant
    g3b["tax_paid_6_1"]["itc_utilized"] = {"igst": 16200.0, "cgst": 0.0, "sgst": 0.0, "cess": 0.0}
    g3b["tax_paid_6_1"]["cash_paid"] = {"igst": 145800.0, "cgst": 0.0, "sgst": 0.0, "cess": 0.0}

    res = compute_gstr9_return(
        fy="2025-26",
        turnover_above_5cr=True,
        cutoff_month=11,
        sales_df=parse_sales_register_df(sales_df, "2025-26", 11),
        gstr1_df=parse_sales_register_df(sales_df, "2025-26", 11),
        gstr3b_data=g3b,
        raw_books_purchase_df=pur_df,
        raw_2b_df=g2b_df,
    )
    s = res["summary"]
    assert round(s["rule_42_exempt_ratio_pct"], 2) == 10.0
    assert round(s["rule_42_reversal_tax"], 2) == 1800.0
    assert round(s["table_7j_net_itc"], 2) == 16200.0
    assert s["rule88a_warning"] is False
    assert round(s["tax_shortfall_excess"], 2) == 0.0


def test_gstr1_multisheet_excel_parser():
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame([
            {
                "GSTIN/UIN of Recipient": "27AAACR5055K1Z5", "Receiver Name": "Reliance Retail",
                "Invoice Number": "B2B-101", "Invoice date": "15-05-2025", "Rate": 18,
                "Taxable Value": 500000.0, "Integrated Tax": 90000.0, "Central Tax": 0.0, "State/UT Tax": 0.0,
            }
        ]).to_excel(writer, sheet_name="b2b", index=False)
        pd.DataFrame([
            {
                "HSN": "84713010", "UQC": "NOS", "Total Quantity": 25, "Rate": 18,
                "Taxable Value": 500000.0, "Integrated Tax Amount": 90000.0, "Central Tax Amount": 0.0, "State/UT Tax Amount": 0.0,
            }
        ]).to_excel(writer, sheet_name="hsn", index=False)

    g1_df, hsn_df = parse_gstr1_files([(buf.getvalue(), "GSTR1_FY2526.xlsx")], fy="2025-26", cutoff_month=11)
    assert len(g1_df) == 1
    assert g1_df.iloc[0]["category"] == "B2B"
    assert round(float(g1_df.iloc[0]["taxable"]), 2) == 500000.0
    assert len(hsn_df) == 1
    assert hsn_df.iloc[0]["hsn"] == "84713010"


def test_fastapi_gstr9_endpoints_and_8_sheet_excel_export():
    # 1. Sample endpoint
    r_sample = client.get("/api/gstr9/sample?fy=2025-26&cutoff_month=11&turnover_above_5cr=true")
    assert r_sample.status_code == 200
    sample_data = r_sample.json()
    assert sample_data["status"] == "success"

    # 2. Export endpoint -> verify 8 openpyxl sheets and live formulas
    r_exp = client.post("/api/gstr9/export", json=sample_data)
    assert r_exp.status_code == 200
    assert "spreadsheetml" in r_exp.headers.get("content-type", "")
    wb = openpyxl.load_workbook(io.BytesIO(r_exp.content), data_only=False)
    expected_sheets = [
        "GSTR-9 Executive Dashboard",
        "Tables 4 & 5 - Outward",
        "Tables 6, 7 & 8 - ITC Bridge",
        "Tables 9 & 10-14 - Tax & Cutoff",
        "Tables 17 & 18 - HSN Summary",
        "Sec 17(5) & Reversal Schedule",
        "Table 8D Vendor Drilldown",
        "GSTR-9C Audit Tie-Out",
    ]
    assert wb.sheetnames == expected_sheets
    out_ws = wb["Tables 4 & 5 - Outward"]
    formulas = [str(cell.value) for row in out_ws.iter_rows() for cell in row if isinstance(cell.value, str) and cell.value.startswith("=")]
    assert len(formulas) > 0, "Expected Excel formulas in Tables 4 & 5 sheet"

    # 3. Compute endpoint with gzip-compressed Sales CSV & JSON GSTR-3B
    sales_csv = (
        "Party Name,GSTIN,Invoice No,Date,Voucher Type,Supply Category,HSN,UQC,Qty,Rate (%),Taxable,IGST,CGST,SGST,Cess\n"
        "Tata Corp,27AAACT2727Q1ZW,INV-01,10-06-2025,Sales,B2B,84713010,NOS,10,18,100000,18000,0,0,0\n"
    ).encode("utf-8")
    sales_gz = gzip.compress(sales_csv)
    cfg = {
        "fy": "2025-26",
        "cutoff_month": 11,
        "turnover_above_5cr": True,
        "manual_overrides": {
            "gstr3b": {
                "outward_3_1": {"a_taxable": {"taxable": 100000.0, "igst": 18000.0, "cgst": 0.0, "sgst": 0.0, "cess": 0.0}}
            }
        },
    }
    r_cmp = client.post(
        "/api/gstr9/compute",
        files={"sales_files": ("sales.csv.gz", sales_gz, "application/gzip")},
        data={"config_json": json.dumps(cfg)},
    )
    assert r_cmp.status_code == 200
    cmp_json = r_cmp.json()
    assert cmp_json["summary"]["books_turnover"] == 100000.0
    assert cmp_json["summary"]["outward_tax_3b"] == 18000.0


def test_zero_emoji_constraint_on_gstr9_files():
    emoji_re = re.compile(
        "[\U0001F300-\U0001F9FF\u2600-\u26FF\u2700-\u27BF]"
    )
    targets = [
        Path("app/engine/gstr9_parsers.py"),
        Path("app/engine/gstr9_engine.py"),
        Path("app/engine/gstr9_exporter.py"),
        Path("app/engine/gstr9_sample.py"),
        Path("app/static/index.html"),
        Path("app/static/js/app.js"),
        Path("app/static/css/style.css"),
    ]
    for p in targets:
        txt = p.read_text(encoding="utf-8")
        assert not emoji_re.search(txt), f"Unexpected emoji found in {p}"
