"""
TallyWalls Local Companion Connector (Hybrid Bridge Agent).
Runs on the user's Windows PC (127.0.0.1:9455-9459) to give the Vercel web dashboard
authenticated access to local desktop capabilities:
- Tally Prime XML HTTP sync (localhost:9000)
- Live Playwright GST Portal automation (services.gst.gov.in)
- Local Downloads folder scanning and statement ingestion
- Local AES-256 encrypted credential keystore (%LOCALAPPDATA%\\TallyWalls)
"""

import os
import re
import sys
import json
import hmac
import time
import uuid
import socket
import asyncio
import secrets
import argparse
import threading
import webbrowser
from typing import Optional, List, Dict, Any

# Prevent Playwright/uvicorn crashes when running without a console window (--noconsole)
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    MEIPASS = getattr(sys, "_MEIPASS", CURRENT_DIR)
    for _p in (MEIPASS, os.path.join(MEIPASS, "app")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
else:
    for _p in (CURRENT_DIR, os.path.join(CURRENT_DIR, "app")):
        if _p not in sys.path:
            sys.path.insert(0, _p)

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.engine.portal_bridge import (
    save_credentials,
    load_credentials,
    clear_credentials,
    get_public_profile,
    execute_portal_bridge_sync,
    execute_multi_month_portal_bridge_sync,
    simulate_portal_download,
    simulate_multi_month_download,
    scan_recent_downloads,
    scan_downloads_for_gstr9,
    load_local_gstr9_files,
    ingest_gstr2b_file,
    consolidate_gstr2b_statements,
    sanitize_df_for_json,
    DOWNLOADS_DIR,
    get_user_downloads_dir,
)
from app.engine.tally_connector import (
    check_tally_status,
    fetch_purchase_vouchers_from_tally,
    simulate_tally_sync,
    sync_gstr9_full_fy_from_tally,
)

BRIDGE_VERSION = "2.4.0"
BRIDGE_HOST = "127.0.0.1"
DEFAULT_PORTS = [9455, 9456, 9457, 9458, 9459]
DEFAULT_WEB_URL = os.environ.get("TALLYWALLS_WEB_URL", "https://tallywalls.vercel.app").rstrip("/")

# ------------------------------------------------------------------
# Bridge Pairing Configuration & Security State
# ------------------------------------------------------------------

def _resolve_config_dir() -> str:
    override = os.environ.get("TALLYWALLS_CONFIG_DIR")
    if override:
        os.makedirs(override, exist_ok=True)
        return override
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        path = os.path.join(local_app, "TallyWalls")
    else:
        path = os.path.join(os.path.expanduser("~"), ".tallywalls")
    os.makedirs(path, exist_ok=True)
    return path


def _load_or_create_bridge_security(rotate_pin: bool = True) -> Dict[str, str]:
    cfg_dir = _resolve_config_dir()
    cfg_path = os.path.join(cfg_dir, "bridge_config.json")
    data: Dict[str, Any] = {}
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    data = loaded
        except Exception:
            data = {}

    if not data.get("bridge_token") or len(str(data.get("bridge_token"))) < 24:
        data["bridge_token"] = secrets.token_urlsafe(32)

    if rotate_pin or not data.get("pin") or not re.fullmatch(r"\d{6}", str(data.get("pin", ""))):
        data["pin"] = f"{secrets.randbelow(1000000):06d}"

    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        try:
            os.chmod(cfg_path, 0o600)
        except OSError:
            pass
    except Exception:
        pass

    return {
        "bridge_token": str(data["bridge_token"]),
        "pin": str(data["pin"]),
    }


SECURITY_STATE = _load_or_create_bridge_security(rotate_pin=True)
ACTIVE_PORT: int = DEFAULT_PORTS[0]

# Pair endpoint rate-limiting (max 5 failed attempts per 60 seconds)
_PAIR_LOCK = threading.Lock()
_PAIR_FAILURES: List[float] = []
PAIR_MAX_FAILURES = 5
PAIR_WINDOW_SECONDS = 60.0

# Async portal fetch job store
_JOBS_LOCK = threading.Lock()
JOBS: Dict[str, Dict[str, Any]] = {}

# Exact allowed origins + wildcard patterns
_EXACT_ALLOWED_ORIGINS = {
    "https://tallywalls.vercel.app",
    "http://tallywalls.vercel.app",
}
for _env_origin in os.environ.get("TALLYWALLS_ALLOWED_ORIGINS", "").split(","):
    cleaned = _env_origin.strip().rstrip("/")
    if cleaned:
        _EXACT_ALLOWED_ORIGINS.add(cleaned)

_ORIGIN_REGEXES = [
    re.compile(r"^https://tallywalls-[a-zA-Z0-9-]+\.vercel\.app$"),
    re.compile(r"^http://(127\.0\.0\.1|localhost):(800[0-9]|801[0-9]|3000|5173|945[5-9])$"),
]


def is_origin_allowed(origin: Optional[str]) -> bool:
    if not origin:
        return True
    normalized = origin.strip().rstrip("/")
    if normalized in _EXACT_ALLOWED_ORIGINS:
        return True
    return any(rx.match(normalized) for rx in _ORIGIN_REGEXES)


def _apply_cors_pna_headers(headers: Any, origin: Optional[str]) -> None:
    if origin and is_origin_allowed(origin):
        headers["Access-Control-Allow-Origin"] = origin.strip().rstrip("/")
        headers["Vary"] = "Origin"
        headers["Access-Control-Allow-Private-Network"] = "true"
        headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        headers["Access-Control-Allow-Headers"] = "Content-Type, X-Bridge-Token, Authorization"
        headers["Access-Control-Max-Age"] = "600"


# ------------------------------------------------------------------
# FastAPI Companion Bridge Application
# ------------------------------------------------------------------

bridge_app = FastAPI(
    title="TallyWalls Local Connector",
    description="Local companion bridge for TallyWalls Hybrid Web + Desktop architecture",
    version=BRIDGE_VERSION,
)

_EXEMPT_AUTH_PATHS = {"/bridge/health", "/bridge/pair"}


@bridge_app.middleware("http")
async def security_cors_pna_middleware(request: Request, call_next):
    origin = request.headers.get("origin")

    # 1. Strict Origin Whitelist Enforcement
    if origin and not is_origin_allowed(origin):
        return JSONResponse(
            status_code=403,
            content={"detail": f"Origin '{origin}' is not authorized to access TallyWalls Connector."}
        )

    # 2. Chrome/Edge Private Network Access (PNA) + CORS Preflight Handling
    if request.method == "OPTIONS":
        resp = Response(status_code=204)
        _apply_cors_pna_headers(resp.headers, origin)
        return resp

    # 3. Pairing Token Validation for Protected Endpoints
    path = request.url.path.rstrip("/") or "/"
    if path not in _EXEMPT_AUTH_PATHS:
        provided = request.headers.get("x-bridge-token", "")
        expected = SECURITY_STATE["bridge_token"]
        if not provided or not hmac.compare_digest(provided, expected):
            resp = JSONResponse(
                status_code=401,
                content={
                    "detail": (
                        "Unauthorized: Missing or invalid X-Bridge-Token. "
                        "Launch the website from TallyWalls Connector or pair using the 6-digit PIN."
                    )
                },
            )
            _apply_cors_pna_headers(resp.headers, origin)
            return resp

    response = await call_next(request)
    _apply_cors_pna_headers(response.headers, origin)
    return response


# ------------------------------------------------------------------
# Request Models
# ------------------------------------------------------------------

class PairRequest(BaseModel):
    pin: str

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


class TallyGstr9SyncRequest(BaseModel):
    fy: Optional[str] = "2025-26"
    host: Optional[str] = "localhost"
    port: Optional[int] = 9000
    simulate: Optional[bool] = False


class LocalGstr9BatchRequest(BaseModel):
    file_paths: List[str]


# ------------------------------------------------------------------
# Health & Pairing Endpoints
# ------------------------------------------------------------------

@bridge_app.get("/bridge/health")
async def bridge_health():
    """Unauthenticated local discovery endpoint returning non-sensitive status only."""
    tally_info = await asyncio.to_thread(check_tally_status, "127.0.0.1", 9000, 0.25)
    return {
        "status": "ok",
        "service": "TallyWalls-Connector",
        "version": BRIDGE_VERSION,
        "port": ACTIVE_PORT,
        "tally_active": bool(tally_info.get("active")),
        "tally_company": tally_info.get("company", "") if tally_info.get("active") else "",
    }


@bridge_app.post("/bridge/pair")
async def bridge_pair(req: PairRequest):
    """Exchange the 6-digit Connector PIN for the secret X-Bridge-Token (rate-limited)."""
    now = time.time()
    with _PAIR_LOCK:
        # Prune expired failure timestamps
        recent_failures = [t for t in _PAIR_FAILURES if (now - t) < PAIR_WINDOW_SECONDS]
        _PAIR_FAILURES[:] = recent_failures
        if len(recent_failures) >= PAIR_MAX_FAILURES:
            raise HTTPException(
                status_code=429,
                detail="Too many invalid pairing PIN attempts. Please wait 60 seconds and try again."
            )

    submitted = (req.pin or "").strip().replace(" ", "")
    expected = SECURITY_STATE["pin"]
    if not submitted or not hmac.compare_digest(submitted, expected):
        with _PAIR_LOCK:
            _PAIR_FAILURES.append(time.time())
        raise HTTPException(status_code=401, detail="Invalid 6-digit pairing PIN.")

    with _PAIR_LOCK:
        _PAIR_FAILURES.clear()

    return {
        "status": "paired",
        "bridge_token": SECURITY_STATE["bridge_token"],
        "port": ACTIVE_PORT,
    }


# ------------------------------------------------------------------
# Tally Prime Bridge Endpoints
# ------------------------------------------------------------------

@bridge_app.get("/bridge/tally/status")
async def bridge_tally_status(host: str = "localhost", port: int = 9000):
    return await asyncio.to_thread(check_tally_status, host, port)


@bridge_app.post("/bridge/tally/sync")
async def bridge_tally_sync(req: TallySyncRequest):
    if req.simulate:
        df_books = simulate_tally_sync()
        return {
            "status": "success",
            "source": "SIMULATED_TALLY_SYNC",
            "message": "Successfully synchronized Purchase Register from Tally Prime (Demo Mode).",
            "rows_count": len(df_books),
            "data_preview": sanitize_df_for_json(df_books),
        }

    try:
        df_books = await asyncio.to_thread(
            fetch_purchase_vouchers_from_tally,
            req.host,
            req.port,
        )
        if df_books.empty:
            raise ValueError("No purchase vouchers found in the active Tally Prime company.")

        return {
            "status": "success",
            "source": "LIVE_TALLY_SYNC",
            "message": f"Successfully pulled {len(df_books)} purchase vouchers directly from Tally Prime.",
            "rows_count": len(df_books),
            "data_preview": sanitize_df_for_json(df_books),
        }
    except ConnectionError as ce:
        raise HTTPException(status_code=503, detail=f"Tally Prime unreachable: {str(ce)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Tally synchronization failed: {str(e)}")


@bridge_app.post("/bridge/tally/sync-gstr9")
async def bridge_tally_sync_gstr9(req: TallyGstr9SyncRequest):
    try:
        res = await asyncio.to_thread(
            sync_gstr9_full_fy_from_tally,
            req.fy or "2025-26",
            req.host or "localhost",
            req.port or 9000,
            bool(req.simulate),
        )
        return res
    except ConnectionError as ce:
        raise HTTPException(status_code=503, detail=f"Tally Prime unreachable: {str(ce)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Tally GSTR-9 sync failed: {str(e)}")


@bridge_app.get("/bridge/gstr9/scan-downloads")
async def bridge_gstr9_scan_downloads():
    try:
        matches = await asyncio.to_thread(scan_downloads_for_gstr9, 86400 * 365)
        return {"status": "success", "matches": matches}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to scan local Downloads: {str(e)}")


@bridge_app.post("/bridge/gstr9/load-local-batch")
async def bridge_gstr9_load_local_batch(req: LocalGstr9BatchRequest):
    try:
        items = await asyncio.to_thread(load_local_gstr9_files, req.file_paths)
        return {"status": "success", "items": items}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load local GSTR-9 files: {str(e)}")


# ------------------------------------------------------------------
# Local Keystore & Downloads Scanner Endpoints
# ------------------------------------------------------------------

@bridge_app.get("/bridge/portal/profile")
async def bridge_portal_profile():
    return get_public_profile()


@bridge_app.get("/bridge/portal/saved-creds")
async def bridge_portal_saved_creds():
    creds = load_credentials()
    u = creds.get("username", "")
    p = creds.get("password", "")
    if u.upper() in ("DEMO_USER", "TEST_GST_ACCOUNTANT", "DEMO", "TEST", "SAMPLE_USER"):
        u = ""
        p = ""
    return {
        "username": u,
        "password": p,
        "gstin": creds.get("gstin", ""),
        "has_saved": bool(u and p),
    }


@bridge_app.post("/bridge/portal/save-profile")
async def bridge_portal_save_profile(req: PortalProfileRequest):
    if req.username.upper() in ("DEMO_USER", "TEST_GST_ACCOUNTANT", "DEMO", "TEST"):
        return {"status": "success", "message": "Example credentials skipped."}
    ok = save_credentials(req.username, req.password, req.gstin or "")
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to encrypt and store credentials locally.")
    return {"status": "success", "message": "Portal credentials saved securely on your local PC."}


@bridge_app.post("/bridge/portal/clear-creds")
async def bridge_portal_clear_creds():
    ok = clear_credentials()
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to clear local credentials.")
    return {"status": "success", "message": "Credentials cleared from local machine."}


@bridge_app.get("/bridge/portal/scan-downloads")
async def bridge_scan_downloads():
    try:
        matches = await asyncio.to_thread(scan_recent_downloads, 86400 * 7)
        return {"status": "success", "matches": matches}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to scan local Downloads: {str(e)}")


@bridge_app.post("/bridge/portal/delete-download")
async def bridge_delete_download(req: DeleteDownloadRequest):
    file_path = os.path.abspath(req.file_path)
    user_downloads = os.path.abspath(get_user_downloads_dir())
    cache_downloads = os.path.abspath(DOWNLOADS_DIR)

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
            "file_name": os.path.basename(file_path),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete file: {str(e)}")


@bridge_app.post("/bridge/portal/ingest-local")
async def bridge_ingest_local(req: LocalFileIngestRequest):
    try:
        df = await asyncio.to_thread(ingest_gstr2b_file, req.file_path)
        if df.empty:
            raise ValueError("The selected statement contains no data or 0 rows.")
        return {
            "status": "success",
            "file_name": os.path.basename(req.file_path),
            "rows_count": len(df),
            "data_preview": sanitize_df_for_json(df),
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ingestion failed: {str(e)}")


@bridge_app.post("/bridge/portal/ingest-batch")
async def bridge_ingest_batch(req: BatchIngestRequest):
    try:
        df = await asyncio.to_thread(consolidate_gstr2b_statements, req.file_paths, req.financial_year)
        if df.empty:
            raise ValueError("No invoice records could be extracted from the specified files.")
        return {
            "status": "success",
            "batch_mode": True,
            "rows_count": len(df),
            "data_preview": sanitize_df_for_json(df),
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Batch ingestion failed: {str(e)}")


# ------------------------------------------------------------------
# Async Live Portal Automation Jobs (Start + Poll Status)
# ------------------------------------------------------------------

def _update_job(job_id: str, **fields: Any) -> None:
    with _JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is not None:
            job.update(fields)
            job["updated_at"] = time.time()


def _run_portal_job_worker(job_id: str, req: PortalFetchRequest, username: str, password: str) -> None:
    def progress_cb(step: int, msg: str):
        _update_job(job_id, step=step, message=msg)

    try:
        if req.simulate:
            progress_cb(1, "Preparing simulated GST Portal response...")
            if req.batch_mode:
                s_m = req.start_month or "04"
                e_m = req.end_month or "10"
                df_2b = simulate_multi_month_download(req.financial_year, s_m, e_m)
                res = {
                    "status": "success",
                    "batch_mode": True,
                    "source": "SIMULATED_PORTAL_FETCH",
                    "message": f"Successfully simulated Multi-Month GSTR-2B fetch across {s_m} to {e_m} (FY {req.financial_year}).",
                    "rows_count": len(df_2b),
                    "data_preview": sanitize_df_for_json(df_2b),
                }
            else:
                df_2b = simulate_portal_download(req.financial_year, req.return_period or "042026")
                res = {
                    "status": "success",
                    "batch_mode": False,
                    "source": "SIMULATED_PORTAL_FETCH",
                    "message": f"Successfully simulated GSTR-2B fetch for FY {req.financial_year} ({req.return_period}).",
                    "rows_count": len(df_2b),
                    "data_preview": sanitize_df_for_json(df_2b),
                }
            _update_job(job_id, status="completed", step=4, message=res["message"], result=res)
            return

        if req.batch_mode:
            s_m = req.start_month or "04"
            e_m = req.end_month or "10"
            bridge_res = execute_multi_month_portal_bridge_sync(
                req.financial_year,
                s_m,
                e_m,
                username,
                password,
                480,
                req.return_type or "GSTR-2B",
                progress_cb=progress_cb,
            )
            res = {
                "status": "success",
                "batch_mode": True,
                "source": "LIVE_PORTAL_FETCH",
                "message": (
                    f"Multi-Month GSTR-2B statements successfully captured and consolidated across "
                    f"{s_m} to {e_m} (FY {req.financial_year})."
                ),
                "rows_count": bridge_res.get("rows_count", 0),
                "data_preview": bridge_res.get("data_preview", []),
                "captured_files": bridge_res.get("captured_files", []),
            }
            _update_job(job_id, status="completed", step=4, message=res["message"], result=res)
        else:
            bridge_res = execute_portal_bridge_sync(
                req.financial_year,
                req.return_period or "042026",
                username,
                password,
                240,
                req.return_type or "GSTR-2B",
                progress_cb=progress_cb,
            )
            file_path = bridge_res.get("file_path", "")
            df_2b = ingest_gstr2b_file(file_path)
            res = {
                "status": "success",
                "batch_mode": False,
                "source": "LIVE_PORTAL_FETCH",
                "message": f"GSTR-2B successfully captured from GST Portal for {req.return_period}.",
                "rows_count": len(df_2b),
                "data_preview": sanitize_df_for_json(df_2b),
                "file_path": file_path,
            }
            _update_job(job_id, status="completed", step=4, message=res["message"], result=res)
    except Exception as exc:
        err_msg = str(exc).strip() or f"{type(exc).__name__}: Portal bridge session failed."
        _update_job(job_id, status="failed", error=err_msg, message=err_msg)


@bridge_app.post("/bridge/portal/fetch-2b/start")
async def bridge_portal_fetch_start(req: PortalFetchRequest):
    if not req.simulate:
        creds = load_credentials()
        username = (req.username or "").strip() or creds.get("username", "")
        password = req.password or creds.get("password", "")
        if not username:
            raise HTTPException(status_code=400, detail="Portal username is required.")
        if req.save_creds and req.username and req.password and req.username.upper() not in ("DEMO", "DEMO_USER", "TEST"):
            save_credentials(req.username, req.password)

        # Prevent duplicate simultaneous browser sessions
        with _JOBS_LOCK:
            for existing in JOBS.values():
                if existing.get("status") == "running" and not existing.get("simulate"):
                    raise HTTPException(
                        status_code=409,
                        detail="A live GST Portal automation browser session is already active. Complete or close it before starting another."
                    )
    else:
        username = req.username or ""
        password = req.password or ""

    job_id = uuid.uuid4().hex
    with _JOBS_LOCK:
        # Keep memory bounded on long-running sessions
        if len(JOBS) > 40:
            oldest = sorted(JOBS.items(), key=lambda kv: kv[1].get("created_at", 0))[:20]
            for old_id, old_job in oldest:
                if old_job.get("status") != "running":
                    JOBS.pop(old_id, None)

        JOBS[job_id] = {
            "job_id": job_id,
            "status": "running",
            "simulate": bool(req.simulate),
            "step": 1,
            "message": "Initializing GST Portal bridge job...",
            "error": "",
            "result": None,
            "created_at": time.time(),
            "updated_at": time.time(),
        }

    worker = threading.Thread(
        target=_run_portal_job_worker,
        args=(job_id, req, username, password),
        daemon=True,
    )
    worker.start()

    return {
        "status": "started",
        "job_id": job_id,
    }


@bridge_app.get("/bridge/portal/fetch-2b/status/{job_id}")
async def bridge_portal_fetch_status(job_id: str):
    with _JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Portal sync job '{job_id}' not found.")
        return {
            "job_id": job["job_id"],
            "status": job["status"],
            "step": job["step"],
            "message": job["message"],
            "error": job["error"],
            "result": job["result"] if job["status"] == "completed" else None,
        }


# ------------------------------------------------------------------
# Port Helpers, Single-Instance Discovery & Desktop Status UI
# ------------------------------------------------------------------

def is_port_in_use(port: int, host: str = BRIDGE_HOST) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


def find_running_bridge_port() -> Optional[int]:
    import urllib.request
    for port in DEFAULT_PORTS:
        if is_port_in_use(port):
            try:
                url = f"http://{BRIDGE_HOST}:{port}/bridge/health"
                req = urllib.request.Request(url, headers={"User-Agent": "TallyWalls-Connector"})
                with urllib.request.urlopen(req, timeout=0.8) as resp:
                    if resp.status == 200:
                        payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
                        if payload.get("service") == "TallyWalls-Connector":
                            return port
            except Exception:
                pass
    return None


def pick_available_port(preferred_port: Optional[int] = None) -> int:
    if preferred_port is not None:
        if not is_port_in_use(preferred_port):
            return preferred_port
        raise RuntimeError(f"Requested port {preferred_port} is already in use.")
    for port in DEFAULT_PORTS:
        if not is_port_in_use(port):
            return port
    # Fallback dynamic ephemeral port range
    for port in range(9460, 9490):
        if not is_port_in_use(port):
            return port
    raise RuntimeError("No available local port found for TallyWalls Connector.")


def build_website_launch_url(port: int) -> str:
    tok = SECURITY_STATE["bridge_token"]
    return f"{DEFAULT_WEB_URL}/#bridge_token={tok}&bridge_port={port}"


def _run_desktop_ui(server: Any, port: int, open_browser: bool = True) -> None:
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.title(f"TallyWalls Connector v{BRIDGE_VERSION}")
    root.geometry("460x355")
    root.resizable(False, False)
    root.configure(bg="#f8fafc")

    # Apply app icon if bundled
    icon_candidates = [
        os.path.join(getattr(sys, "_MEIPASS", CURRENT_DIR), "tallywalls.ico"),
        os.path.join(CURRENT_DIR, "tallywalls.ico"),
    ]
    for ico in icon_candidates:
        if os.path.exists(ico):
            try:
                root.iconbitmap(ico)
                break
            except Exception:
                pass

    header = tk.Frame(root, bg="#0f172a", height=58)
    header.pack(fill="x")
    header.pack_propagate(False)

    tk.Label(
        header,
        text="TallyWalls Local Connector",
        font=("Segoe UI", 12, "bold"),
        fg="#ffffff",
        bg="#0f172a",
    ).pack(anchor="w", padx=16, pady=(10, 0))
    tk.Label(
        header,
        text=f"Hybrid Desktop Bridge v{BRIDGE_VERSION} (Port {port})",
        font=("Segoe UI", 8),
        fg="#94a3b8",
        bg="#0f172a",
    ).pack(anchor="w", padx=16, pady=(0, 8))

    body = tk.Frame(root, bg="#f8fafc", padx=16, pady=12)
    body.pack(fill="both", expand=True)

    status_card = tk.Frame(body, bg="#ffffff", highlightbackground="#cbd5e1", highlightthickness=1, padx=12, pady=10)
    status_card.pack(fill="x", pady=(0, 10))

    tk.Label(
        status_card,
        text=f"Bridge Server: Active on http://127.0.0.1:{port}",
        font=("Segoe UI", 9, "bold"),
        fg="#059669",
        bg="#ffffff",
    ).pack(anchor="w")

    tally_var = tk.StringVar(value="Tally Prime (Port 9000): Probing...")
    tally_lbl = tk.Label(
        status_card,
        textvariable=tally_var,
        font=("Segoe UI", 9),
        fg="#475569",
        bg="#ffffff",
    )
    tally_lbl.pack(anchor="w", pady=(4, 0))

    def refresh_tally():
        tally_var.set("Tally Prime (Port 9000): Probing...")
        tally_lbl.config(fg="#475569")

        def _probe():
            res = check_tally_status("localhost", 9000, timeout=1.2)
            def _apply():
                if res.get("active"):
                    comp = res.get("company") or "Active Company"
                    tally_var.set(f"Tally Prime (Port 9000): Online ({comp})")
                    tally_lbl.config(fg="#059669")
                else:
                    tally_var.set("Tally Prime (Port 9000): Offline (Enable F1 -> Settings -> Connectivity)")
                    tally_lbl.config(fg="#b45309")
            try:
                root.after(0, _apply)
            except Exception:
                pass

        threading.Thread(target=_probe, daemon=True).start()

    pin_card = tk.Frame(body, bg="#eef2ff", highlightbackground="#c7d2fe", highlightthickness=1, padx=12, pady=10)
    pin_card.pack(fill="x", pady=(0, 12))

    tk.Label(
        pin_card,
        text="6-Digit Manual Pairing PIN",
        font=("Segoe UI", 8, "bold"),
        fg="#3730a3",
        bg="#eef2ff",
    ).pack(anchor="w")

    pin_code = SECURITY_STATE["pin"]
    tk.Label(
        pin_card,
        text=f"{pin_code[:3]} {pin_code[3:]}",
        font=("Consolas", 18, "bold"),
        fg="#1e1b4b",
        bg="#eef2ff",
    ).pack(anchor="w", pady=(2, 2))

    tk.Label(
        pin_card,
        text="Click 'Open Website' for automatic pairing, or enter this PIN on tallywalls.vercel.app.",
        font=("Segoe UI", 8),
        fg="#4338ca",
        bg="#eef2ff",
        wraplength=400,
        justify="left",
    ).pack(anchor="w")

    btn_row = tk.Frame(body, bg="#f8fafc")
    btn_row.pack(fill="x", pady=(4, 0))

    def on_open_web():
        webbrowser.open(build_website_launch_url(port))

    def on_quit():
        if messagebox.askyesno("Quit Connector", "Stop TallyWalls Local Connector? Live Tally sync and GST Portal bridge on the website will pause until restarted."):
            server.should_exit = True
            root.destroy()

    tk.Button(
        btn_row,
        text="Open TallyWalls Website",
        command=on_open_web,
        bg="#4f46e5",
        fg="#ffffff",
        activebackground="#4338ca",
        activeforeground="#ffffff",
        font=("Segoe UI", 9, "bold"),
        relief="flat",
        padx=12,
        pady=7,
        cursor="hand2",
    ).pack(side="left", fill="x", expand=True, padx=(0, 6))

    tk.Button(
        btn_row,
        text="Refresh Tally",
        command=refresh_tally,
        bg="#e2e8f0",
        fg="#0f172a",
        activebackground="#cbd5e1",
        font=("Segoe UI", 9),
        relief="flat",
        padx=10,
        pady=7,
        cursor="hand2",
    ).pack(side="left", padx=(0, 6))

    tk.Button(
        btn_row,
        text="Quit",
        command=on_quit,
        bg="#fee2e2",
        fg="#991b1b",
        activebackground="#fecaca",
        font=("Segoe UI", 9, "bold"),
        relief="flat",
        padx=12,
        pady=7,
        cursor="hand2",
    ).pack(side="left")

    root.protocol("WM_DELETE_WINDOW", on_quit)
    root.after(250, refresh_tally)

    if open_browser:
        def _auto_open():
            for _ in range(40):
                if is_port_in_use(port):
                    webbrowser.open(build_website_launch_url(port))
                    return
                time.sleep(0.1)
        threading.Thread(target=_auto_open, daemon=True).start()

    root.mainloop()


def main():
    global ACTIVE_PORT

    parser = argparse.ArgumentParser(description="TallyWalls Local Connector Service")
    parser.add_argument("--headless", action="store_true", help="Run without desktop GUI window")
    parser.add_argument("--no-browser", action="store_true", help="Do not open website automatically on startup")
    parser.add_argument("--port", type=int, default=None, help="Bind explicit localhost port")
    args = parser.parse_args()

    # Interactive mode: reuse already-running Connector instance cleanly
    if not args.headless and args.port is None:
        running_port = find_running_bridge_port()
        if running_port is not None:
            if not args.no_browser:
                webbrowser.open(build_website_launch_url(running_port))
            sys.exit(0)

    ACTIVE_PORT = pick_available_port(args.port)

    import uvicorn
    config = uvicorn.Config(
        bridge_app,
        host=BRIDGE_HOST,
        port=ACTIVE_PORT,
        log_level="warning" if args.headless else "error",
        access_log=False,
    )
    server = uvicorn.Server(config)

    if args.headless:
        print(f"TallyWalls Connector listening on http://{BRIDGE_HOST}:{ACTIVE_PORT} (headless)")
        server.run()
    else:
        server_thread = threading.Thread(target=server.run, daemon=True)
        server_thread.start()
        try:
            _run_desktop_ui(server, ACTIVE_PORT, open_browser=not args.no_browser)
        finally:
            server.should_exit = True
            server_thread.join(timeout=2.0)


if __name__ == "__main__":
    main()
