"""
Automated End-to-End Test for GST Reconciliation Engine.
Verifies all deliberate test cases:
- Exact Match
- Leading Zeros Normalization (0049 vs 49)
- Prefix/Suffix Normalization (INV/2023-24/108 vs 108)
- Probable Typo Detection (49 vs 94)
- Value Mismatch Variance
- Missing in 2B (ITC at Risk)
- Missing in Books (Unbooked)
- Multi-sheet Excel export generation
"""

import sys
import os

# Ensure UTF-8 output on Windows console
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd
import openpyxl

from app.engine.reconciler import run_reconciliation
from app.engine.excel_exporter import export_reconciliation_excel

def test_full_pipeline():
    print("==================================================")
    print("RUNNING GST RECONCILIATION PIPELINE TEST")
    print("==================================================")

    file_2b = "sample_data/sample_gstr2b.xlsx"
    file_books = "sample_data/sample_purchase_register.xlsx"

    assert os.path.exists(file_2b), f"Missing {file_2b}"
    assert os.path.exists(file_books), f"Missing {file_books}"

    df_2b = pd.read_excel(file_2b, sheet_name="B2B")
    df_books = pd.read_excel(file_books, sheet_name="Purchase Register")

    print(f"Loaded 2B Rows: {len(df_2b)}, Books Rows: {len(df_books)}")

    recon = run_reconciliation(df_2b, df_books, tolerance=1.00)
    summary = recon["summary"]
    results = recon["results"]

    print("\n--- Summary Metrics ---")
    for k, v in summary.items():
        print(f"  {k}: {v}")

    print("\n--- Detailed Results ---")
    for idx, r in enumerate(results, 1):
        status = r["status"]
        books_inv = r["books"]["raw_inv"] if r["books"] else "NONE"
        portal_inv = r["portal"]["raw_inv"] if r["portal"] else "NONE"
        party = (r["books"] or r["portal"])["party_name"]
        print(f"[{idx}] {status:<20} | {party:<32} | Books: {books_inv:<18} | 2B: {portal_inv:<15} | {r['remarks']}")

    # Assertions
    assert summary["exact_count"] >= 1, f"Expected exact matches, got {summary['exact_count']}"
    assert summary["normalized_count"] >= 1, f"Expected normalized matches, got {summary['normalized_count']}"
    assert summary["typo_count"] >= 1, f"Expected typo matches, got {summary['typo_count']}"
    assert summary["value_mismatch_count"] >= 1, f"Expected value mismatch, got {summary['value_mismatch_count']}"
    assert summary["missing_in_2b_count"] >= 1, f"Expected missing in 2B, got {summary['missing_in_2b_count']}"
    assert summary["missing_in_books_count"] >= 1, f"Expected missing in books, got {summary['missing_in_books_count']}"

    print("\n[PASSED] All reconciliation logic assertions passed!")

    # Test Excel Export
    print("\nTesting Excel Export...")
    excel_stream = export_reconciliation_excel(recon)
    wb = openpyxl.load_workbook(excel_stream)
    expected_sheets = ["Executive Summary", "Reconciled Ledger", "Vendor Follow-up (ITC at Risk)", "Unbooked Invoices"]
    assert wb.sheetnames == expected_sheets, f"Expected sheets {expected_sheets}, got {wb.sheetnames}"
    print(f"[PASSED] Excel export generated successfully with sheets: {wb.sheetnames}")
    print("==================================================")
    print("ALL TESTS PASSED SUCCESSFULLY!")
    print("==================================================")

if __name__ == "__main__":
    test_full_pipeline()
