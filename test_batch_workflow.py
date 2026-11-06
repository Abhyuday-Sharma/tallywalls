"""
Multi-Month Batch Workflow Verification Test Suite.
Validates sequential statutory period generation (April to October, FY 2026-27),
GSTR-2B consolidation, batch endpoint simulation, and multi-file reconciliation against Tally Books.
"""

import sys
import io
import os
import pandas as pd
from starlette.testclient import TestClient

sys.stdout.reconfigure(encoding="utf-8")

from app.main import app
from app.engine.portal_bridge import (
    get_fy_month_sequence,
    simulate_multi_month_download,
    consolidate_gstr2b_statements,
    sanitize_df_for_json
)

client = TestClient(app)

print("=" * 60)
print("TESTING MULTI-MONTH BATCH GSTR-2B WORKFLOW (APRIL - OCTOBER)")
print("=" * 60)

# Test 1: Month Sequence
seq = get_fy_month_sequence("04", "10", "2026-27")
assert len(seq) == 7, f"Expected 7 months, got {len(seq)}"
expected_names = ["April", "May", "June", "July", "August", "September", "October"]
assert [m["name"] for m in seq] == expected_names, "Month names mismatch in sequence."
assert seq[0]["quarter_label"] == "Quarter 1"
assert seq[3]["quarter_label"] == "Quarter 2"
assert seq[6]["quarter_label"] == "Quarter 3"
print("[PASSED] Month sequence generated perfectly (April to October across Q1, Q2, Q3).")

# Test 2: Multi-Month Simulation
df_multi = simulate_multi_month_download("2026-27", "04", "10")
assert not df_multi.empty, "Simulated multi-month DataFrame is empty."
assert len(df_multi["Return Period"].unique()) == 7, "Return period count mismatch."
print(f"[PASSED] Simulated multi-month dataset generated: {len(df_multi)} invoices across 7 months.")

# Test 3: Batch Fetch Endpoint
fetch_resp = client.post("/api/portal/fetch-2b", json={
    "financial_year": "2026-27",
    "batch_mode": True,
    "start_month": "04",
    "end_month": "10",
    "username": "DEMO_USER",
    "password": "Password@123",
    "simulate": True
})
assert fetch_resp.status_code == 200, f"Batch fetch failed: {fetch_resp.text}"
fetch_data = fetch_resp.json()
assert fetch_data["status"] == "success"
assert fetch_data["batch_mode"] is True
assert fetch_data["rows_count"] == 49
print("[PASSED] /api/portal/fetch-2b batch mode endpoint verified (49 rows returned).")

# Test 4: Multi-File Reconciliation Endpoint
# Create 2 separate monthly GSTR-2B CSV streams and 1 continuous Books CSV stream
b2b_part1 = (
    "GSTIN of supplier,Trade/Legal name,Invoice number,Invoice Date,Taxable Value (₹),Integrated Tax (₹),Central Tax (₹),State/UT Tax (₹)\n"
    "07AACCY1303K1ZH,Yori Labs Private Limited,YL/5/26-27/001,04/05/2026,200000.00,36000.00,0,0\n"
    "07AACCY1303K1ZH,Yori Labs Private Limited,YL/5/26-27/002,14/05/2026,150000.00,27000.00,0,0\n"
)
b2b_part2 = (
    "GSTIN of supplier,Trade/Legal name,Invoice number,Invoice Date,Taxable Value (₹),Integrated Tax (₹),Central Tax (₹),State/UT Tax (₹)\n"
    "07AACCY1303K1ZH,Yori Labs Private Limited,YL/6/26-27/004,25/06/2026,319205.00,57456.90,0,0\n"
    "06AAACB9876C1Z1,Blue Eye Tech,BET/26/091,01/09/2026,100000.00,18000.00,0,0\n"
)

# Continuous books covering 1-Apr-26 to 29-Oct-26
books_data = (
    "GSTIN,Party Name,Invoice No,Date,Taxable,IGST,CGST,SGST\n"
    "07AACCY1303K1ZH,Yori Labs,YL/5/26-27/001,04-May-2026,200000.00,36000.00,0,0\n"
    "07AACCY1303K1ZH,Yori Labs,YL/5/26-27/002,14-May-2026,150000.00,27000.00,0,0\n"
    "07AACCY1303K1ZH,Yori Labs,YL/6/26-27/004,25-Jun-2026,319205.00,57456.90,0,0\n"
    "06AAACB9876C1Z1,Blue Eye Tech,BET/26/091,01-Sep-2026,100000.00,18000.00,0,0\n"
)

files = [
    ("file_2b", ("GSTR2B_May_2026.csv", io.BytesIO(b2b_part1.encode("utf-8")), "text/csv")),
    ("file_2b", ("GSTR2B_June_Sep_2026.csv", io.BytesIO(b2b_part2.encode("utf-8")), "text/csv")),
    ("file_books", ("Tally_Continuous_Purchase_Register.csv", io.BytesIO(books_data.encode("utf-8")), "text/csv")),
]

recon_resp = client.post("/api/reconcile", files=files, data={"tolerance": 1.00})
assert recon_resp.status_code == 200, f"Multi-file reconciliation failed: {recon_resp.text}"
recon_data = recon_resp.json()

assert "summary" in recon_data, "Summary missing from reconciliation result."
assert "kpis" in recon_data, "KPIs missing from reconciliation result."
assert "ledger" in recon_data, "Ledger missing from reconciliation result."
assert recon_data["summary"]["matched_count"] == 4, f"Expected 4 matched invoices, got {recon_data['summary']['matched_count']}"
assert recon_data["summary"]["total_2b_invoices"] == 4, f"Expected 4 total 2B invoices, got {recon_data['summary']['total_2b_invoices']}"
print("[PASSED] Multi-file statutory reconciliation verified! All 4 multi-month invoices matched against Books.")

# Test 5: Batch Ingest Endpoint with Real Cached File
cached_real_file = "app/cache/downloads/GSTR2B_062026_062026_06AAGCH3494L1Z6_GSTR2B_05112026.xlsx"
if os.path.exists(cached_real_file):
    batch_ingest_resp = client.post("/api/portal/ingest-batch", json={
        "file_paths": [cached_real_file],
        "financial_year": "2026-27"
    })
    assert batch_ingest_resp.status_code == 200, f"Batch ingest endpoint failed: {batch_ingest_resp.text}"
    b_data = batch_ingest_resp.json()
    assert b_data["rows_count"] >= 1
    print(f"[PASSED] /api/portal/ingest-batch verified with real downloaded statement ({b_data['rows_count']} rows).")

print("=" * 60)
print("ALL MULTI-MONTH BATCH WORKFLOW TESTS PASSED 100%!")
print("=" * 60)

