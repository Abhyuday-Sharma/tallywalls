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

