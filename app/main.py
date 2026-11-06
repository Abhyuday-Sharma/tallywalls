"""
GST Intelligence & Reconciliation Engine - FastAPI Backend.
Powers automated GSTR-2B vs. Books (Purchase Register) matching,
official GST portal automation bridge, and multi-sheet audit exports.
"""

import os
import sys
import json
import asyncio
from typing import Optional, List
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import pandas as pd

from app.engine.column_mapper import detect_columns
from app.engine.reconciler import run_reconciliation
from app.engine.excel_exporter import export_reconciliation_excel
from app.engine.portal_bridge import (
    save_credentials, load_credentials, get_public_profile,
    parse_gstr2b_json, execute_portal_bridge_sync, simulate_portal_download,
    scan_recent_downloads, ingest_gstr2b_file, sanitize_df_for_json,
    get_fy_month_sequence, execute_multi_month_portal_bridge_sync,
    simulate_multi_month_download, consolidate_gstr2b_statements,
    DOWNLOADS_DIR, get_user_downloads_dir
)
from app.engine.tally_connector import (
    check_tally_status, fetch_purchase_vouchers_from_tally, simulate_tally_sync
)

app = FastAPI(
    title="TallyWalls-GST Intelligence & Reconciliation Hub",
    description="Enterprise-grade automated GSTR-2B vs. Books Reconciliation System",
    version="2.4.0"
)

# Enable CORS for local cross-origin development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
SAMPLE_DIR = os.path.join(os.path.dirname(BASE_DIR), "sample_data")

# Mount static files
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _extract_df_from_bytes(contents: bytes, filename: str) -> pd.DataFrame:
    """
    Core resilient extraction of Pandas DataFrame from in-memory file bytes.
    Finance-first: Intelligently handles:
    1. Official GST Portal GSTR-2B workbooks (.xlsx/.xls) via parse_gstr2b_excel
    2. Official GST Portal ZIP archives containing Excel or JSON
    3. ERP purchase registers (Tally Prime, SAP, Busy, Zoho) with title banners,
       continuous date range banners (e.g. '1-Apr-26 to 29-Oct-26'), or 2-tier headers.
    4. Explicitly uses engine='openpyxl' to prevent 'Excel file format cannot be determined'.
    """
    import io
    import zipfile
    from app.engine.portal_bridge import parse_gstr2b_excel, parse_gstr2b_json

    fn_lower = filename.lower()

    if not contents or len(contents) == 0:
        raise ValueError(f"File '{filename}' is empty (0 bytes).")

    # 1. Handle ZIP archives (official GST Portal batch / single download container)
    if fn_lower.endswith(".zip") or (contents.startswith(b"PK\x03\x04") and not fn_lower.endswith((".xlsx", ".xls", ".csv"))):
        try:
            with zipfile.ZipFile(io.BytesIO(contents), "r") as zf:
                excel_entries = [n for n in zf.namelist() if n.lower().endswith((".xlsx", ".xls")) and not n.startswith("__MACOSX")]
                json_entries = [n for n in zf.namelist() if n.lower().endswith(".json")]

                if excel_entries:
                    excel_bytes = zf.read(excel_entries[0])
                    return parse_gstr2b_excel(io.BytesIO(excel_bytes))
                elif json_entries:
                    json_data = json.loads(zf.read(json_entries[0]).decode("utf-8", errors="ignore"))
                    return parse_gstr2b_json(json_data)
        except Exception:
            pass  # Fall through to binary excel / csv handlers

    # 2. Handle JSON file
    if fn_lower.endswith(".json"):
        json_data = json.loads(contents.decode("utf-8", errors="ignore"))
        return parse_gstr2b_json(json_data)

    # 3. Handle Excel Workbooks (.xlsx, .xls) or binary magic bytes
    is_zip_excel = contents.startswith(b"PK\x03\x04")
    is_ole_excel = contents.startswith(b"\xd0\xcf\x11\xe0")
    is_excel = fn_lower.endswith((".xlsx", ".xls")) or is_zip_excel or is_ole_excel

    if is_excel:
        engine = "openpyxl" if (is_zip_excel or fn_lower.endswith(".xlsx")) else "xlrd"
        bio = io.BytesIO(contents)

        # Check sheets
        try:
            xls = pd.ExcelFile(bio, engine=engine)
            sheet_names = [s.lower().strip() for s in xls.sheet_names]

            # If B2B sheet is present, route to parse_gstr2b_excel
            if any("b2b" in s for s in sheet_names):
                bio.seek(0)
                return parse_gstr2b_excel(bio)

            # Not B2B — likely Purchase Register (Books / ERP Ledger)
            target_sheet = None
            for s in xls.sheet_names:
                sl = s.lower()
                if any(k in sl for k in ["purchase", "register", "tally", "ledger"]):
                    target_sheet = s
                    break
            if not target_sheet:
                for s in xls.sheet_names:
                    sl = s.lower()
                    if not any(k in sl for k in ["read", "help", "instruction", "readme"]):
                        target_sheet = s
                        break
            if not target_sheet:
                target_sheet = xls.sheet_names[0]

            bio.seek(0)
            df_raw = pd.read_excel(bio, sheet_name=target_sheet, header=None, engine=engine)

            # Check 2-tier header
            two_tier_idx = None
            for i in range(min(15, len(df_raw) - 1)):
                r_curr = [str(x).lower().strip() for x in df_raw.iloc[i].dropna()]
                r_next = [str(x).lower().strip() for x in df_raw.iloc[i + 1].dropna()]
                has_gstin_curr = any("gstin" in x for x in r_curr)
                has_inv_next = any("invoice" in x or "voucher" in x or "tax" in x for x in r_next)
                if has_gstin_curr and has_inv_next:
                    two_tier_idx = i
                    break

            if two_tier_idx is not None:
                r_top = df_raw.iloc[two_tier_idx]
                r_bot = df_raw.iloc[two_tier_idx + 1]
                combined_cols = []
                for c in range(len(r_top)):
                    vb = str(r_bot[c]).strip() if pd.notna(r_bot[c]) else ""
                    vt = str(r_top[c]).strip() if pd.notna(r_top[c]) else ""
                    if vb and vb.lower() != "nan":
                        combined_cols.append(vb)
                    elif vt and vt.lower() != "nan":
                        combined_cols.append(vt)
                    else:
                        combined_cols.append(f"Col_{c}")
                df = df_raw.iloc[two_tier_idx + 2:].copy()
                df.columns = combined_cols
            else:
                # Single-tier header scan (skips date range banners like '1-Apr-26 to 29-Oct-26', company names, etc.)
                header_row_idx = 0
                max_matches = 0
                key_terms = ["gstin", "invoice", "voucher", "taxable", "integrated", "central", "state", "cess", "supplier", "party", "particulars", "date", "bill"]

                for idx in range(min(15, len(df_raw))):
                    row_vals = [str(v).lower().strip() for v in df_raw.iloc[idx].dropna()]
                    matches = sum(any(term in val for term in key_terms) for val in row_vals)
                    if matches > max_matches and matches >= 2:
                        max_matches = matches
                        header_row_idx = idx

                bio.seek(0)
                if max_matches >= 2:
                    df = pd.read_excel(bio, sheet_name=target_sheet, header=header_row_idx, engine=engine)
                else:
                    df = pd.read_excel(bio, sheet_name=target_sheet, engine=engine)

            df.columns = [str(c).strip() for c in df.columns]
            df = df.dropna(how="all")
            if not df.empty:
                first_col = df.columns[0]
                df = df[~df[first_col].astype(str).str.lower().str.contains("total|grand total", na=False)]
            return df

        except Exception as ex_excel:
            # Fallback 1: Attempt parse_gstr2b_excel directly
            bio.seek(0)
            try:
                return parse_gstr2b_excel(bio)
            except Exception:
                pass
            # Fallback 2: CSV reader in case of misnamed text file
            bio.seek(0)
            try:
                df = pd.read_csv(bio)
                df.columns = [str(c).strip() for c in df.columns]
                return df.dropna(how="all")
            except Exception:
                raise ValueError(f"Excel read failure: {str(ex_excel)}")

    # 4. Fallback to CSV / Delimited text
    bio = io.BytesIO(contents)
    try:
        df = pd.read_csv(bio, encoding="utf-8-sig")
    except UnicodeDecodeError:
        bio.seek(0)
        df = pd.read_csv(bio, encoding="latin1")
    df.columns = [str(c).strip() for c in df.columns]
    return df.dropna(how="all")


async def read_uploaded_file(file: UploadFile) -> pd.DataFrame:
    """
    Asynchronously reads an uploaded UploadFile into a clean Pandas DataFrame.
    Guarantees seek(0) rewinds and structured exception messaging.
    """
    try:
        await file.seek(0)
        contents = await file.read()
    except Exception:
        try:
            file.file.seek(0)
            contents = file.file.read()
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to access stream for '{file.filename}': {str(e)}")

    if not contents or len(contents) == 0:
        raise HTTPException(
            status_code=400,
            detail=f"Uploaded file '{file.filename}' is empty (0 bytes). Please re-attach the valid statutory or books file."
        )

    try:
        return _extract_df_from_bytes(contents, file.filename)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to read file '{file.filename}': {str(e)}")


@app.get("/favicon.ico")
async def favicon():
    return HTMLResponse(content="", status_code=204)


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    """Serve the single-page application dashboard with strict anti-caching headers."""
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        with open(index_file, "r", encoding="utf-8") as f:
            content = f.read()
        return HTMLResponse(
            content=content,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0"
            }
        )
    return HTMLResponse(content="<h1>Dashboard index.html not found.</h1>", status_code=404)


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    import traceback
    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content={"status": "error", "detail": f"Server Notice: {str(exc)}", "message": str(exc)}
    )


@app.post("/api/reconcile")
async def reconcile_files(
    file_2b: List[UploadFile] = File(...),
    file_books: UploadFile = File(...),
    tolerance: float = Form(1.00)
):
    """
    Run multi-tier GST reconciliation on uploaded GSTR-2B and Purchase Register files.
    Seamlessly handles single or multi-month batch statutory statements.
    """
    dfs_2b = []
    for f in file_2b:
        if f.filename:
            df_part = await read_uploaded_file(f)
            if not df_part.empty:
                dfs_2b.append(df_part)

    if not dfs_2b:
        raise HTTPException(status_code=400, detail="Uploaded GSTR-2B statement(s) contain no rows.")

    if len(dfs_2b) == 1:
        df_2b = dfs_2b[0]
    else:
        df_2b = pd.concat(dfs_2b, ignore_index=True)
        # Deduplicate overlapping rows across monthly statements
        gstin_cols = [c for c in df_2b.columns if "gstin" in c.lower()]
        inv_cols = [c for c in df_2b.columns if "invoice" in c.lower() and "number" in c.lower() or c.lower() in ("invoice number", "inum", "invoice no")]
        if gstin_cols and inv_cols:
            df_2b = df_2b.drop_duplicates(subset=[gstin_cols[0], inv_cols[0]], keep="last")

    df_books = await read_uploaded_file(file_books)
    if df_books.empty:
        raise HTTPException(status_code=400, detail="Purchase Register file contains no rows.")

    result = run_reconciliation(df_2b, df_books, tolerance=tolerance)
    return result


@app.get("/api/load-sample")
async def load_sample_reconciliation(tolerance: float = 1.00):
    """Run reconciliation on built-in realistic Indian GST sample datasets."""
    file_2b = os.path.join(SAMPLE_DIR, "sample_gstr2b.xlsx")
    file_books = os.path.join(SAMPLE_DIR, "sample_purchase_register.xlsx")

    if not os.path.exists(file_2b) or not os.path.exists(file_books):
        raise HTTPException(status_code=404, detail="Sample datasets not found.")

    df_2b = pd.read_excel(file_2b, sheet_name="B2B")
    df_books = pd.read_excel(file_books, sheet_name="Purchase Register")

    result = run_reconciliation(df_2b, df_books, tolerance=tolerance)
    return result


@app.post("/api/export")
async def export_excel_report(data: dict):
    """Export reconciliation result as an executive multi-sheet Excel workbook."""
    try:
        excel_stream = export_reconciliation_excel(data)
        headers = {
            "Content-Disposition": "attachment; filename=GST_Reconciliation_Report.xlsx",
            "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        }
        return StreamingResponse(excel_stream, headers=headers)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")


class PortalProfileRequest(BaseModel):
    username: str
    password: str
    gstin: Optional[str] = ""

class PortalFetchRequest(BaseModel):
    financial_year: str
    return_period: Optional[str] = "042026"
    start_month: Optional[str] = "04"
    end_month: Optional[str] = "10"
    batch_mode: Optional[bool] = False
    return_type: Optional[str] = "GSTR-2B"
    username: Optional[str] = ""
    password: Optional[str] = ""
    simulate: Optional[bool] = False

class LocalFileIngestRequest(BaseModel):
    file_path: str

class BatchIngestRequest(BaseModel):
    file_paths: List[str]
    financial_year: Optional[str] = "2026-27"

class DeleteDownloadRequest(BaseModel):
    file_path: str

class TallySyncRequest(BaseModel):
    host: Optional[str] = "localhost"
    port: Optional[int] = 9000
    simulate: Optional[bool] = False


@app.get("/api/portal/profile")
async def get_saved_portal_profile():
    """Retrieve non-sensitive saved profile information."""
    return get_public_profile()


@app.get("/api/portal/saved-creds")
async def get_saved_portal_creds():
    """Retrieve locally stored credentials for autofilling the UI."""
    creds = load_credentials()
    u = creds.get("username", "")
    p = creds.get("password", "")
    # Exclude dummy/example test credentials
    if u.upper() in ("DEMO_USER", "TEST_GST_ACCOUNTANT", "DEMO", "TEST", "SAMPLE_USER"):
        u = ""
        p = ""
    return {
        "username": u,
        "password": p,
        "gstin": creds.get("gstin", ""),
        "has_saved": bool(u and p)
    }


@app.post("/api/portal/save-profile")
async def save_portal_profile(req: PortalProfileRequest):
    """Securely store GST portal credentials locally with AES-256 encryption."""
    if req.username.upper() in ("DEMO_USER", "TEST_GST_ACCOUNTANT", "DEMO", "TEST"):
        return {"status": "success", "message": "Example credentials skipped."}
    ok = save_credentials(req.username, req.password, req.gstin)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to encrypt and store credentials.")
    return {"status": "success", "message": "Portal credentials saved securely on your local machine."}


@app.post("/api/portal/clear-creds")
async def clear_portal_creds():
    """Clear locally stored credentials."""
    from app.engine.portal_bridge import clear_credentials
    ok = clear_credentials()
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to clear credentials.")
    return {"status": "success", "message": "Credentials cleared from local machine."}


@app.post("/api/portal/delete-download")
async def delete_downloaded_file(req: DeleteDownloadRequest):
    """
    Safely delete a downloaded GSTR-2B file from Downloads or app cache directory.
    Validates path to prevent arbitrary file deletion outside download folders.
    """
    file_path = os.path.abspath(req.file_path)
    user_downloads = os.path.abspath(get_user_downloads_dir())
    cache_downloads = os.path.abspath(DOWNLOADS_DIR)

    # Security boundary check
    is_safe = (
        file_path.startswith(user_downloads) or
        file_path.startswith(cache_downloads)
    )

    if not is_safe:
        raise HTTPException(
            status_code=403,
            detail="Permission denied: Target file is outside authorized download directories."
        )

    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found or already deleted.")

    try:
        os.remove(file_path)
        return {
            "status": "success",
            "message": f"File '{os.path.basename(file_path)}' permanently removed from downloads.",
            "file_name": os.path.basename(file_path)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete file: {str(e)}")


@app.get("/api/portal/scan-downloads")
async def scan_downloads_folder():
    """
    Scan the user's Downloads directory and cache for recently downloaded GSTR-2B statements.
    """
    try:
        matches = scan_recent_downloads(max_age_seconds=86400 * 7)
        return {"status": "success", "matches": matches}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to scan downloads: {str(e)}")


@app.post("/api/portal/ingest-local")
async def ingest_local_file(req: LocalFileIngestRequest):
    """
    Ingest a locally detected GSTR-2B statement (.xlsx, .json, or .zip).
    """
    try:
        df = ingest_gstr2b_file(req.file_path)
        if df.empty:
            raise ValueError("The selected statement contains no data or 0 rows.")
        return {
            "status": "success",
            "file_name": os.path.basename(req.file_path),
            "rows_count": len(df),
            "data_preview": sanitize_df_for_json(df)
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ingestion failed: {str(e)}")


@app.post("/api/portal/ingest-batch")
async def ingest_batch_statements(req: BatchIngestRequest):
    """
    Ingest and consolidate multiple locally downloaded monthly statements (e.g. April to October).
    """
    try:
        df = consolidate_gstr2b_statements(req.file_paths, fy=req.financial_year)
        if df.empty:
            raise ValueError("No invoice records could be extracted from the specified files.")
        return {
            "status": "success",
            "batch_mode": True,
            "rows_count": len(df),
            "data_preview": sanitize_df_for_json(df)
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Batch ingestion failed: {str(e)}")


@app.post("/api/portal/fetch-2b")
async def fetch_2b_from_portal(req: PortalFetchRequest):
    """
    Connects to services.gst.gov.in/services/login, autofills credentials,
    synchronizes CAPTCHA, assists navigation to Return Dashboard,
    and captures GSTR-2B via Playwright download or local Downloads folder watcher.
    Supports single month or sequential multi-month batch sync (April to October).
    """
    creds = load_credentials()
    username = req.username or creds.get("username", "")
    password = req.password or creds.get("password", "")

    # Persist non-empty credentials locally with AES-256 encryption (excluding demo/example)
    if req.username and req.password and not req.simulate and req.username.upper() not in ("DEMO", "DEMO_USER", "TEST"):
        save_credentials(req.username, req.password)

    if not username and not req.simulate:
        raise HTTPException(status_code=400, detail="Portal username is required.")

    # Multi-Month Batch Execution Mode
    if req.batch_mode:
        s_m = req.start_month or "04"
        e_m = req.end_month or "10"

        if req.simulate or username.lower() in ("demo", "test", "simulate"):
            df_2b = simulate_multi_month_download(req.financial_year, s_m, e_m)
            return {
                "status": "success",
                "batch_mode": True,
                "source": "SIMULATED_PORTAL_FETCH",
                "message": f"Successfully simulated Multi-Month GSTR-2B fetch across {s_m} to {e_m} (FY {req.financial_year}).",
                "rows_count": len(df_2b),
                "data_preview": sanitize_df_for_json(df_2b)
            }

        try:
            bridge_res = await asyncio.to_thread(
                execute_multi_month_portal_bridge_sync,
                req.financial_year,
                s_m,
                e_m,
                username,
                password,
                480,
                req.return_type or "GSTR-2B"
            )
            return {
                "status": "success",
                "batch_mode": True,
                "source": "LIVE_PORTAL_FETCH",
                "message": f"Multi-Month GSTR-2B statements successfully captured and consolidated across {s_m} to {e_m} (FY {req.financial_year}).",
                "rows_count": bridge_res.get("rows_count", 0),
                "data_preview": bridge_res.get("data_preview", []),
                "captured_files": bridge_res.get("captured_files", [])
            }
        except Exception as be:
            import traceback
            traceback.print_exc()
            err_msg = str(be).strip() or f"{type(be).__name__}: Portal bridge session ended before batch download completed."
            return JSONResponse(
                status_code=500,
                content={"status": "error", "detail": f"Batch Portal bridge note: {err_msg}", "message": err_msg}
            )

    # Single Month Execution Mode
    if req.simulate or username.lower() in ("demo", "test", "simulate"):
        df_2b = simulate_portal_download(req.financial_year, req.return_period or "042026")
        return {
            "status": "success",
            "batch_mode": False,
            "source": "SIMULATED_PORTAL_FETCH",
            "message": f"Successfully simulated GSTR-2B fetch for FY {req.financial_year} ({req.return_period}).",
            "rows_count": len(df_2b),
            "data_preview": sanitize_df_for_json(df_2b)
        }

    try:
        bridge_res = await asyncio.to_thread(
            execute_portal_bridge_sync,
            req.financial_year,
            req.return_period or "042026",
            username,
            password,
            240,
            req.return_type or "GSTR-2B"
        )
        file_path = bridge_res.get("file_path", "")
        df_2b = ingest_gstr2b_file(file_path)

        return {
            "status": "success",
            "batch_mode": False,
            "source": "LIVE_PORTAL_FETCH",
            "message": f"GSTR-2B successfully captured from GST Portal for {req.return_period}.",
            "rows_count": len(df_2b),
            "data_preview": sanitize_df_for_json(df_2b),
            "file_path": file_path
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        err_msg = str(e).strip() or f"{type(e).__name__}: Portal bridge session ended before file download completed."
        return JSONResponse(
            status_code=500,
            content={"status": "error", "detail": f"Portal bridge note: {err_msg}", "message": err_msg}
        )


# -------------------------------------------------------------
# Tally Prime Direct Integration Endpoints
# -------------------------------------------------------------

@app.get("/api/tally/status")
async def get_tally_status(host: str = "localhost", port: int = 9000):
    """Check connectivity to local Tally Prime XML HTTP server."""
    status = check_tally_status(host=host, port=port)
    return status


@app.post("/api/tally/sync")
async def sync_purchase_register_from_tally(req: TallySyncRequest):
    """
    Directly query Purchase Register from Tally Prime on port 9000.
    Eliminates manual export/import steps for the accounting team.
    """
    if req.simulate:
        df_books = simulate_tally_sync()
        return {
            "status": "success",
            "source": "SIMULATED_TALLY_SYNC",
            "message": "Successfully synchronized Purchase Register from Tally Prime (Demo Mode).",
            "rows_count": len(df_books),
            "data_preview": sanitize_df_for_json(df_books)
        }

    try:
        df_books = await asyncio.to_thread(
            fetch_purchase_vouchers_from_tally,
            req.host,
            req.port
        )
        if df_books.empty:
            raise ValueError("No purchase vouchers found in the active Tally Prime company.")

        return {
            "status": "success",
            "source": "LIVE_TALLY_SYNC",
            "message": f"Successfully pulled {len(df_books)} purchase vouchers directly from Tally Prime.",
            "rows_count": len(df_books),
            "data_preview": sanitize_df_for_json(df_books)
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Tally synchronization notice: {str(e)}"
        )


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "service": "TallyWalls-GST Intelligence & Reconciliation Hub",
        "version": "2.4.0"
    }
