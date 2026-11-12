"""
Hybrid Architecture & Hardening Verification Suite.
Tests:
1. O(N) GSTIN-indexed reconciler + slim payload (include_results=False) -> Excel export parity
2. xlrd optional dependency availability and .xls engine selection
3. Cloud mode (VERCEL=1) strict 501 guards on desktop-only routes (no silent fake fallbacks)
4. bridge_agent.py security: CORS + Chrome Private Network Access (PNA), origin whitelist,
   PIN rate limiting, X-Bridge-Token auth, encrypted keystore round-trip, async portal job polling
5. Real subprocess HTTP check for bridge_agent.py --headless --port 9459
"""

import io
import os
import sys
import time
import json
import socket
import subprocess
import urllib.request
import importlib

import pytest
import openpyxl
import pandas as pd
from fastapi.testclient import TestClient

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from app.engine.reconciler import run_reconciliation
from app.engine.excel_exporter import export_reconciliation_excel
import app.main as main_mod
import app.engine.portal_bridge as portal_bridge_mod


def test_slim_reconcile_and_excel_export_parity():
    df_2b = pd.read_excel("sample_data/sample_gstr2b.xlsx", sheet_name="B2B")
    df_books = pd.read_excel("sample_data/sample_purchase_register.xlsx", sheet_name="Purchase Register")

    full_res = run_reconciliation(df_2b, df_books, tolerance=1.0, include_results=True)
    slim_res = run_reconciliation(df_2b, df_books, tolerance=1.0, include_results=False)

    assert "results" in full_res
    assert "results" not in slim_res
    assert slim_res["summary"] == full_res["summary"]
    assert len(slim_res["ledger"]) == len(full_res["ledger"])

    wb_full = openpyxl.load_workbook(export_reconciliation_excel(full_res))
    wb_slim = openpyxl.load_workbook(export_reconciliation_excel(slim_res))

    assert wb_full.sheetnames == wb_slim.sheetnames
    for sheet in wb_full.sheetnames:
        assert wb_full[sheet].max_row == wb_slim[sheet].max_row
        assert wb_full[sheet].max_column == wb_slim[sheet].max_column

    # Verify Sheet 3 (ITC at Risk) invoice value column is preserved accurately in slim mode
    ws_full_s3 = wb_full["Vendor Follow-up (ITC at Risk)"]
    ws_slim_s3 = wb_slim["Vendor Follow-up (ITC at Risk)"]
    for r in range(1, ws_full_s3.max_row + 1):
        vals_full = [ws_full_s3.cell(r, c).value for c in range(1, ws_full_s3.max_column + 1)]
        vals_slim = [ws_slim_s3.cell(r, c).value for c in range(1, ws_slim_s3.max_column + 1)]
        assert vals_full == vals_slim


def test_reconciler_scale_performance():
    rows_2b = []
    rows_books = []
    for i in range(500):
        gstin = f"27AABCT{1000 + (i % 50):04d}K1Z5"
        inv = f"INV/26/{i:04d}"
        rows_2b.append({
            "GSTIN of supplier": gstin,
            "Trade/Legal name": f"Vendor {i % 50}",
            "Invoice number": inv,
            "Invoice Date": "01/05/2026",
            "Taxable Value (₹)": 10000.0,
            "Integrated Tax (₹)": 1800.0,
            "Central Tax (₹)": 0.0,
            "State/UT Tax (₹)": 0.0,
        })
        rows_books.append({
            "GSTIN": gstin,
            "Party Name": f"Vendor {i % 50}",
            "Invoice No": inv,
            "Date": "01-05-2026",
            "Taxable": 10000.0,
            "IGST": 1800.0,
            "CGST": 0.0,
            "SGST": 0.0,
        })

    t0 = time.perf_counter()
    res = run_reconciliation(pd.DataFrame(rows_2b), pd.DataFrame(rows_books), tolerance=1.0, include_results=False)
    elapsed = time.perf_counter() - t0
    assert res["summary"]["matched_count"] == 500
    assert elapsed < 2.5, f"Reconciliation took too long: {elapsed:.2f}s"


def test_xlrd_available_and_legacy_xls_dispatch():
    import xlrd
    assert xlrd.__version__

    # Verify .xls dispatch attempts xlrd rather than failing with Missing optional dependency
    with pytest.raises(ValueError) as exc_info:
        main_mod._extract_df_from_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1corrupt", "legacy.xls")
    assert "Missing optional dependency 'xlrd'" not in str(exc_info.value)


def test_cloud_mode_guards_and_no_fake_fallback(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    client = TestClient(main_mod.app)

    h = client.get("/api/health")
    assert h.status_code == 200
    assert h.json()["environment"] == "cloud"

    ts = client.get("/api/tally/status")
    assert ts.status_code == 200
    t_body = ts.json()
    assert t_body["active"] is False
    assert t_body["cloud"] is True

    # All desktop-only endpoints must return HTTP 501 in cloud mode
    assert client.get("/api/portal/profile").status_code == 501
    assert client.get("/api/portal/saved-creds").status_code == 501
    assert client.post("/api/portal/save-profile", json={"username": "u", "password": "p"}).status_code == 501
    assert client.post("/api/portal/clear-creds").status_code == 501
    assert client.get("/api/portal/scan-downloads").status_code == 501
    assert client.post("/api/portal/ingest-local", json={"file_path": "C:\\dummy.xlsx"}).status_code == 501
    assert client.post("/api/portal/ingest-batch", json={"file_paths": ["C:\\dummy.xlsx"]}).status_code == 501
    assert client.post("/api/portal/delete-download", json={"file_path": "C:\\dummy.xlsx"}).status_code == 501

    # Critical anti-fake-data checks: simulate=False MUST return 501, never 49 demo invoices
    live_portal = client.post("/api/portal/fetch-2b", json={
        "financial_year": "2026-27",
        "batch_mode": True,
        "start_month": "04",
        "end_month": "10",
        "username": "REAL_USER",
        "password": "RealPassword123",
        "simulate": False,
    })
    assert live_portal.status_code == 501

    live_tally = client.post("/api/tally/sync", json={"host": "localhost", "port": 9000, "simulate": False})
    assert live_tally.status_code == 501

    # Explicit simulate=True still works in cloud mode with SIMULATED_* source label
    demo_portal = client.post("/api/portal/fetch-2b", json={
        "financial_year": "2026-27",
        "return_period": "042026",
        "simulate": True,
    })
    assert demo_portal.status_code == 200
    assert demo_portal.json()["source"] == "SIMULATED_PORTAL_FETCH"

    demo_tally = client.post("/api/tally/sync", json={"simulate": True})
    assert demo_tally.status_code == 200
    assert demo_tally.json()["source"] == "SIMULATED_TALLY_SYNC"

    # Reconcile + slim export in cloud mode
    sample_resp = client.get("/api/load-sample")
    assert sample_resp.status_code == 200
    recon_json = sample_resp.json()
    assert "results" not in recon_json
    assert "ledger" in recon_json
    export_resp = client.post("/api/export", json={"summary": recon_json["summary"], "ledger": recon_json["ledger"]})
    assert export_resp.status_code == 200
    assert export_resp.content[:2] == b"PK"


def test_bridge_agent_security_pna_pairing_and_jobs(monkeypatch, tmp_path):
    monkeypatch.setenv("TALLYWALLS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("TALLYWALLS_CACHE_DIR", str(tmp_path / "cache"))

    # Reload portal_bridge first so CRED_FILE / KEY_FILE point to tmp_path/cache
    importlib.reload(portal_bridge_mod)
    import bridge_agent
    importlib.reload(bridge_agent)

    try:
        client = TestClient(bridge_agent.bridge_app)
        bridge_agent._PAIR_FAILURES.clear()

        # 1. /bridge/health open for discovery without leaking token or PIN
        health = client.get("/bridge/health")
        assert health.status_code == 200
        h_json = health.json()
        assert h_json["service"] == "TallyWalls-Connector"
        assert "bridge_token" not in h_json
        assert "pin" not in h_json

        # 2. Chrome Private Network Access + CORS preflight on whitelisted origin
        preflight = client.options(
            "/bridge/portal/saved-creds",
            headers={
                "Origin": "https://tallywalls.vercel.app",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Private-Network": "true",
            },
        )
        assert preflight.status_code == 204
        assert preflight.headers.get("access-control-allow-origin") == "https://tallywalls.vercel.app"
        assert preflight.headers.get("access-control-allow-private-network") == "true"
        assert preflight.headers.get("vary") == "Origin"

        # Preview deployment origin allowed
        preview_ok = client.get(
            "/bridge/health",
            headers={"Origin": "https://tallywalls-git-main-team.vercel.app"},
        )
        assert preview_ok.status_code == 200

        # Untrusted origin blocked with 403
        evil = client.get("/bridge/health", headers={"Origin": "https://evil.example"})
        assert evil.status_code == 403

        # 3. Protected routes require X-Bridge-Token
        unauth = client.get("/bridge/portal/saved-creds")
        assert unauth.status_code == 401
        wrong_tok = client.get("/bridge/portal/saved-creds", headers={"X-Bridge-Token": "wrong-token"})
        assert wrong_tok.status_code == 401

        # 4. Pairing PIN rate-limiting & valid PIN token exchange
        for _ in range(5):
            bad_pin = client.post("/bridge/pair", json={"pin": "000000" if bridge_agent.SECURITY_STATE["pin"] != "000000" else "999999"})
            assert bad_pin.status_code == 401
        throttled = client.post("/bridge/pair", json={"pin": bridge_agent.SECURITY_STATE["pin"]})
        assert throttled.status_code == 429

        bridge_agent._PAIR_FAILURES.clear()
        paired = client.post("/bridge/pair", json={"pin": bridge_agent.SECURITY_STATE["pin"]})
        assert paired.status_code == 200
        token = paired.json()["bridge_token"]
        assert len(token) >= 24

        auth_headers = {"X-Bridge-Token": token, "Origin": "https://tallywalls.vercel.app"}

        # 5. Encrypted keystore round-trip through bridge endpoints
        save_res = client.post(
            "/bridge/portal/save-profile",
            json={"username": "HYBRID_USER", "password": "StrongPassword!1", "gstin": "27AAACU1234F1Z5"},
            headers=auth_headers,
        )
        assert save_res.status_code == 200

        creds_res = client.get("/bridge/portal/saved-creds", headers=auth_headers)
        assert creds_res.status_code == 200
        assert creds_res.json()["username"] == "HYBRID_USER"
        assert creds_res.json()["password"] == "StrongPassword!1"
        assert creds_res.json()["has_saved"] is True

        clear_res = client.post("/bridge/portal/clear-creds", headers=auth_headers)
        assert clear_res.status_code == 200
        assert client.get("/bridge/portal/saved-creds", headers=auth_headers).json()["has_saved"] is False

        # 6. Async portal job start & status polling (simulate=True)
        start_res = client.post(
            "/bridge/portal/fetch-2b/start",
            json={"financial_year": "2026-27", "return_period": "042026", "simulate": True},
            headers=auth_headers,
        )
        assert start_res.status_code == 200
        job_id = start_res.json()["job_id"]

        final_job = None
        for _ in range(30):
            st = client.get(f"/bridge/portal/fetch-2b/status/{job_id}", headers=auth_headers)
            assert st.status_code == 200
            body = st.json()
            if body["status"] == "completed":
                final_job = body
                break
            time.sleep(0.05)

        assert final_job is not None
        assert final_job["result"]["source"] == "SIMULATED_PORTAL_FETCH"
        assert final_job["result"]["rows_count"] > 0

        # 7. Tally simulated sync through bridge
        tally_res = client.post("/bridge/tally/sync", json={"simulate": True}, headers=auth_headers)
        assert tally_res.status_code == 200
        assert tally_res.json()["source"] == "SIMULATED_TALLY_SYNC"
    finally:
        # Restore module state for any subsequent imports
        monkeypatch.delenv("TALLYWALLS_CACHE_DIR", raising=False)
        monkeypatch.delenv("TALLYWALLS_CONFIG_DIR", raising=False)
        importlib.reload(portal_bridge_mod)


def _find_free_port(start: int = 9461) -> int:
    for p in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    raise RuntimeError("No free port found")


def test_bridge_agent_headless_subprocess(tmp_path):
    port = _find_free_port(9461)
    env = os.environ.copy()
    env["TALLYWALLS_CONFIG_DIR"] = str(tmp_path)

    proc = subprocess.Popen(
        [sys.executable, "bridge_agent.py", "--headless", "--no-browser", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        health_payload = None
        for _ in range(40):
            if proc.poll() is not None:
                out, err = proc.communicate(timeout=2)
                raise AssertionError(f"bridge_agent exited prematurely: {out.decode()} {err.decode()}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/bridge/health", timeout=2.0) as resp:
                    if resp.status == 200:
                        health_payload = json.loads(resp.read().decode("utf-8"))
                        break
            except Exception:
                time.sleep(0.2)

        assert health_payload is not None, "Headless bridge server did not become ready in time"
        assert health_payload["service"] == "TallyWalls-Connector"
        assert health_payload["port"] == port
        assert "bridge_token" not in health_payload
        assert "pin" not in health_payload
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)


def test_inbuilt_connector_download_page_and_routes(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    client = TestClient(main_mod.app)

    # 1. Built-in connector download & setup page serves cleanly on all expected routes
    for path in ("/connector", "/connector.html", "/download"):
        resp = client.get(path)
        assert resp.status_code == 200, f"Failed route: {path}"
        assert "Download TallyWalls Connector for Windows" in resp.text
        assert "/api/connector/download" in resp.text
        assert "TallyWallsConnector_Windows.zip" in resp.text
        assert "github.com" not in resp.text.lower()

    # 2. Dashboard index.html no longer sends users to GitHub UI
    idx = client.get("/")
    assert idx.status_code == 200
    assert "/api/connector/download" in idx.text
    assert "/connector" in idx.text
    assert "github.com" not in idx.text.lower()

    # 3. Cloud /api/connector/download issues direct binary attachment redirect (GET & HEAD)
    dl_get = client.get("/api/connector/download", follow_redirects=False)
    assert dl_get.status_code == 302
    assert dl_get.headers["location"].endswith("/releases/latest/download/TallyWallsConnector_Windows.zip")

    dl_head = client.head("/api/connector/download", follow_redirects=False)
    assert dl_head.status_code == 302
    assert dl_head.headers["location"].endswith("/releases/latest/download/TallyWallsConnector_Windows.zip")

    # 4. Byte-for-byte parity between app/static and public/static
    for rel_path in ("index.html", "connector.html", os.path.join("js", "app.js")):
        with open(os.path.join("app", "static", rel_path), "rb") as f1, open(os.path.join("public", "static", rel_path), "rb") as f2:
            assert f1.read() == f2.read(), f"Static sync mismatch: {rel_path}"


def test_supplier_invoice_no_priority_over_voucher_no():
    df_2b = pd.DataFrame([
        {
            "GSTIN of supplier": "27AABCU9603R1ZM",
            "Trade/Legal name": "Apex Fasteners Pvt Ltd",
            "Invoice number": "SUP/25-26/8821",
            "Invoice Date": "12/05/2026",
            "Taxable Value (₹)": 50000.0,
            "Integrated Tax (₹)": 9000.0,
            "Central Tax (₹)": 0.0,
            "State/UT Tax (₹)": 0.0,
        }
    ])
    # Voucher No intentionally comes BEFORE Supplier Invoice No and differs completely
    df_books = pd.DataFrame([
        {
            "Voucher No.": "PUR/26/0001",
            "Party Name": "Apex Fasteners Pvt Ltd",
            "GST No": "27AABCU9603R1ZM",
            "Date": "12-05-2026",
            "Supplier Invoice No.": "SUP/25-26/8821",
            "Taxable": 50000.0,
            "CGST": 0.0,
            "SGST": 0.0,
            "IGST": 9000.0,
        }
    ])
    res = run_reconciliation(df_2b, df_books, tolerance=1.0, include_results=True)
    assert res["summary"]["exact_count"] == 1
    assert res["summary"]["reconciliation_rate"] == 100.0
    assert res["summary"]["matched_tax"] == 9000.0


def test_multi_rate_split_invoice_consolidation():
    # Case A: Books splits 1 invoice across 5% + 18%; Portal has 1 consolidated row
    # Case B: Books has 1 consolidated row; Portal splits across 5% + 18%
    # Case C: Books has 3 item rows (4k@5%, 6k@5%, 20k@18%); Portal has 2 rate rows (10k@5%, 20k@18%)
    # Case D: Multi-rate split in Books with prefix formatting vs leading-zero Portal invoice
    df_2b = pd.DataFrame([
        {
            "GSTIN of supplier": "07AAACU1234F1Z5",
            "Trade/Legal name": "Delhi Electricals Pvt Ltd",
            "Invoice number": "DE/26/101",
            "Invoice Date": "10/05/2026",
            "Taxable Value (₹)": 30000.0,
            "Integrated Tax (₹)": 0.0,
            "Central Tax (₹)": 2050.0,
            "State/UT Tax (₹)": 2050.0,
        },
        {
            "GSTIN of supplier": "27AABCS1429B1Z1",
            "Trade/Legal name": "Sahyadri Polymers LLP",
            "Invoice number": "SP/2026/505",
            "Invoice Date": "14/05/2026",
            "Rate(%)": 5,
            "Taxable Value (₹)": 10000.0,
            "Integrated Tax (₹)": 500.0,
            "Central Tax (₹)": 0.0,
            "State/UT Tax (₹)": 0.0,
        },
        {
            "GSTIN of supplier": "27AABCS1429B1Z1",
            "Trade/Legal name": "Sahyadri Polymers LLP",
            "Invoice number": "SP/2026/505",
            "Invoice Date": "14/05/2026",
            "Rate(%)": 18,
            "Taxable Value (₹)": 20000.0,
            "Integrated Tax (₹)": 3600.0,
            "Central Tax (₹)": 0.0,
            "State/UT Tax (₹)": 0.0,
        },
        {
            "GSTIN of supplier": "07AAACG4412P1Z8",
            "Trade/Legal name": "Ganga Hardware Depot",
            "Invoice number": "GHD/901",
            "Invoice Date": "18/05/2026",
            "Rate(%)": 5,
            "Taxable Value (₹)": 10000.0,
            "Integrated Tax (₹)": 0.0,
            "Central Tax (₹)": 250.0,
            "State/UT Tax (₹)": 250.0,
        },
        {
            "GSTIN of supplier": "07AAACG4412P1Z8",
            "Trade/Legal name": "Ganga Hardware Depot",
            "Invoice number": "GHD/901",
            "Invoice Date": "18/05/2026",
            "Rate(%)": 18,
            "Taxable Value (₹)": 20000.0,
            "Integrated Tax (₹)": 0.0,
            "Central Tax (₹)": 1800.0,
            "State/UT Tax (₹)": 1800.0,
        },
        {
            "GSTIN of supplier": "06AAACH1234D1Z2",
            "Trade/Legal name": "Haryana Precision Forge",
            "Invoice number": "0042",
            "Invoice Date": "22/05/2026",
            "Taxable Value (₹)": 30000.0,
            "Integrated Tax (₹)": 4100.0,
            "Central Tax (₹)": 0.0,
            "State/UT Tax (₹)": 0.0,
        },
    ])

    df_books = pd.DataFrame([
        # Case A (5% + 18% split)
        {"Party Name": "Delhi Electricals Pvt Ltd", "GST No": "07AAACU1234F1Z5", "Date": "10-05-2026", "Invoice no": "DE/26/101", "Rate(%)": 5, "Taxable": 10000.0, "CGST": 250.0, "SGST": 250.0, "IGST": 0.0},
        {"Party Name": "Delhi Electricals Pvt Ltd", "GST No": "07AAACU1234F1Z5", "Date": "10-05-2026", "Invoice no": "DE/26/101", "Rate(%)": 18, "Taxable": 20000.0, "CGST": 1800.0, "SGST": 1800.0, "IGST": 0.0},
        # Case B (combined in Books)
        {"Party Name": "Sahyadri Polymers LLP", "GST No": "27AABCS1429B1Z1", "Date": "14-05-2026", "Invoice no": "SP/2026/505", "Taxable": 30000.0, "CGST": 0.0, "SGST": 0.0, "IGST": 4100.0},
        # Case C (3 item rows in Books vs 2 rate rows in Portal)
        {"Party Name": "Ganga Hardware Depot", "GST No": "07AAACG4412P1Z8", "Date": "18-05-2026", "Invoice no": "GHD/901", "Rate(%)": 5, "Taxable": 4000.0, "CGST": 100.0, "SGST": 100.0, "IGST": 0.0},
        {"Party Name": "Ganga Hardware Depot", "GST No": "07AAACG4412P1Z8", "Date": "18-05-2026", "Invoice no": "GHD/901", "Rate(%)": 5, "Taxable": 6000.0, "CGST": 150.0, "SGST": 150.0, "IGST": 0.0},
        {"Party Name": "Ganga Hardware Depot", "GST No": "07AAACG4412P1Z8", "Date": "18-05-2026", "Invoice no": "GHD/901", "Rate(%)": 18, "Taxable": 20000.0, "CGST": 1800.0, "SGST": 1800.0, "IGST": 0.0},
        # Case D (Normalized prefix vs leading zero)
        {"Party Name": "Haryana Precision Forge", "GST No": "06AAACH1234D1Z2", "Date": "22-05-2026", "Invoice no": "HPF/26-27/0042", "Rate(%)": 5, "Taxable": 10000.0, "CGST": 0.0, "SGST": 0.0, "IGST": 500.0},
        {"Party Name": "Haryana Precision Forge", "GST No": "06AAACH1234D1Z2", "Date": "22-05-2026", "Invoice no": "HPF/26-27/0042", "Rate(%)": 18, "Taxable": 20000.0, "CGST": 0.0, "SGST": 0.0, "IGST": 3600.0},
    ])

    res = run_reconciliation(df_2b, df_books, tolerance=1.0, include_results=True)
    s = res["summary"]
    assert s["reconciliation_rate"] == 100.0
    assert s["missing_in_2b_count"] == 0
    assert s["missing_in_books_count"] == 0
    assert s["value_mismatch_count"] == 0
    assert s["matched_tax"] == 16400.0
    assert s["books_tax_total"] == 16400.0
    assert s["portal_tax_total"] == 16400.0
    assert s["multi_rate_matched_count"] >= 4


def test_multi_rate_value_mismatch_and_duplicate_booking_guard():
    df_2b = pd.DataFrame([
        # Multi-rate invoice with genuine ₹500 taxable difference (29,500 vs 30,000)
        {"GSTIN of supplier": "07AAACU1234F1Z5", "Trade/Legal name": "Metro Cables", "Invoice number": "MC/88", "Invoice Date": "05/05/2026", "Taxable Value (₹)": 29500.0, "Integrated Tax (₹)": 0.0, "Central Tax (₹)": 2005.0, "State/UT Tax (₹)": 2005.0},
        # Single row for duplicate booking test
        {"GSTIN of supplier": "27AAACT0012A1Z8", "Trade/Legal name": "Tata Steel", "Invoice number": "TS/999", "Invoice Date": "06/05/2026", "Taxable Value (₹)": 10000.0, "Integrated Tax (₹)": 1800.0, "Central Tax (₹)": 0.0, "State/UT Tax (₹)": 0.0},
    ])
    df_books = pd.DataFrame([
        {"Party Name": "Metro Cables", "GST No": "07AAACU1234F1Z5", "Date": "05-05-2026", "Invoice no": "MC/88", "Rate(%)": 5, "Taxable": 10000.0, "CGST": 250.0, "SGST": 250.0, "IGST": 0.0},
        {"Party Name": "Metro Cables", "GST No": "07AAACU1234F1Z5", "Date": "05-05-2026", "Invoice no": "MC/88", "Rate(%)": 18, "Taxable": 20000.0, "CGST": 1800.0, "SGST": 1800.0, "IGST": 0.0},
        # Double-entered duplicate in Books
        {"Party Name": "Tata Steel", "GST No": "27AAACT0012A1Z8", "Date": "06-05-2026", "Invoice no": "TS/999", "Taxable": 10000.0, "CGST": 0.0, "SGST": 0.0, "IGST": 1800.0},
        {"Party Name": "Tata Steel", "GST No": "27AAACT0012A1Z8", "Date": "06-05-2026", "Invoice no": "TS/999", "Taxable": 10000.0, "CGST": 0.0, "SGST": 0.0, "IGST": 1800.0},
    ])
    res = run_reconciliation(df_2b, df_books, tolerance=1.0, include_results=True)
    s = res["summary"]
    assert s["value_mismatch_count"] == 1
    assert s["exact_count"] == 1
    assert s["missing_in_2b_count"] == 1
    assert s["itc_at_risk_tax"] == 1800.0

    mismatch_rows = [r for r in res["results"] if r["status"] == "VALUE_MISMATCH"]
    assert mismatch_rows[0]["taxable_diff"] == 500.0
    assert mismatch_rows[0]["tax_diff"] == 90.0

    dup_rows = [r for r in res["results"] if r["status"] == "MISSING_IN_2B"]
    assert "duplicate" in dup_rows[0]["remarks"].lower()


def test_sequential_monthly_fixed_bills_not_false_typo():
    df_2b = pd.DataFrame([
        # Recurring fixed ₹10,000 Apr bill present in 2B only
        {"GSTIN of supplier": "07AAACB0000A1Z0", "Trade/Legal name": "Airtel Lease", "Invoice number": "BB/25-26/04", "Invoice Date": "05-04-2026", "Taxable Value (₹)": 10000.0, "Central Tax (₹)": 900.0, "State/UT Tax (₹)": 900.0, "Integrated Tax (₹)": 0.0},
        # Sequential 3-digit retainer May bill present in 2B only
        {"GSTIN of supplier": "27AAACR5055K1Z3", "Trade/Legal name": "Retainer Corp", "Invoice number": "RET/26/106", "Invoice Date": "01-06-2026", "Taxable Value (₹)": 25000.0, "Integrated Tax (₹)": 4500.0, "Central Tax (₹)": 0.0, "State/UT Tax (₹)": 0.0},
        # Genuine typo transposition (49 vs 94, same date)
        {"GSTIN of supplier": "24AAACA0001B1Z9", "Trade/Legal name": "Asian Paints", "Invoice number": "94", "Invoice Date": "18-05-2026", "Taxable Value (₹)": 12000.0, "Integrated Tax (₹)": 2160.0, "Central Tax (₹)": 0.0, "State/UT Tax (₹)": 0.0},
    ])
    df_books = pd.DataFrame([
        # May bill in Books only (NOT a typo of Apr!)
        {"Party Name": "Airtel Lease", "GST No": "07AAACB0000A1Z0", "Date": "05-05-2026", "Invoice no": "BB/25-26/05", "Taxable": 10000.0, "CGST": 900.0, "SGST": 900.0, "IGST": 0.0},
        # Apr 3-digit retainer in Books only (31 days apart -> NOT a typo!)
        {"Party Name": "Retainer Corp", "GST No": "27AAACR5055K1Z3", "Date": "01-05-2026", "Invoice no": "RET/26/105", "Taxable": 25000.0, "CGST": 0.0, "SGST": 0.0, "IGST": 4500.0},
        {"Party Name": "Asian Paints", "GST No": "24AAACA0001B1Z9", "Date": "18-05-2026", "Invoice no": "49", "Taxable": 12000.0, "CGST": 0.0, "SGST": 0.0, "IGST": 2160.0},
    ])
    res = run_reconciliation(df_2b, df_books, tolerance=1.0, include_results=True)
    s = res["summary"]
    assert s["typo_count"] == 1
    assert s["missing_in_2b_count"] == 2
    assert s["missing_in_books_count"] == 2


def test_ineligible_itc_rcm_and_tax_head_swap_export_parity():
    df_2b = pd.DataFrame([
        # Clean eligible supply
        {"GSTIN of supplier": "07AAACU1234F1Z5", "Trade/Legal name": "Vendor Eligible", "Invoice number": "ELIG/01", "Invoice Date": "04/05/2026", "Taxable Value (₹)": 20000.0, "Central Tax (₹)": 1800.0, "State/UT Tax (₹)": 1800.0, "Integrated Tax (₹)": 0.0, "Supply Attract Reverse Charge": "N", "ITC Availability": "Yes"},
        # Blocked personal/17(5) item marked ITC Availability = No
        {"GSTIN of supplier": "07AAACS1111C1Z9", "Trade/Legal name": "Sugar & Sweets Store", "Invoice number": "BLK/02", "Invoice Date": "08/05/2026", "Taxable Value (₹)": 10000.0, "Central Tax (₹)": 900.0, "State/UT Tax (₹)": 900.0, "Integrated Tax (₹)": 0.0, "Supply Attract Reverse Charge": "N", "ITC Availability": "No"},
        # Reverse charge supply
        {"GSTIN of supplier": "27AAACG7777D1Z1", "Trade/Legal name": "GTA Logistics RCM", "Invoice number": "RCM/03", "Invoice Date": "10/05/2026", "Taxable Value (₹)": 18000.0, "Central Tax (₹)": 0.0, "State/UT Tax (₹)": 0.0, "Integrated Tax (₹)": 900.0, "Supply Attract Reverse Charge": "Y", "ITC Availability": "Yes"},
        # Tax-head swap (Portal has IGST, Books booked CGST+SGST)
        {"GSTIN of supplier": "06AAACB9876C1Z1", "Trade/Legal name": "Swap Components", "Invoice number": "SWP/04", "Invoice Date": "12/05/2026", "Taxable Value (₹)": 10000.0, "Central Tax (₹)": 0.0, "State/UT Tax (₹)": 0.0, "Integrated Tax (₹)": 1800.0, "Supply Attract Reverse Charge": "N", "ITC Availability": "Yes"},
    ])
    df_books = pd.DataFrame([
        {"Party Name": "Vendor Eligible", "GST No": "07AAACU1234F1Z5", "Date": "04-05-2026", "Invoice no": "ELIG/01", "Taxable": 20000.0, "CGST": 1800.0, "SGST": 1800.0, "IGST": 0.0},
        {"Party Name": "Sugar & Sweets Store", "GST No": "07AAACS1111C1Z9", "Date": "08-05-2026", "Invoice no": "BLK/02", "Taxable": 10000.0, "CGST": 900.0, "SGST": 900.0, "IGST": 0.0},
        {"Party Name": "GTA Logistics RCM", "GST No": "27AAACG7777D1Z1", "Date": "10-05-2026", "Invoice no": "RCM/03", "Taxable": 18000.0, "CGST": 0.0, "SGST": 0.0, "IGST": 900.0},
        {"Party Name": "Swap Components", "GST No": "06AAACB9876C1Z1", "Date": "12-05-2026", "Invoice no": "SWP/04", "Taxable": 10000.0, "CGST": 900.0, "SGST": 900.0, "IGST": 0.0},
    ])

    full_res = run_reconciliation(df_2b, df_books, tolerance=1.0, include_results=True)
    slim_res = run_reconciliation(df_2b, df_books, tolerance=1.0, include_results=False)
    s = slim_res["summary"]

    assert s["matched_count"] == 4
    assert s["gross_matched_tax"] == 8100.0
    assert s["ineligible_count"] == 1
    assert s["ineligible_tax"] == 1800.0
    assert s["matched_tax"] == 6300.0  # Ineligible excluded from eligible claimable ITC
    assert s["rcm_count"] == 1
    assert s["rcm_tax"] == 900.0
    assert s["tax_head_mismatch_count"] == 1

    wb_full = openpyxl.load_workbook(export_reconciliation_excel(full_res))
    wb_slim = openpyxl.load_workbook(export_reconciliation_excel(slim_res))
    assert wb_full.sheetnames == wb_slim.sheetnames
    for sheet in wb_full.sheetnames:
        assert wb_full[sheet].max_row == wb_slim[sheet].max_row


def test_multi_month_upload_preserves_multi_rate_split_rows():
    client = TestClient(main_mod.app)
    b2b_apr = (
        "GSTIN of supplier,Trade/Legal name,Invoice number,Invoice Date,Rate(%),Taxable Value (₹),Integrated Tax (₹),Central Tax (₹),State/UT Tax (₹)\n"
        "07AAACU1234F1Z5,Delhi Tools Pvt Ltd,MR/001,10/04/2026,5,10000.0,0,250.0,250.0\n"
        "07AAACU1234F1Z5,Delhi Tools Pvt Ltd,MR/001,10/04/2026,18,20000.0,0,1800.0,1800.0\n"
    )
    b2b_may = (
        "GSTIN of supplier,Trade/Legal name,Invoice number,Invoice Date,Rate(%),Taxable Value (₹),Integrated Tax (₹),Central Tax (₹),State/UT Tax (₹)\n"
        "27AABCS1429B1Z1,Mumbai Alloys,MR/002,15/05/2026,18,15000.0,2700.0,0,0\n"
    )
    books_csv = (
        "Party Name,GST No,Date,Invoice no,Taxable,CGST,SGST,IGST\n"
        "Delhi Tools Pvt Ltd,07AAACU1234F1Z5,10-04-2026,MR/001,30000.0,2050.0,2050.0,0\n"
        "Mumbai Alloys,27AABCS1429B1Z1,15-05-2026,MR/002,15000.0,0,0,2700.0\n"
    )
    files = [
        ("file_2b", ("GSTR2B_Apr_2026.csv", io.BytesIO(b2b_apr.encode("utf-8")), "text/csv")),
        ("file_2b", ("GSTR2B_May_2026.csv", io.BytesIO(b2b_may.encode("utf-8")), "text/csv")),
        ("file_books", ("Books_2026.csv", io.BytesIO(books_csv.encode("utf-8")), "text/csv")),
    ]
    resp = client.post("/api/reconcile", files=files, data={"tolerance": 1.00})
    assert resp.status_code == 200
    s = resp.json()["summary"]
    assert s["matched_count"] == 2
    assert s["reconciliation_rate"] == 100.0
    assert s["matched_tax"] == 6800.0


