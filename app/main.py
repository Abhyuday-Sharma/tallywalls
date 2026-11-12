"""
GST Intelligence & Reconciliation Engine - FastAPI Backend.
Powers automated GSTR-2B vs. Books (Purchase Register) matching,
official GST portal automation bridge, and multi-sheet audit exports.
"""

import os
import sys

# Ensure repository root and app directory are always in sys.path
_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_CURRENT_DIR)
for _d in [_PARENT_DIR, _CURRENT_DIR]:
    if _d not in sys.path:
        sys.path.insert(0, _d)

import gzip
import json
import asyncio
from typing import Optional, List
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import pandas as pd

from app.engine.column_mapper import detect_columns
from app.engine.reconciler import run_reconciliation
from app.engine.excel_exporter import export_reconciliation_excel
from app.engine.gstr9_parsers import (
    extract_generic_files_to_df,
    parse_gstr1_files,
    parse_gstr3b_inputs,
    parse_sales_register_df,
)
from app.engine.gstr9_engine import compute_gstr9_return
from app.engine.gstr9_exporter import export_gstr9_excel
from app.engine.gstr9_sample import run_sample_gstr9_fy2526
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
    version="2.4.0",
    redirect_slashes=False
)

# Resilient Vercel serverless request path normalizer middleware
@app.middleware("http")
async def vercel_path_normalizer(request: Request, call_next):
    # Check if __path__ was passed via query parameters by Vercel rewrite
    path_param = request.query_params.get("__path__")
    if path_param:
        while path_param.startswith("//"):
            path_param = path_param[1:]
        if not path_param.startswith("/"):
            path_param = "/" + path_param
            
        request.scope["path"] = path_param
        request.scope["raw_path"] = path_param.encode("utf-8")
        
        # Strip __path__ from query_string so it doesn't pollute endpoint query params
        raw_qs = request.scope.get("query_string", b"")
        qs_str = raw_qs.decode("latin-1") if isinstance(raw_qs, bytes) else str(raw_qs)
        import urllib.parse
        parsed_q = urllib.parse.parse_qsl(qs_str, keep_blank_values=True)
        filtered_q = [(k, v) for k, v in parsed_q if k != "__path__"]
        new_qs = urllib.parse.urlencode(filtered_q)
        request.scope["query_string"] = new_qs.encode("latin-1")
    else:
        orig_path = request.headers.get("x-matched-path") or request.headers.get("x-invoke-path")
        if orig_path and not any(ch in orig_path for ch in ("*", "(", ")", "$")) and not orig_path.endswith("index.py"):
            if "?" in orig_path:
                orig_path = orig_path.split("?")[0]
            request.scope["path"] = orig_path
            request.scope["raw_path"] = orig_path.encode("utf-8")
        else:
            raw_path = request.scope.get("path", "")
            if raw_path in ("/api/index.py", "/api/index", "/api", "/api/"):
                request.scope["path"] = "/"
                request.scope["raw_path"] = b"/"
            elif raw_path.startswith("/api/index.py/"):
                new_p = raw_path[len("/api/index.py"):]
                request.scope["path"] = new_p
                request.scope["raw_path"] = new_p.encode("utf-8")
            elif raw_path.startswith("/api/index/"):
                new_p = raw_path[len("/api/index"):]
                request.scope["path"] = new_p
                request.scope["raw_path"] = new_p.encode("utf-8")
                
    response = await call_next(request)
    return response

def _is_cloud_env() -> bool:
    """Detect whether the FastAPI app is running in a cloud/serverless container (e.g., Vercel)."""
    return bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


# Enable CORS without insecure wildcard + credentials combination
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def _find_dir(dir_name: str, fallback_relative: str) -> str:
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.extend([
            os.path.join(meipass, "app", dir_name),
            os.path.join(meipass, dir_name)
        ])
    candidates.extend([
        os.path.join(BASE_DIR, dir_name),
        os.path.join(os.path.dirname(BASE_DIR), dir_name),
        os.path.join(os.getcwd(), dir_name),
        os.path.join(os.getcwd(), "app", dir_name)
    ])
    for c in candidates:
        if os.path.isdir(c):
            return c
    return os.path.normpath(os.path.join(BASE_DIR, fallback_relative))

STATIC_DIR = _find_dir("static", "static")
SAMPLE_DIR = _find_dir("sample_data", os.path.join("..", "sample_data"))

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
                df = df[~df[first_col].astype(str).str.strip().str.lower().str.match(r"^(?:grand\s+|sub\s*[-\s]*)?total(?:\s*[:\-].*)?$", na=False)]
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
@app.get("/index.html", response_class=HTMLResponse)
@app.get("/api", response_class=HTMLResponse)
@app.get("/api/", response_class=HTMLResponse)
@app.get("/api/index", response_class=HTMLResponse)
@app.get("/api/index/", response_class=HTMLResponse)
@app.get("/api/index.py", response_class=HTMLResponse)
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


@app.get("/gst_financial_terms_guide.html", response_class=HTMLResponse)
async def serve_terms_guide():
    """Serve statutory GST financial definitions and reconciliation rulebook."""
    candidates = [
        os.path.join(getattr(sys, "_MEIPASS", ""), "gst_financial_terms_guide.html"),
        os.path.join(os.path.dirname(BASE_DIR), "gst_financial_terms_guide.html"),
        os.path.join(os.getcwd(), "gst_financial_terms_guide.html"),
        os.path.join(STATIC_DIR, "gst_financial_terms_guide.html")
    ]
    for p in candidates:
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>Statutory Terms Guide Not Found</h1>", status_code=404)


@app.get("/connector", response_class=HTMLResponse)
@app.get("/connector.html", response_class=HTMLResponse)
@app.get("/download", response_class=HTMLResponse)
async def serve_connector_page():
    """Serve the built-in Windows Connector download and 3-step setup page."""
    connector_file = os.path.join(STATIC_DIR, "connector.html")
    if os.path.exists(connector_file):
        with open(connector_file, "r", encoding="utf-8") as f:
            return HTMLResponse(
                content=f.read(),
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                    "Expires": "0",
                },
            )
    return HTMLResponse(content="<h1>Connector download page not found.</h1>", status_code=404)


DEFAULT_CONNECTOR_DOWNLOAD_URL = (
    "https://github.com/Abhyuday-Sharma/tallywalls/releases/latest/download/TallyWallsConnector_Windows.zip"
)


@app.api_route("/api/connector/download", methods=["GET", "HEAD"])
async def download_connector_archive():
    """
    Direct download endpoint for TallyWallsConnector_Windows.zip.
    Locally serves the built zip if present on disk; on cloud hosts redirects directly
    to the binary attachment CDN (Content-Disposition: attachment) without showing GitHub UI.
    """
    if not _is_cloud_env():
        local_candidates = [
            os.path.join(_PARENT_DIR, "dist", "TallyWallsConnector_Windows.zip"),
            os.path.join(os.getcwd(), "dist", "TallyWallsConnector_Windows.zip"),
        ]
        for candidate in local_candidates:
            if os.path.isfile(candidate):
                return FileResponse(
                    path=candidate,
                    filename="TallyWallsConnector_Windows.zip",
                    media_type="application/zip",
                )

    target_url = os.environ.get("TALLYWALLS_CONNECTOR_DOWNLOAD_URL", DEFAULT_CONNECTOR_DOWNLOAD_URL)
    return RedirectResponse(
        url=target_url,
        status_code=302,
        headers={"Cache-Control": "no-store"},
    )


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
        # Tag each file index before concatenation so overlapping monthly statements
        # deduplicate cleanly without dropping intra-statement multi-rate split rows.
        tagged_dfs = []
        for f_idx, d in enumerate(dfs_2b):
            dc = d.copy()
            dc["_tw_src_file_idx"] = f_idx
            tagged_dfs.append(dc)
        df_2b = pd.concat(tagged_dfs, ignore_index=True)

        gstin_cols = [c for c in df_2b.columns if "gstin" in c.lower() or "gst no" in c.lower()]
        inv_cols = [
            c for c in df_2b.columns
            if ("invoice" in c.lower() and "number" in c.lower())
            or c.lower().strip() in ("invoice number", "inum", "invoice no")
        ]
        if gstin_cols and inv_cols:
            g_col, i_col = gstin_cols[0], inv_cols[0]
            latest_file_idx = df_2b.groupby([g_col, i_col], dropna=False)["_tw_src_file_idx"].transform("max")
            df_2b = df_2b[df_2b["_tw_src_file_idx"] == latest_file_idx]
        df_2b = df_2b.drop(columns=["_tw_src_file_idx"], errors="ignore")

    df_books = await read_uploaded_file(file_books)
    if df_books.empty:
        raise HTTPException(status_code=400, detail="Purchase Register file contains no rows.")

    result = run_reconciliation(df_2b, df_books, tolerance=tolerance, include_results=False)
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

    result = run_reconciliation(df_2b, df_books, tolerance=tolerance, include_results=False)
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
    save_creds: Optional[bool] = False
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


def _require_local_desktop(feature_name: str):
    if _is_cloud_env():
        raise HTTPException(
            status_code=501,
            detail=(
                f"{feature_name} requires a local Windows environment and cannot execute on cloud servers. "
                "Please install and start TallyWalls Connector on your PC to enable this feature from the website."
            )
        )


@app.get("/api/portal/profile")
async def get_saved_portal_profile():
    """Retrieve non-sensitive saved profile information."""
    _require_local_desktop("Local GST credential storage")
    return get_public_profile()


@app.get("/api/portal/saved-creds")
async def get_saved_portal_creds():
    """Retrieve locally stored credentials for autofilling the UI."""
    _require_local_desktop("Local GST credential storage")
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
    _require_local_desktop("Local GST credential storage")
    if req.username.upper() in ("DEMO_USER", "TEST_GST_ACCOUNTANT", "DEMO", "TEST"):
        return {"status": "success", "message": "Example credentials skipped."}
    ok = save_credentials(req.username, req.password, req.gstin)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to encrypt and store credentials.")
    return {"status": "success", "message": "Portal credentials saved securely on your local machine."}


@app.post("/api/portal/clear-creds")
async def clear_portal_creds():
    """Clear locally stored credentials."""
    _require_local_desktop("Local GST credential storage")
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
    _require_local_desktop("Local Downloads folder management")
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
    _require_local_desktop("Local Downloads folder scanner")
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
    _require_local_desktop("Local GSTR-2B file ingestion")
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
    _require_local_desktop("Local batch GSTR-2B ingestion")
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
    In cloud mode, live portal execution is rejected with HTTP 501; only explicit
    simulation (`simulate=True`) returns demo rows.
    """
    is_cloud = _is_cloud_env()

    # Explicit simulation / demo mode (must be opted into explicitly)
    if req.simulate:
        if req.batch_mode:
            s_m = req.start_month or "04"
            e_m = req.end_month or "10"
            df_2b = simulate_multi_month_download(req.financial_year, s_m, e_m)
            return {
                "status": "success",
                "batch_mode": True,
                "source": "SIMULATED_PORTAL_FETCH",
                "message": f"Successfully simulated Multi-Month GSTR-2B fetch across {s_m} to {e_m} (FY {req.financial_year}).",
                "rows_count": len(df_2b),
                "data_preview": sanitize_df_for_json(df_2b)
            }
        df_2b = simulate_portal_download(req.financial_year, req.return_period or "042026")
        return {
            "status": "success",
            "batch_mode": False,
            "source": "SIMULATED_PORTAL_FETCH",
            "message": f"Successfully simulated GSTR-2B fetch for FY {req.financial_year} ({req.return_period}).",
            "rows_count": len(df_2b),
            "data_preview": sanitize_df_for_json(df_2b)
        }

    # Live GST Portal execution cannot happen inside a cloud serverless container
    if is_cloud:
        raise HTTPException(
            status_code=501,
            detail=(
                "Live GST Portal browser automation requires TallyWalls Connector running on your Windows PC. "
                "Launch TallyWalls Connector or switch to Simulate Fetch / Manual File Upload."
            )
        )

    creds = load_credentials()
    username = (req.username or "").strip() or creds.get("username", "")
    password = req.password or creds.get("password", "")

    # Persist credentials only when user explicitly opted in via save_creds=True
    if req.save_creds and req.username and req.password and req.username.upper() not in ("DEMO", "DEMO_USER", "TEST"):
        save_credentials(req.username, req.password)

    if not username:
        raise HTTPException(status_code=400, detail="Portal username is required.")

    # Multi-Month Batch Execution Mode (Local desktop)
    if req.batch_mode:
        s_m = req.start_month or "04"
        e_m = req.end_month or "10"
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
                content={"status": "error", "detail": f"Batch Portal bridge error: {err_msg}", "message": err_msg}
            )

    # Single Month Execution Mode (Local desktop)
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
            content={"status": "error", "detail": f"Portal bridge error: {err_msg}", "message": err_msg}
        )


# -------------------------------------------------------------
# Tally Prime Direct Integration Endpoints
# -------------------------------------------------------------

@app.get("/api/tally/status")
async def get_tally_status(host: str = "localhost", port: int = 9000):
    """Check connectivity to local Tally Prime XML HTTP server."""
    if _is_cloud_env():
        return {
            "active": False,
            "cloud": True,
            "host": host,
            "port": port,
            "company": "",
            "message": "Cloud web dashboard requires TallyWalls Connector on your Windows PC to reach localhost:9000."
        }
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

    _require_local_desktop("Live Tally Prime Port 9000 sync")

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
    except ConnectionError as ce:
        raise HTTPException(
            status_code=503,
            detail=f"Tally Prime unreachable: {str(ce)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Tally synchronization failed: {str(e)}"
        )


async def _read_optional_uploads(files: Optional[List[UploadFile]]) -> List[tuple]:
    """Read optional multipart UploadFiles, transparently decompressing .gz payloads."""
    out = []
    for f in files or []:
        if not f or not f.filename:
            continue
        raw = await f.read()
        if not raw:
            continue
        fname = f.filename
        if fname.lower().endswith(".gz"):
            raw = gzip.decompress(raw)
            fname = fname[:-3]
        out.append((raw, fname))
    return out


# -------------------------------------------------------------
# GSTR-9 Annual Return Hub Endpoints
# -------------------------------------------------------------

@app.post("/api/gstr9/compute")
async def compute_gstr9_endpoint(
    sales_files: Optional[List[UploadFile]] = File(default=[]),
    gstr1_files: Optional[List[UploadFile]] = File(default=[]),
    gstr3b_files: Optional[List[UploadFile]] = File(default=[]),
    purchase_files: Optional[List[UploadFile]] = File(default=[]),
    gstr2b_files: Optional[List[UploadFile]] = File(default=[]),
    config_json: str = Form("{}"),
):
    """
    Compute full-year GSTR-9 statutory Tables 4-18 & GSTR-9C tie-out.
    Supports incremental partial uploads (Outward-only, Inward-only, or Complete FY)
    plus manual GSTR-3B / Rule 37 / Rule 42 / Sec 17(5) overrides in config_json.
    """
    try:
        cfg = json.loads(config_json or "{}")
    except Exception:
        cfg = {}

    fy = str(cfg.get("fy") or "2025-26")
    cutoff_month = int(cfg.get("cutoff_month") or 11)
    turnover_above_5cr = bool(cfg.get("turnover_above_5cr", True))
    overrides = cfg.get("manual_overrides") or {}

    s_tuples = await _read_optional_uploads(sales_files)
    g1_tuples = await _read_optional_uploads(gstr1_files)
    g3b_tuples = await _read_optional_uploads(gstr3b_files)
    pur_tuples = await _read_optional_uploads(purchase_files)
    g2b_tuples = await _read_optional_uploads(gstr2b_files)

    raw_sales = extract_generic_files_to_df(s_tuples, is_portal_2b=False) if s_tuples else pd.DataFrame()
    sales_df = parse_sales_register_df(raw_sales, fy=fy, cutoff_month=cutoff_month) if not raw_sales.empty else pd.DataFrame()

    g1_df, g1_hsn_df = parse_gstr1_files(g1_tuples, fy=fy, cutoff_month=cutoff_month) if g1_tuples else (pd.DataFrame(), pd.DataFrame())
    g3b_data = parse_gstr3b_inputs(g3b_tuples, manual_overrides=overrides)
    pur_df = extract_generic_files_to_df(pur_tuples, is_portal_2b=False) if pur_tuples else pd.DataFrame()
    g2b_df = extract_generic_files_to_df(g2b_tuples, is_portal_2b=True) if g2b_tuples else pd.DataFrame()

    if sales_df.empty and g1_df.empty and pur_df.empty and g2b_df.empty and not overrides.get("gstr3b"):
        raise HTTPException(
            status_code=400,
            detail="Please upload at least one statutory statement (Sales Register, GSTR-1, GSTR-3B, Purchase Register, or GSTR-2B) or enter GSTR-3B figures."
        )

    result = compute_gstr9_return(
        fy=fy,
        turnover_above_5cr=turnover_above_5cr,
        cutoff_month=cutoff_month,
        sales_df=sales_df,
        gstr1_df=g1_df,
        gstr1_hsn_df=g1_hsn_df,
        gstr3b_data=g3b_data,
        raw_books_purchase_df=pur_df,
        raw_2b_df=g2b_df,
        manual_overrides=overrides,
    )
    return result


@app.get("/api/gstr9/sample")
async def load_sample_gstr9(fy: str = "2025-26", turnover_above_5cr: bool = True, cutoff_month: int = 11):
    """Run and return a complete 12-month FY 2025-26 GSTR-9 sample calculation."""
    return run_sample_gstr9_fy2526(turnover_above_5cr=turnover_above_5cr, cutoff_month=cutoff_month)


@app.post("/api/gstr9/export")
async def export_gstr9_working_paper(data: dict):
    """Stream the 8-sheet CA-ready GSTR-9 Working Paper (.xlsx)."""
    try:
        fy = (data.get("summary") or {}).get("financial_year", "2025-26").replace("/", "-")
        excel_stream = export_gstr9_excel(data)
        headers = {
            "Content-Disposition": f"attachment; filename=TallyWalls_GSTR9_Working_Paper_FY{fy}.xlsx",
            "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }
        return StreamingResponse(excel_stream, headers=headers)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"GSTR-9 Excel export failed: {str(e)}")


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "service": "TallyWalls-GST Intelligence & Reconciliation Hub",
        "version": "2.4.0",
        "environment": "cloud" if _is_cloud_env() else "local"
    }
