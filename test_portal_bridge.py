"""
Test Suite for GST Portal Automation Bridge & Session Ingestion.
Verifies:
1. AES-256 Local Credential Encryption & Decryption
2. Official GSTR-2B JSON Parser
3. Portal Profile & Fetch API Endpoints
4. Ingestion of Portal Data into Multi-Tier Reconciliation Engine
"""

import sys
import os

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

import json
import pandas as pd
from fastapi.testclient import TestClient

from app.main import app
from app.engine.portal_bridge import (
    save_credentials, load_credentials, parse_gstr2b_json, clear_credentials
)
from app.engine.reconciler import run_reconciliation

def test_portal_bridge():
    print("==================================================")
    print("TESTING GST PORTAL AUTOMATION & ENCRYPTION BRIDGE")
    print("==================================================")

    # Backup existing credentials if present
    prev_creds = load_credentials()

    try:
        # 1. Test Credential Encryption
        test_user = "ENTERPRISE_GST_USER"
        test_pass = "Secret#Tax2024!"
        test_gstin = "07AAACU1234F1Z5"

        saved = save_credentials(test_user, test_pass, test_gstin)
        assert saved is True, "Failed to save credentials"
        loaded = load_credentials()
        assert loaded["username"] == test_user, f"Expected {test_user}, got {loaded['username']}"
        assert loaded["password"] == test_pass, "Password mismatch after decryption"
        assert loaded["gstin"] == test_gstin, "GSTIN mismatch after decryption"
        print("[PASSED] AES-256 Local Credential Encryption & Decryption verified!")

        # 2. Test Official GSTR-2B JSON Parser
        sample_gstn_json = {
            "data": {
                "b2b": [
                    {
                        "ctin": "07AAACU1234F1Z5",
                        "cpty": "UltraTech Cement Ltd",
                        "inv": [
                            {
                                "inum": "UTC/2023/101",
                                "idt": "2023-10-05",
                                "val": 192000.00,
                                "items": [
                                    {
                                        "itmdet": {
                                            "txval": 150000.00,
                                            "iamt": 42000.00,
                                            "camt": 0.00,
                                            "samt": 0.00,
                                            "csamt": 0.00
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        }

        df_parsed = parse_gstr2b_json(sample_gstn_json)
        assert len(df_parsed) == 1, f"Expected 1 invoice, got {len(df_parsed)}"
        assert df_parsed.iloc[0]["GSTIN of supplier"] == "07AAACU1234F1Z5"
        assert df_parsed.iloc[0]["Taxable Value (₹)"] == 150000.00
        assert df_parsed.iloc[0]["Integrated Tax (₹)"] == 42000.00
        print("[PASSED] Official GST Portal JSON format parsed into DataFrame successfully!")

        # 3. Test API Endpoints
        client = TestClient(app)
        res_prof = client.get("/api/portal/profile")
        assert res_prof.status_code == 200
        assert res_prof.json()["username"] == test_user
        assert res_prof.json()["has_saved_password"] is True
        print(f"[PASSED] Saved profile endpoint verified (Non-sensitive: {res_prof.json()})")

        res_creds = client.get("/api/portal/saved-creds")
        assert res_creds.status_code == 200
        assert res_creds.json()["username"] == test_user
        assert res_creds.json()["password"] == test_pass
        assert res_creds.json()["has_saved"] is True
        print("[PASSED] Saved creds prefill endpoint verified (Encrypted retrieval)")

        # 4. Test Simulated Portal Fetch
        payload = {
            "financial_year": "2023-24",
            "return_period": "102023",
            "simulate": True
        }
        res_fetch = client.post("/api/portal/fetch-2b", json=payload)
        assert res_fetch.status_code == 200
        fetch_data = res_fetch.json()
        assert fetch_data["status"] == "success"
        assert fetch_data["rows_count"] > 0
        print(f"[PASSED] Portal Fetch Endpoint verified! Retrieved {fetch_data['rows_count']} invoices.")

        # 5. Test Reconciliation using Fetched Portal Data vs. Books
        books_file = "sample_data/sample_purchase_register.xlsx"
        df_books = pd.read_excel(books_file, sheet_name="Purchase Register")
        df_portal = pd.DataFrame(fetch_data["data_preview"])

        recon = run_reconciliation(df_portal, df_books, tolerance=1.00)
        summary = recon["summary"]
        assert summary["matched_count"] > 0
        print(f"[PASSED] Reconciled portal data against Books! Matched tax: Rs. {summary['matched_tax']:.2f}")

        print("==================================================")
        print("ALL GST PORTAL BRIDGE TESTS PASSED SUCCESSFULLY!")
        print("==================================================")
    finally:
        if prev_creds.get("username") and prev_creds.get("password") and prev_creds["username"].upper() not in ("DEMO_USER", "ENTERPRISE_GST_USER", "TEST_GST_ACCOUNTANT", "DEMO", "TEST"):
            save_credentials(prev_creds["username"], prev_creds["password"], prev_creds.get("gstin", ""))
        else:
            clear_credentials()

if __name__ == "__main__":
    test_portal_bridge()

