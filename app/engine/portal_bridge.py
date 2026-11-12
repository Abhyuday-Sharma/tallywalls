"""
GST Portal Automation & Session Bridge for services.gst.gov.in.
Handles local encrypted credential storage, automated Chromium login bridge,
CAPTCHA synchronization, Return Dashboard navigation from fowelcome,
strict Financial Year + Quarter + Month selection, and GSTR-2B statutory statement ingestion.
Supports Excel (.xlsx, .xls), JSON, and ZIP downloads with dual filesystem watcher.
"""

import os
import sys
import time
import json
import base64
import glob
import zipfile
import tempfile
import asyncio
from typing import Dict, Any, Optional, Tuple, List, Callable
import pandas as pd
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

def _resolve_cache_dir() -> str:
    env_cache = os.environ.get("TALLYWALLS_CACHE_DIR")
    if env_cache:
        try:
            os.makedirs(env_cache, exist_ok=True)
            return env_cache
        except (OSError, PermissionError):
            pass

    env_cfg = os.environ.get("TALLYWALLS_CONFIG_DIR")
    if env_cfg:
        cfg_cache = os.path.join(env_cfg, "cache")
        try:
            os.makedirs(cfg_cache, exist_ok=True)
            return cfg_cache
        except (OSError, PermissionError):
            pass

    local_app = os.environ.get("LOCALAPPDATA")
    if getattr(sys, "frozen", False) and local_app:
        frozen_dir = os.path.join(local_app, "TallyWalls", "cache")
        try:
            os.makedirs(frozen_dir, exist_ok=True)
            return frozen_dir
        except (OSError, PermissionError):
            pass

    default_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache")
    try:
        os.makedirs(default_dir, exist_ok=True)
        test_file = os.path.join(default_dir, ".write_test")
        with open(test_file, "w") as f:
            f.write("ok")
        os.remove(test_file)
        return default_dir
    except (OSError, PermissionError):
        if local_app:
            la_dir = os.path.join(local_app, "TallyWalls", "cache")
            try:
                os.makedirs(la_dir, exist_ok=True)
                return la_dir
            except (OSError, PermissionError):
                pass
        tmp_dir = os.path.join(tempfile.gettempdir(), "tallywalls_cache")
        try:
            os.makedirs(tmp_dir, exist_ok=True)
        except Exception:
            pass
        return tmp_dir

CACHE_DIR = _resolve_cache_dir()
DOWNLOADS_DIR = os.path.join(CACHE_DIR, "downloads")
try:
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
except Exception:
    pass

CRED_FILE = os.path.join(CACHE_DIR, "portal_profile.enc")
KEY_FILE = os.path.join(CACHE_DIR, ".key.bin")


def _get_legacy_encryption_key() -> bytes:
    salt = b"FinRecon_GST_India_Secure_Salt_2024"
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
    )
    machine_id = os.environ.get("USERNAME", os.environ.get("USER", "DefaultUser")).encode()
    return base64.urlsafe_b64encode(kdf.derive(machine_id))


def _get_encryption_key() -> bytes:
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, "rb") as f:
            key = f.read().strip()
        if key:
            return key
    key = Fernet.generate_key()
    with open(KEY_FILE, "wb") as f:
        f.write(key)
    try:
        os.chmod(KEY_FILE, 0o600)
    except OSError:
        pass
    return key


def save_credentials(username: str, password: str, gstin: str = "") -> bool:
    """Save user credentials locally with AES-256 Fernet encryption."""
    try:
        fernet = Fernet(_get_encryption_key())
        data = {
            "username": username.strip(),
            "password": password,
            "gstin": gstin.strip().upper()
        }
        encrypted = fernet.encrypt(json.dumps(data).encode("utf-8"))
        with open(CRED_FILE, "wb") as f:
            f.write(encrypted)
        return True
    except Exception as e:
        print(f"Failed to save credentials: {e}")
        return False


def load_credentials() -> Dict[str, str]:
    """Load and decrypt saved credentials from local cache."""
    if not os.path.exists(CRED_FILE):
        return {"username": "", "password": "", "gstin": ""}
    try:
        with open(CRED_FILE, "rb") as f:
            encrypted = f.read()
        try:
            fernet = Fernet(_get_encryption_key())
            decrypted = fernet.decrypt(encrypted)
            return json.loads(decrypted.decode("utf-8"))
        except Exception:
            # Backward-compatibility fallback for legacy key migration
            legacy_fernet = Fernet(_get_legacy_encryption_key())
            decrypted = legacy_fernet.decrypt(encrypted)
            data = json.loads(decrypted.decode("utf-8"))
            if data.get("username") and data.get("password"):
                save_credentials(data["username"], data["password"], data.get("gstin", ""))
            return data
    except Exception as e:
        print(f"Failed to decrypt credentials: {e}")
        return {"username": "", "password": "", "gstin": ""}


def clear_credentials() -> bool:
    """Clear saved credentials file."""
    try:
        if os.path.exists(CRED_FILE):
            os.remove(CRED_FILE)
        return True
    except Exception as e:
        print(f"Failed to clear credentials: {e}")
        return False


def get_public_profile() -> Dict[str, str]:
    """Return non-sensitive profile info (username and GSTIN only)."""
    creds = load_credentials()
    return {
        "username": creds.get("username", ""),
        "gstin": creds.get("gstin", ""),
        "has_saved_password": bool(creds.get("password"))
    }


def parse_gstr2b_json(data: Dict[str, Any]) -> pd.DataFrame:
    """
    Parse official GST Portal GSTR-2B JSON structure into a clean DataFrame.
    Official schema has: data -> b2b -> [ { ctin, cpty, inv: [ { inum, idt, val, items: [ ... ] } ] } ]
    """
    rows = []
    b2b_list = data.get("data", {}).get("b2b", [])
    if not b2b_list and "b2b" in data:
        b2b_list = data.get("b2b", [])

    for supplier in b2b_list:
        gstin = supplier.get("ctin", "")
        trade_name = supplier.get("cpty", "") or supplier.get("trdnm", "")
        for inv in supplier.get("inv", []):
            inv_no = str(inv.get("inum", ""))
            inv_date = str(inv.get("idt", ""))
            inv_val = float(inv.get("val", 0.0))

            taxable_total = 0.0
            igst_total = 0.0
            cgst_total = 0.0
            sgst_total = 0.0
            cess_total = 0.0

            for itm in inv.get("items", []):
                itm_det = itm.get("itmdet", itm)
                taxable_total += float(itm_det.get("txval", 0.0))
                igst_total += float(itm_det.get("iamt", 0.0))
                cgst_total += float(itm_det.get("camt", 0.0))
                sgst_total += float(itm_det.get("samt", 0.0))
                cess_total += float(itm_det.get("csamt", 0.0))

            rows.append({
                "GSTIN of supplier": gstin,
                "Trade/Legal name": trade_name,
                "Invoice number": inv_no,
                "Invoice Date": inv_date,
                "Invoice Value (₹)": inv_val,
                "Taxable Value (₹)": round(taxable_total, 2),
                "Integrated Tax (₹)": round(igst_total, 2),
                "Central Tax (₹)": round(cgst_total, 2),
                "State/UT Tax (₹)": round(sgst_total, 2),
                "Cess (₹)": round(cess_total, 2),
                "ITC Availability": "Yes"
            })

    return pd.DataFrame(rows)


def parse_gstr2b_excel(file_or_path) -> pd.DataFrame:
    """
    Robust reader for official GST Portal GSTR-2B Excel (.xlsx, .xls) statements.
    Finance-first: Handles multi-sheet files, taxpayer metadata headers, and summary rows.
    Supports file path or in-memory BytesIO streams.
    """
    with pd.ExcelFile(file_or_path) as xls:
        sheet_names = list(xls.sheet_names)

    # Identify target sheet (prefer 'B2B')
    target_sheet = None
    for s in sheet_names:
        if s.strip().lower() == "b2b":
            target_sheet = s
            break
    if not target_sheet:
        for s in sheet_names:
            if "b2b" in s.lower():
                target_sheet = s
                break
    if not target_sheet:
        for s in sheet_names:
            if not any(k in s.lower() for k in ["read", "help", "instruction", "readme"]):
                target_sheet = s
                break
    if not target_sheet:
        target_sheet = sheet_names[0]

    # Reset position if BytesIO
    if hasattr(file_or_path, "seek"):
        file_or_path.seek(0)

    # Read raw rows to detect the real table header row (skipping taxpayer metadata rows)
    df_raw = pd.read_excel(file_or_path, sheet_name=target_sheet, header=None)

    # Check for official GST Portal 2-tier header (e.g. Row 4 has 'GSTIN of supplier', Row 5 has 'Invoice number')
    two_tier_idx = None
    for i in range(min(12, len(df_raw) - 1)):
        r_curr = [str(x).lower().strip() for x in df_raw.iloc[i].dropna()]
        r_next = [str(x).lower().strip() for x in df_raw.iloc[i + 1].dropna()]
        has_gstin_curr = any("gstin" in x for x in r_curr)
        has_inv_next = any("invoice number" in x or "integrated tax" in x or "tax amount" in x for x in r_next)
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
        # Fallback to single-tier header scan
        header_row_idx = 0
        max_matches = 0
        key_terms = ["gstin", "invoice", "taxable", "integrated", "central", "state", "cess", "supplier", "party"]

        for idx in range(min(12, len(df_raw))):
            row_vals = [str(v).lower().strip() for v in df_raw.iloc[idx].dropna()]
            matches = sum(any(term in val for term in key_terms) for val in row_vals)
            if matches > max_matches and matches >= 2:
                max_matches = matches
                header_row_idx = idx

        if hasattr(file_or_path, "seek"):
            file_or_path.seek(0)

        if max_matches >= 2:
            df = pd.read_excel(file_or_path, sheet_name=target_sheet, header=header_row_idx)
        else:
            df = pd.read_excel(file_or_path, sheet_name=target_sheet)

    # Clean columns: strip whitespace
    df.columns = [str(c).strip() for c in df.columns]

    # Filter out empty rows or summary/total rows
    df = df.dropna(how="all")
    if not df.empty:
        first_col = df.columns[0]
        df = df[~df[first_col].astype(str).str.strip().str.lower().str.match(r"^(?:grand\s+|sub\s*[-\s]*)?total(?:\s*[:\-].*)?$", na=False)]

    return df


def ingest_gstr2b_file(file_path: str) -> pd.DataFrame:
    """
    Universal ingestor for GSTR-2B statements, strictly prioritizing Excel.
    Supports:
    - .xlsx or .xls file (official GST Portal B2B sheet with multi-row headers)
    - .zip file containing Excel (.xlsx/.xls) or JSON
    - .json file as fallback
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    ext = os.path.splitext(file_path)[1].lower()

    if ext in [".xlsx", ".xls"]:
        return parse_gstr2b_excel(file_path)

    elif ext == ".zip":
        with zipfile.ZipFile(file_path, "r") as zf:
            file_names = zf.namelist()
            excel_files = [f for f in file_names if f.lower().endswith((".xlsx", ".xls"))]
            json_files = [f for f in file_names if f.lower().endswith(".json")]

            # ALWAYS PREFER EXCEL FILES FIRST IN ZIP (Finance Default)
            if excel_files:
                target = excel_files[0]
                import io
                data_bytes = zf.read(target)
                bio = io.BytesIO(data_bytes)
                return parse_gstr2b_excel(bio)
            elif json_files:
                target = json_files[0]
                with zf.open(target) as f:
                    data = json.load(f)
                return parse_gstr2b_json(data)
            else:
                raise ValueError(f"Zip file contains no Excel or JSON statements: {file_names}")

    elif ext == ".json":
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return parse_gstr2b_json(data)

    elif ext == ".csv":
        return pd.read_csv(file_path)

    else:
        raise ValueError(f"Unsupported file format for GSTR-2B: {ext}. Please use Excel (.xlsx, .xls) or .zip.")


def sanitize_df_for_json(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    Sanitize DataFrame into JSON-compliant list of records.
    Replaces NaN, Infinity, and un-serializable float values with None or empty string
    so Starlette/FastAPI json.dumps never raises Out of range float values are not JSON compliant.
    """
    if df.empty:
        return []
    df_clean = df.fillna("").copy()
    records = df_clean.to_dict(orient="records")
    for row in records:
        for k, v in list(row.items()):
            if isinstance(v, float) and (v != v or v == float("inf") or v == float("-inf")):
                row[k] = None
    return records


def get_user_downloads_dir() -> str:
    """Return the absolute path to the current user's Downloads folder."""
    user_profile = os.environ.get("USERPROFILE", os.path.expanduser("~"))
    return os.path.join(user_profile, "Downloads")


def scan_recent_downloads(max_age_seconds: int = 86400 * 7) -> List[Dict[str, Any]]:
    """
    Scan the user's Downloads directory and local cache for recently downloaded GSTR-2B files.
    Strictly prioritizes Excel files (.xlsx, .xls, .zip containing Excel) over JSON.
    Returns sorted list of matches with metadata.
    """
    downloads_dir = get_user_downloads_dir()
    search_dirs = [downloads_dir, DOWNLOADS_DIR]

    patterns = [
        "*2B*.xlsx", "*2B*.xls", "*2B*.zip",
        "*2b*.xlsx", "*2b*.xls", "*2b*.zip",
        "*R2B*.xlsx", "*R2B*.xls", "*R2B*.zip",
        "*gstr2b*.xlsx", "*gstr2b*.xls", "*gstr2b*.zip",
        "*GSTR2B*.xlsx", "*GSTR2B*.xls", "*GSTR2B*.zip",
        "*gstr_2b*.xlsx", "*gstr_2b*.xls", "*gstr_2b*.zip",
        "*GSTR_2B*.xlsx", "*GSTR_2B*.xls", "*GSTR_2B*.zip",
        "*.xlsx", "*.xls",
        "*2B*.json", "*2b*.json"
    ]

    now = time.time()
    found = []
    seen_paths = set()

    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        for pat in patterns:
            for p in glob.glob(os.path.join(d, pat)):
                abs_p = os.path.abspath(p)
                if abs_p in seen_paths or not os.path.isfile(abs_p):
                    continue
                seen_paths.add(abs_p)

                # Strictly exclude any GSTR-1 files
                base_name = os.path.basename(abs_p).upper()
                if "GSTR1" in base_name or "GSTR-1" in base_name or "_R1_" in base_name or "_R1." in base_name:
                    continue

                if abs_p.endswith((".crdownload", ".tmp")):
                    continue

                mtime = os.path.getmtime(abs_p)
                age = now - mtime
                if age <= max_age_seconds:
                    size = os.path.getsize(abs_p)
                    f_ext = os.path.splitext(abs_p)[1].lower()
                    # Rank Excel (.xlsx/.xls) as 0, Zip as 1, JSON as 2
                    format_rank = 0 if f_ext in [".xlsx", ".xls"] else (1 if f_ext == ".zip" else 2)
                    found.append({
                        "file_path": abs_p,
                        "file_name": os.path.basename(abs_p),
                        "mtime": mtime,
                        "age_seconds": int(age),
                        "size_kb": round(size / 1024, 1),
                        "modified_date": time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)),
                        "format_rank": format_rank,
                        "is_excel": f_ext in [".xlsx", ".xls", ".zip"]
                    })

    # Sort strictly: Excel first, then by newest modification time
    found.sort(key=lambda x: (x["format_rank"], -x["mtime"]))
    return found


def get_return_period_details(period_str: str) -> Dict[str, Any]:
    """
    Map return period input (e.g. '042026', 'April', '112026', 'November')
    to Month Name, Month Code (MM), and corresponding Financial Quarter.
    """
    p = str(period_str).strip()
    month_data = {
        "04": {"name": "April", "quarter_num": "1", "quarter_code": "Q1", "quarter_label": "Quarter 1"},
        "05": {"name": "May", "quarter_num": "1", "quarter_code": "Q1", "quarter_label": "Quarter 1"},
        "06": {"name": "June", "quarter_num": "1", "quarter_code": "Q1", "quarter_label": "Quarter 1"},
        "07": {"name": "July", "quarter_num": "2", "quarter_code": "Q2", "quarter_label": "Quarter 2"},
        "08": {"name": "August", "quarter_num": "2", "quarter_code": "Q2", "quarter_label": "Quarter 2"},
        "09": {"name": "September", "quarter_num": "2", "quarter_code": "Q2", "quarter_label": "Quarter 2"},
        "10": {"name": "October", "quarter_num": "3", "quarter_code": "Q3", "quarter_label": "Quarter 3"},
        "11": {"name": "November", "quarter_num": "3", "quarter_code": "Q3", "quarter_label": "Quarter 3"},
        "12": {"name": "December", "quarter_num": "3", "quarter_code": "Q3", "quarter_label": "Quarter 3"},
        "01": {"name": "January", "quarter_num": "4", "quarter_code": "Q4", "quarter_label": "Quarter 4"},
        "02": {"name": "February", "quarter_num": "4", "quarter_code": "Q4", "quarter_label": "Quarter 4"},
        "03": {"name": "March", "quarter_num": "4", "quarter_code": "Q4", "quarter_label": "Quarter 4"}
    }

    m_code = p[:2] if len(p) >= 2 and p[:2] in month_data else None
    if not m_code:
        for k, v in month_data.items():
            if v["name"].lower() in p.lower():
                m_code = k
                break
    if not m_code:
        m_code = "04"  # Default to April

    info = month_data[m_code].copy()
    info["code"] = m_code
    return info


def get_fy_month_sequence(start_period: str = "04", end_period: str = "10", fy: str = "2026-27") -> List[Dict[str, Any]]:
    """
    Generate ordered sequence of statutory return months across financial quarters.
    Default: 04 (April) to 10 (October) for FY 2026-27 (7 months: Apr, May, Jun, Jul, Aug, Sep, Oct).
    Finance-first: Handles financial year rollover from calendar year 1 (Apr-Dec) to year 2 (Jan-Mar).
    """
    FY_MONTH_ORDER = ["04", "05", "06", "07", "08", "09", "10", "11", "12", "01", "02", "03"]
    month_data = {
        "04": {"name": "April", "quarter_num": "1", "quarter_code": "Q1", "quarter_label": "Quarter 1"},
        "05": {"name": "May", "quarter_num": "1", "quarter_code": "Q1", "quarter_label": "Quarter 1"},
        "06": {"name": "June", "quarter_num": "1", "quarter_code": "Q1", "quarter_label": "Quarter 1"},
        "07": {"name": "July", "quarter_num": "2", "quarter_code": "Q2", "quarter_label": "Quarter 2"},
        "08": {"name": "August", "quarter_num": "2", "quarter_code": "Q2", "quarter_label": "Quarter 2"},
        "09": {"name": "September", "quarter_num": "2", "quarter_code": "Q2", "quarter_label": "Quarter 2"},
        "10": {"name": "October", "quarter_num": "3", "quarter_code": "Q3", "quarter_label": "Quarter 3"},
        "11": {"name": "November", "quarter_num": "3", "quarter_code": "Q3", "quarter_label": "Quarter 3"},
        "12": {"name": "December", "quarter_num": "3", "quarter_code": "Q3", "quarter_label": "Quarter 3"},
        "01": {"name": "January", "quarter_num": "4", "quarter_code": "Q4", "quarter_label": "Quarter 4"},
        "02": {"name": "February", "quarter_num": "4", "quarter_code": "Q4", "quarter_label": "Quarter 4"},
        "03": {"name": "March", "quarter_num": "4", "quarter_code": "Q4", "quarter_label": "Quarter 4"}
    }
    s_clean = str(start_period).strip()[:2]
    e_clean = str(end_period).strip()[:2]

    # Map month names if passed as strings (e.g. 'April', 'October')
    for k, v in month_data.items():
        if v["name"].lower() in str(start_period).lower():
            s_clean = k
        if v["name"].lower() in str(end_period).lower():
            e_clean = k

    s_idx = FY_MONTH_ORDER.index(s_clean) if s_clean in FY_MONTH_ORDER else 0
    e_idx = FY_MONTH_ORDER.index(e_clean) if e_clean in FY_MONTH_ORDER else 6

    if s_idx > e_idx:
        e_idx = s_idx

    start_year = int(fy.split("-")[0]) if "-" in fy else 2026
    end_year = start_year + 1

    sequence = []
    for m in FY_MONTH_ORDER[s_idx:e_idx + 1]:
        info = month_data[m].copy()
        info["code"] = m
        yr = start_year if int(m) >= 4 else end_year
        info["year"] = str(yr)
        info["full_period_code"] = f"{m}{yr}"
        info["period_label"] = f"{info['name']} {yr}"
        info["fy"] = fy
        sequence.append(info)

    return sequence


def navigate_from_welcome_to_returns_dashboard(page) -> bool:
    """
    Handle legitimate in-session navigation from services.gst.gov.in/services/auth/fowelcome
    to return.gst.gov.in/returns/auth/dashboard without triggering unauthenticated SSO redirects.
    Uses in-page dashboard tile button or top navbar Services -> Returns -> Returns Dashboard.
    Never navigates directly via page.goto or clicks calendar status cells.
    """
    print("Portal Bridge: On fowelcome landing page. Attempting in-page navigation...")
    page.wait_for_timeout(2000)

    # 1. Dismiss any modal/dialog or notice overlay if present (e.g. Geocoding, Advisory)
    try:
        remind_later = page.locator("button:has-text('Remind Me Later'), a:has-text('Remind Me Later'), button:has-text('Close'), button:has-text('Dismiss'), button:has-text('Skip')").first
        if remind_later.is_visible(timeout=1500):
            remind_later.click()
            page.wait_for_timeout(1000)
    except Exception:
        pass

    # 2. Strategy A: Click 'RETURN DASHBOARD' or 'FILE RETURNS' button in dashboard panel
    try:
        page.evaluate("window.scrollBy(0, 450);")
        page.wait_for_timeout(1000)

        selectors = [
            "button:has-text('RETURN DASHBOARD')",
            "a:has-text('RETURN DASHBOARD')",
            "button:has-text('Return Dashboard')",
            "a:has-text('Return Dashboard')",
            "button:has-text('FILE RETURNS')",
            "a:has-text('FILE RETURNS')",
            "div.dash-tile:has-text('RETURN DASHBOARD')",
            "div:has-text('RETURN DASHBOARD') button",
            "div:has-text('RETURN DASHBOARD') a",
            "a[href*='returns/auth/dashboard']",
            "button[onclick*='dashboard']"
        ]
        for sel in selectors:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=2000):
                print(f"Portal Bridge: Found Return Dashboard button using selector: {sel}")
                loc.scroll_into_view_if_needed()
                loc.click()
                page.wait_for_timeout(3000)
                if "return.gst.gov.in" in page.url or "returns/auth/dashboard" in page.url:
                    return True
    except Exception as e:
        print(f"Dashboard button strategy note: {e}")

    # 3. Strategy B: Top Navigation Menu (Services -> Returns -> Returns Dashboard)
    try:
        print("Portal Bridge: Attempting top navigation menu (Services -> Returns -> Returns Dashboard)...")
        services_menu = page.locator("nav a:has-text('Services'), header a:has-text('Services'), a.dropdown-toggle:has-text('Services'), a:has-text('Services')").first
        if services_menu.is_visible(timeout=2500):
            services_menu.click()
            page.wait_for_timeout(600)

            returns_sub = page.locator("a:has-text('Returns'), li:has-text('Returns') > a").first
            if returns_sub.is_visible(timeout=2000):
                returns_sub.hover()
                page.wait_for_timeout(400)

            rd_link = page.locator("a:has-text('Returns Dashboard'), a[href*='returns/auth/dashboard']").first
            if rd_link.is_visible(timeout=2000):
                rd_link.click()
                page.wait_for_timeout(3000)
                if "return.gst.gov.in" in page.url or "returns/auth/dashboard" in page.url:
                    return True
    except Exception as e:
        print(f"Top nav menu strategy note: {e}")

    return False


def select_period_and_trigger_download(page, fy: str, return_period: str, return_type: str = "GSTR-2B", force_select: bool = False) -> bool:
    """
    Automates selection on https://return.gst.gov.in/returns/auth/dashboard:
    1. Checks if the GSTR-2B tile is already rendered on screen (only if force_select is False).
    2. Selects Financial Year, Quarter, and target Month, then clicks SEARCH.
    3. Clicks DOWNLOAD exclusively on the isolated GSTR-2B tile (strict exclusion of GSTR-1 & GSTR-2A).
    4. Triggers JSON or Excel download on the GSTR-2B statement screen.
    """
    p_info = get_return_period_details(return_period)
    target_statement = "GSTR-2B" if "2B" in (return_type or "GSTR-2B").upper() else "GSTR-2A"
    print(f"Portal Bridge: Target statement -> {target_statement} | Month: {p_info['name']} (Code: {p_info['code']}), Quarter: {p_info['quarter_label']} ({p_info['quarter_code']}), FY: {fy}")

    already_rendered = False
    if not force_select:
        try:
            already_rendered = page.evaluate(r"""() => {
                const allElements = Array.from(document.querySelectorAll('*'));
                for (const el of allElements) {
                    const t = (el.innerText || '').trim();
                    if (/^GSTR-?2B$/i.test(t)) {
                        let curr = el.parentElement;
                        while (curr && curr !== document.body) {
                            const buttons = Array.from(curr.querySelectorAll('button, a, input[type="button"]'));
                            if (buttons.some(b => /DOWNLOAD/i.test((b.innerText || b.value || '').trim()))) {
                                return true;
                            }
                            curr = curr.parentElement;
                        }
                    }
                }
                return false;
            }""")
        except Exception:
            pass

    if already_rendered and not force_select:
        print("Portal Bridge: GSTR-2B return tile is ALREADY visible on screen! Proceeding directly to click DOWNLOAD...")
    else:
        # Step A: Financial Year Selection
        try:
            fy_select = page.locator("select[id*='fin'], select[name*='fin'], select[formcontrolname*='fin']").first
            if fy_select.is_visible(timeout=3000):
                options = [o.strip() for o in fy_select.locator("option").all_inner_texts() if o.strip()]
                print(f"Portal Bridge: Available FY options on portal: {options}")
                matched_opt = None
                for opt in options:
                    if fy in opt:
                        matched_opt = opt
                        break
                if not matched_opt and options:
                    matched_opt = options[0] if options[0].lower() != "select" else (options[1] if len(options) > 1 else options[0])
                    print(f"Portal Bridge: Requested FY '{fy}' not in portal list. Falling back to active portal FY: '{matched_opt}'.")

                if matched_opt:
                    fy_select.select_option(label=matched_opt)
                    print(f"Portal Bridge: Selected Financial Year: {matched_opt}")
                    page.wait_for_timeout(1000)
        except Exception as e:
            print(f"FY select note: {e}")

        # Step B: Quarter Dropdown (Angular populates Months based on Quarter selection)
        try:
            q_select = page.locator("select[id*='quarter'], select[name*='quarter'], select[formcontrolname*='quarter']").first
            if q_select.is_visible(timeout=2500):
                options = [o.strip() for o in q_select.locator("option").all_inner_texts() if o.strip()]
                print(f"Portal Bridge: Available Quarter options: {options}")
                q_selected = False
                for opt in options:
                    if (p_info["quarter_label"].lower() in opt.lower()
                        or p_info["quarter_code"].lower() in opt.lower()
                        or p_info["quarter_num"] == opt):
                        q_select.select_option(label=opt)
                        print(f"Portal Bridge: Selected Quarter: {opt}")
                        q_selected = True
                        break
                if not q_selected:
                    try:
                        q_select.select_option(value=p_info["quarter_num"])
                        q_selected = True
                    except Exception:
                        pass

                if q_selected:
                    page.wait_for_timeout(1500)  # Wait for Angular to populate month dropdown
        except Exception as e:
            print(f"Quarter select note: {e}")

        # Step C: Period / Month Dropdown (Explicitly select target month, never accept default November)
        try:
            m_select = page.locator("select[id*='mon'], select[name*='mon'], select[id*='period'], select[name*='period'], select[formcontrolname*='period'], select[formcontrolname*='month']").first
            if m_select.is_visible(timeout=3000):
                for _ in range(6):
                    options = [o.strip() for o in m_select.locator("option").all_inner_texts() if o.strip()]
                    if any(p_info["name"].lower() in o.lower() for o in options):
                        break
                    page.wait_for_timeout(400)

                print(f"Portal Bridge: Available Month options: {options}")
                m_selected = False
                for opt in options:
                    if p_info["name"].lower() in opt.lower() or p_info["code"] == opt:
                        m_select.select_option(label=opt)
                        print(f"Portal Bridge: Successfully selected Month: {opt}")
                        m_selected = True
                        break
                if not m_selected:
                    try:
                        m_select.select_option(value=p_info["code"])
                        print(f"Portal Bridge: Selected Month by code: {p_info['code']}")
                        m_selected = True
                    except Exception:
                        pass

                if m_selected:
                    page.wait_for_timeout(800)
        except Exception as e:
            print(f"Month select note: {e}")

        # Update in-browser HUD banner
        try:
            page.evaluate(f"""() => {{
                const el = document.getElementById('fin-recon-hud-text');
                if (el) el.innerHTML = 'Period Selected: <b>FY {fy} • {p_info["name"]} (Quarter {p_info["quarter_num"]})</b>. Searching returns...';
            }}""")
        except Exception:
            pass

        # Step D: Click SEARCH (strictly scoped to returns form to avoid navbar 'Search Taxpayer')
        try:
            search_btn = page.locator(
                "form button:has-text('SEARCH'), "
                "button:text-is('SEARCH'), "
                "button:text-is('Search'), "
                "input[value='SEARCH'], "
                "button.btn-primary:has-text('SEARCH')"
            ).first
            if search_btn.is_visible(timeout=3000):
                print("Portal Bridge: Clicking SEARCH button...")
                search_btn.scroll_into_view_if_needed()
                search_btn.click()
                page.wait_for_timeout(2500)
        except Exception as e:
            print(f"Search button note: {e}")

        # Wait for GSTR-2B tile/card to appear in search results
        try:
            print("Portal Bridge: Waiting for GSTR-2B statement tile to render in results...")
            page.wait_for_selector("div:has-text('GSTR-2B'), div:has-text('Auto-drafted ITC Statement'), div:has-text('Auto - drafted ITC Statement')", state="visible", timeout=15000)
            page.wait_for_timeout(1000)
        except Exception as e:
            print(f"Wait for GSTR-2B results note: {e}")

    # Step E: Highlight the GSTR-2B statement card and DOWNLOAD button for the user
    try:
        page.evaluate(r"""() => {
            const allElements = Array.from(document.querySelectorAll('*'));
            for (const el of allElements) {
                const t = (el.innerText || '').trim();
                if (/^GSTR-?2B$/i.test(t)) {
                    let curr = el.parentElement;
                    while (curr && curr !== document.body) {
                        const buttons = Array.from(curr.querySelectorAll('button, a, input[type="button"]'));
                        const btn = buttons.find(b => {
                            const txt = (b.innerText || b.value || '').trim().toUpperCase();
                            return txt === 'DOWNLOAD' || txt.startsWith('DOWNLOAD');
                        });
                        if (btn) {
                            curr.style.border = '2px solid #2563eb';
                            curr.style.boxShadow = '0 0 16px rgba(37, 99, 235, 0.35)';
                            btn.style.backgroundColor = '#16a34a';
                            btn.style.color = '#ffffff';
                            btn.style.fontWeight = 'bold';
                            btn.style.border = '2px solid #15803d';
                            break;
                        }
                        curr = curr.parentElement;
                    }
                }
            }
        }""")
        # Update assistive HUD banner
        page.evaluate(f"""() => {{
            const hud = document.getElementById('fin-recon-hud-text');
            if (hud) {{
                hud.innerHTML = 'Period Selected: <b>FY {fy} • {p_info["name"]}</b>. Search complete! Click the <b>DOWNLOAD</b> button on GSTR-2B, then click <b>GENERATE EXCEL FILE</b> (or File 1) to download Excel.';
            }}
        }}""")
        print(f"Portal Bridge: Search completed for FY {fy} - {p_info['name']}. GSTR-2B tile highlighted. Waiting for Excel statement download...")
    except Exception as e:
        print(f"Highlight note: {e}")

    return True


def click_gstr2b_download_button(page) -> bool:
    """
    Locates the isolated GSTR-2B tile on the Returns Dashboard and clicks its DOWNLOAD button.
    Strictly excludes GSTR-1, GSTR-1A, GSTR-3B, and GSTR-2A tiles to prevent mis-clicks.
    """
    try:
        clicked = page.evaluate(r"""() => {
            const allCards = Array.from(document.querySelectorAll('div.card, div.col-md-4, div[class*="tile"], div[class*="box"], div'));
            for (const c of allCards) {
                const txt = (c.innerText || '').toUpperCase();
                if ((txt.includes('GSTR-2B') || txt.includes('AUTO - DRAFTED ITC') || txt.includes('AUTO-DRAFTED ITC'))
                    && !txt.includes('GSTR-1') && !txt.includes('GSTR-3B') && !txt.includes('GSTR-2A')) {
                    const btns = Array.from(c.querySelectorAll('button, a, input[type="button"]'));
                    const dlBtn = btns.find(b => {
                        const t = (b.innerText || b.value || '').trim().toUpperCase();
                        return t === 'DOWNLOAD';
                    });
                    if (dlBtn) {
                        dlBtn.scrollIntoView();
                        dlBtn.click();
                        return true;
                    }
                }
            }
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            let node;
            while (node = walker.nextNode()) {
                const val = (node.nodeValue || '').trim();
                if (/^GSTR-?2B$/i.test(val)) {
                    let p = node.parentElement;
                    while (p && p !== document.body) {
                        const btns = Array.from(p.querySelectorAll('button, a, input[type="button"]'));
                        const dl = btns.find(b => (b.innerText || b.value || '').trim().toUpperCase() === 'DOWNLOAD');
                        if (dl) {
                            dl.scrollIntoView();
                            dl.click();
                            return true;
                        }
                        p = p.parentElement;
                    }
                }
            }
            return false;
        }""")
        if clicked:
            print("Portal Bridge: Clicked DOWNLOAD button on GSTR-2B tile successfully.")
            page.wait_for_timeout(2500)
            return True
    except Exception as e:
        print(f"click_gstr2b_download_button evaluate note: {e}")

    try:
        card_loc = page.locator("div:has-text('Auto - drafted ITC Statement'), div:has-text('Auto-drafted ITC Statement')").first
        if card_loc.is_visible(timeout=3000):
            dl_btn = card_loc.locator("button:has-text('DOWNLOAD'), a:has-text('DOWNLOAD')").first
            if dl_btn.is_visible(timeout=2000):
                dl_btn.scroll_into_view_if_needed()
                dl_btn.click()
                page.wait_for_timeout(2500)
                return True
    except Exception as e:
        print(f"click_gstr2b_download_button locator note: {e}")

    return False


def trigger_excel_download_on_statement_page(page, period_code: str, timeout_seconds: int = 15) -> Optional[str]:
    """
    Triggers Excel download on GSTR-2B statement screen:
    - Clicks 'Click here to download - File 1' if already generated
    - If not generated, clicks 'GENERATE EXCEL FILE TO DOWNLOAD' and waits for File 1 link
    - Catches Playwright download event or newly written files in Downloads directory
    """
    start_time = time.time()
    captured_file = None

    print(f"Portal Bridge: On GSTR-2B download screen for period {period_code}. Inspecting Excel download options...")
    page.wait_for_timeout(2000)

    # Check if 'Click here to download - File 1' is present
    dl_clicked = False
    try:
        dl_link = page.locator(
            "a:has-text('Click here to download - File 1'), "
            "a:has-text('Click here to download'), "
            "button:has-text('Click here to download'), "
            "a:has-text('File 1')"
        ).first
        if dl_link.is_visible(timeout=3000):
            print(f"Portal Bridge: Found direct Excel download link ('File 1') for {period_code}. Clicking...")
            dl_link.scroll_into_view_if_needed()
            dl_link.click()
            dl_clicked = True
    except Exception as e:
        print(f"Direct download link note: {e}")

    # If direct download link was not visible, click 'GENERATE EXCEL FILE TO DOWNLOAD'
    if not dl_clicked:
        try:
            gen_btn = page.locator(
                "button:has-text('GENERATE EXCEL FILE TO DOWNLOAD'), "
                "button:has-text('GENERATE EXCEL'), "
                "a:has-text('GENERATE EXCEL'), "
                "input[value*='GENERATE EXCEL']"
            ).first
            if gen_btn.is_visible(timeout=3000):
                print(f"Portal Bridge: Clicking 'GENERATE EXCEL FILE TO DOWNLOAD' for {period_code}...")
                gen_btn.scroll_into_view_if_needed()
                gen_btn.click()
                page.wait_for_timeout(3000)

                for _ in range(5):
                    chk_link = page.locator("a:has-text('Click here to download - File 1'), a:has-text('Click here to download')").first
                    if chk_link.is_visible(timeout=1500):
                        print(f"Portal Bridge: Download link appeared! Clicking File 1 for {period_code}...")
                        chk_link.click()
                        dl_clicked = True
                        break
                    page.wait_for_timeout(1000)
        except Exception as e:
            print(f"Generate Excel button note: {e}")

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        recent = scan_recent_downloads(max_age_seconds=int(time.time() - start_time) + 15)
        if recent:
            for r in recent:
                f_name = r["file_name"].upper()
                if period_code[:2] in f_name or "GSTR2B" in f_name or "2B" in f_name:
                    captured_file = r["file_path"]
                    print(f"Portal Bridge: Captured statement for {period_code}: {captured_file}")
                    return captured_file
            captured_file = recent[0]["file_path"]
            return captured_file
        page.wait_for_timeout(1000)

    return captured_file


def navigate_back_to_returns_dashboard(page, timeout_seconds: int = 15) -> bool:
    """
    Safely returns from GSTR-2B download screen to the Returns Dashboard (File Returns).
    Preserves Angular SPA session state and strictly avoids cross-domain redirects to gst.gov.in.
    Never clicks global nav links like 'Services' or footer links like 'Feedback'.
    """
    print("Portal Bridge: Navigating back to Returns Dashboard for next period...")

    # Step 0: Check if already on Returns Dashboard
    if "returns/auth/dashboard" in page.url.lower():
        try:
            fy_box = page.locator("select[id*='fin'], select[name*='fin']").first
            if fy_box.is_visible(timeout=1000):
                print("Portal Bridge: Already on Returns Dashboard.")
                return True
        except Exception:
            pass

    # Step 1: In-page DOM action to return to dashboard
    try:
        res = page.evaluate(r"""() => {
            // A. Look for dedicated BACK button in statement card / form (strictly BUTTON or INPUT)
            const allButtons = Array.from(document.querySelectorAll('button, input[type="button"], input[type="submit"]'));
            for (const b of allButtons) {
                const t = (b.innerText || b.value || '').trim().toUpperCase();
                // Strictly exclude 'FEEDBACK' or other words containing 'back'
                if ((t === 'BACK' || t === 'CANCEL' || t === 'BACK TO DASHBOARD') && !t.includes('FEEDBACK')) {
                    b.scrollIntoView();
                    b.click();
                    return 'CLICKED_BACK_BUTTON';
                }
            }

            // B. Breadcrumb link strictly inside breadcrumb container (ol.breadcrumb, .breadcrumb)
            const bLinks = Array.from(document.querySelectorAll('.breadcrumb a, ol.breadcrumb a, ul.breadcrumb a, div.breadcrumb a'));
            for (const a of bLinks) {
                const t = (a.innerText || '').trim().toUpperCase();
                if (t === 'RETURNS' || t === 'RETURN DASHBOARD' || t === 'RETURNS DASHBOARD' || t === 'FILE RETURNS') {
                    const h = (a.href || '').toLowerCase();
                    // Never click external links to www.gst.gov.in or feedback
                    if (!h.includes('www.gst.gov.in') && !h.includes('/feedback') && !h.includes('services.gst.gov.in/services/login')) {
                        a.scrollIntoView();
                        a.click();
                        return 'CLICKED_BREADCRUMB';
                    }
                }
            }

            // C. Look for breadcrumb item 'Returns' by class or structure
            for (const a of bLinks) {
                const t = (a.innerText || '').trim().toUpperCase();
                if (t.includes('RETURN')) {
                    const h = (a.href || '').toLowerCase();
                    if (!h.includes('www.gst.gov.in') && !h.includes('/feedback')) {
                        a.scrollIntoView();
                        a.click();
                        return 'CLICKED_BREADCRUMB_PARTIAL';
                    }
                }
            }

            // D. Same-origin SPA router transition on return.gst.gov.in
            if (window.location.host.includes('return.gst.gov.in')) {
                window.location.href = '/returns/auth/dashboard';
                return 'ROUTED_VIA_LOCATION_HREF';
            }

            return 'NONE';
        }""")
        print(f"Portal Bridge: Return navigation evaluated -> {res}")
    except Exception as e:
        print(f"Portal Bridge: In-page nav evaluate note: {e}")

    # Step 2: Wait for Returns Dashboard elements to become visible
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        curr_url = page.url.lower()

        # If arrived on returns dashboard, verify FY select box is visible
        if "returns/auth/dashboard" in curr_url or "returns" in curr_url:
            try:
                fy_box = page.locator("select[id*='fin'], select[name*='fin']").first
                if fy_box.is_visible(timeout=1500):
                    print("Portal Bridge: Successfully returned to Returns Dashboard!")
                    page.wait_for_timeout(1000)
                    return True
            except Exception:
                pass

        # If landed on fowelcome landing screen
        if "fowelcome" in curr_url:
            print("Portal Bridge: On fowelcome landing page. Re-opening Returns Dashboard...")
            navigate_from_welcome_to_returns_dashboard(page)
            page.wait_for_timeout(2000)
            continue

        # If accidentally bounced to www.gst.gov.in or services.gst.gov.in without returns path
        if ("www.gst.gov.in" in curr_url or "services.gst.gov.in" in curr_url) and "returns" not in curr_url:
            print(f"Portal Bridge: Detected external URL ({curr_url}). Recovering to Returns Dashboard...")
            try:
                page.goto("https://services.gst.gov.in/services/auth/fowelcome", wait_until="domcontentloaded", timeout=12000)
                page.wait_for_timeout(2000)
                navigate_from_welcome_to_returns_dashboard(page)
            except Exception:
                try:
                    page.goto("https://return.gst.gov.in/returns/auth/dashboard", wait_until="domcontentloaded", timeout=12000)
                except Exception:
                    pass

        page.wait_for_timeout(1000)

    return "dashboard" in page.url.lower()


def consolidate_gstr2b_statements(file_paths: List[str], fy: str = "2026-27") -> pd.DataFrame:
    """
    Consolidate multiple monthly GSTR-2B statements (April to October) into a unified dataset.
    Finance-first: Ingests each file, tags rows with statutory Return Period,
    concatenates via pd.concat, and deduplicates by (GSTIN, Invoice number).
    Saves consolidated file into cache for direct Excel audit.
    """
    if not file_paths:
        return pd.DataFrame()

    month_names = {
        "04": "April", "05": "May", "06": "June",
        "07": "July", "08": "August", "09": "September",
        "10": "October", "11": "November", "12": "December",
        "01": "January", "02": "February", "03": "March"
    }

    dfs = []
    for f_idx, fp in enumerate(file_paths):
        if not os.path.exists(fp):
            continue
        try:
            df = ingest_gstr2b_file(fp)
            if not df.empty:
                import re
                base = os.path.basename(fp)
                period_tag = f"YTD {fy}"

                # Match official GST Portal pattern GSTR2B_MMYYYY_
                m_match = re.search(r"GSTR2B_(\d{2})(\d{4})", base, re.IGNORECASE)
                if m_match:
                    mm = m_match.group(1)
                    yr = m_match.group(2)
                    m_label = month_names.get(mm, f"Month {mm}")
                    period_tag = f"{m_label} {yr}"
                else:
                    # Fallback to checking month name in filename
                    for m_code, m_name in month_names.items():
                        if m_name.lower() in base.lower():
                            period_tag = f"{m_name} {fy}"
                            break

                if "Return Period" not in df.columns:
                    df["Return Period"] = period_tag
                df["_tw_src_file_idx"] = f_idx
                dfs.append(df)
        except Exception as ex:
            print(f"Warning: Failed to ingest statement '{fp}': {ex}")

    if not dfs:
        return pd.DataFrame()

    merged_df = pd.concat(dfs, ignore_index=True)

    gstin_col = None
    inv_col = None
    for col in merged_df.columns:
        cl = col.lower()
        if ("gstin" in cl or "gst no" in cl) and not gstin_col:
            gstin_col = col
        if ("invoice number" in cl or "inum" in cl or "invoice no" in cl) and not inv_col:
            inv_col = col

    if gstin_col and inv_col:
        before_count = len(merged_df)
        latest_idx = merged_df.groupby([gstin_col, inv_col], dropna=False)["_tw_src_file_idx"].transform("max")
        merged_df = merged_df[merged_df["_tw_src_file_idx"] == latest_idx]
        after_count = len(merged_df)
        if before_count != after_count:
            print(f"Portal Bridge: Deduplicated {before_count - after_count} overlapping invoices across monthly batches.")
    merged_df = merged_df.drop(columns=["_tw_src_file_idx"], errors="ignore")

    try:
        consolidated_path = os.path.join(DOWNLOADS_DIR, f"GSTR2B_Consolidated_YTD_{fy.replace('-', '_')}.xlsx")
        with pd.ExcelWriter(consolidated_path, engine="openpyxl") as writer:
            merged_df.to_excel(writer, sheet_name="B2B", index=False)
        print(f"Portal Bridge: Saved consolidated Excel statement to: {consolidated_path}")
    except Exception as save_ex:
        print(f"Warning: Failed to save consolidated Excel file: {save_ex}")

    return merged_df


def _launch_browser(playwright_instance) -> Any:
    """
    Intelligently launch browser with resilient Windows fallback:
    1. Try bundled or default Chromium.
    2. Try pre-installed Microsoft Edge (channel='msedge') on Windows.
    3. Try pre-installed Google Chrome (channel='chrome').
    """
    args = [
        "--start-maximized",
        "--disable-blink-features=AutomationControlled"
    ]
    # 1. Try default Chromium
    try:
        return playwright_instance.chromium.launch(headless=False, args=args)
    except Exception:
        pass

    # 2. Try Microsoft Edge (built-in on 100% of Windows 10/11 machines)
    try:
        return playwright_instance.chromium.launch(channel="msedge", headless=False, args=args)
    except Exception:
        pass

    # 3. Try Google Chrome
    try:
        return playwright_instance.chromium.launch(channel="chrome", headless=False, args=args)
    except Exception:
        pass

    raise RuntimeError(
        "Could not launch browser (Chromium, Edge, or Chrome). "
        "Please ensure Microsoft Edge or Google Chrome is installed on this PC."
    )


def execute_portal_bridge_sync(
    fy: str,
    return_period: str,
    username: str,
    password: str,
    timeout_seconds: int = 240,
    return_type: str = "GSTR-2B",
    progress_cb: Optional[Callable[[int, str], None]] = None
) -> Dict[str, Any]:
    """
    Synchronous Playwright bridge connecting to https://services.gst.gov.in/services/login.
    Avoids artificial URLs that trigger F5 BIG-IP WAF rejection.
    Handles fowelcome landing screen via in-page click or top navbar SSO handover,
    accurately selects Financial Year, Quarter, and target Month (e.g. April),
    and runs a dual listener (Playwright download event + local filesystem watcher)
    to capture GSTR-2B whether triggered by automation or direct user click.
    """
    def _report(step: int, msg: str):
        if progress_cb:
            try:
                progress_cb(step, msg)
            except Exception:
                pass

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "Playwright browser automation requires a local desktop environment (via TallyWalls Connector or desktop app)."
        )

    login_url = "https://services.gst.gov.in/services/login"
    start_time = time.time()
    captured_file: Optional[str] = None
    p_info = get_return_period_details(return_period)

    # Automatic fallback to saved credentials from local AES-256 storage
    if not username or not password:
        stored_creds = load_credentials()
        username = username or stored_creds.get("username", "")
        password = password or stored_creds.get("password", "")

    _report(1, "Launching desktop browser & connecting to GST Portal...")
    with sync_playwright() as p:
        browser = _launch_browser(p)
        context = browser.new_context(
            no_viewport=True,
            accept_downloads=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )

        def handle_download(download):
            nonlocal captured_file
            fn = download.suggested_filename.upper()
            if "GSTR1" in fn or "GSTR-1" in fn or "_R1_" in fn or "_R1." in fn:
                print(f"Portal Bridge: REJECTED GSTR-1 download: {download.suggested_filename}. Waiting for GSTR-2B...")
                return
            try:
                dest = os.path.join(DOWNLOADS_DIR, f"GSTR2B_{return_period}_{download.suggested_filename}")
                download.save_as(dest)
                captured_file = dest
                print(f"Portal Bridge: Successfully captured GSTR-2B download: {dest}")
            except Exception as ex:
                print(f"Portal Bridge Download Event Note: {ex}")

        context.on("download", handle_download)
        page = context.new_page()
        page.on("download", handle_download)

        try:
            # Step 1: Open Login Page
            print("Portal Bridge: Opening GST Login Portal...")
            page.goto(login_url, timeout=30000, wait_until="domcontentloaded")

            # Step 2: Inject prominent assistive HUD banner at the top of the viewport
            try:
                page.evaluate(f"""() => {{
                    let hud = document.getElementById('fin-recon-hud');
                    if (!hud) {{
                        hud = document.createElement('div');
                        hud.id = 'fin-recon-hud';
                        document.body.prepend(hud);
                    }}
                    hud.style.cssText = 'position:fixed; top:0; left:0; right:0; z-index:99999999; background:#0f172a; color:#f8fafc; padding:12px 24px; font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif; font-size:13px; font-weight:600; display:flex; align-items:center; justify-content:space-between; box-shadow:0 4px 16px rgba(0,0,0,0.5); border-bottom:2px solid #2563eb;';
                    hud.innerHTML = `
                        <div style="display:flex; align-items:center; gap:12px;">
                            <span style="background:#2563eb; color:#fff; font-size:11px; font-weight:700; letter-spacing:0.04em; text-transform:uppercase; padding:4px 10px; border-radius:4px;">GST Reconciliation Hub</span>
                            <span id="fin-recon-hud-text" style="color:#e2e8f0;">Target: <b>FY {fy} • {p_info['name']} • GSTR-2B</b>. Mounting login form...</span>
                        </div>
                        <span style="font-size:11px; color:#10b981; font-weight:600;">Bridge: Active</span>
                    `;
                }}""")
            except Exception as e:
                print(f"HUD injection note: {e}")

            # Step 3: Explicitly wait for Angular login form elements to mount
            print("Portal Bridge: Waiting for GST Portal login inputs to become visible...")
            page.wait_for_selector("#username", state="visible", timeout=20000)
            page.wait_for_selector("#user_pass", state="visible", timeout=10000)
            page.wait_for_timeout(600)

            # Step 4: Autofill Username and Password with two-way Angular event synchronization
            try:
                if username:
                    u_loc = page.locator("#username").first
                    u_loc.scroll_into_view_if_needed()
                    u_loc.click()
                    u_loc.fill(username)
                    page.evaluate("""(u) => {
                        const el = document.getElementById('username');
                        if (el) {
                            el.value = u;
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                    }""", username)
                    print(f"Portal Bridge: Autofilled username: {username[:3]}***")

                if password:
                    p_loc = page.locator("#user_pass").first
                    p_loc.scroll_into_view_if_needed()
                    p_loc.click()
                    p_loc.fill(password)
                    page.evaluate("""(p) => {
                        const el = document.getElementById('user_pass');
                        if (el) {
                            el.value = p;
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                    }""", password)
                    print("Portal Bridge: Autofilled password securely.")

                # Wait for the dynamic CAPTCHA box to render after credentials entry
                print("Portal Bridge: Waiting for CAPTCHA image and input to render...")
                try:
                    c_el = page.wait_for_selector("#captcha", state="visible", timeout=10000)
                    if c_el:
                        c_el.click()
                        c_el.focus()
                        print("Portal Bridge: Focused blinking cursor into CAPTCHA box!")
                except Exception as ce:
                    print(f"Captcha focus note: {ce}")

                # Update HUD banner instruction
                page.evaluate("""() => {
                    const el = document.getElementById('fin-recon-hud-text');
                    if (el) el.innerHTML = 'Credentials Auto-Filled! Enter the 6 CAPTCHA characters and click <b>LOGIN</b>.';
                }""")
            except Exception as e:
                print(f"Autofill note: {e}")

            _report(2, "Credentials autofilled! Waiting for CAPTCHA entry & login in browser...")

            # Step 4: Wait for user to complete CAPTCHA & Authentication
            print(f"Portal Bridge: Waiting for login (timeout: {timeout_seconds}s)...")
            authenticated = False
            deadline = time.time() + timeout_seconds

            while time.time() < deadline:
                if captured_file and os.path.exists(captured_file):
                    break

                if page.is_closed():
                    recent = scan_recent_downloads(max_age_seconds=120)
                    if recent:
                        captured_file = recent[0]["file_path"]
                        break
                    raise Exception("Browser was closed before download completed.")

                curr_url = page.url.lower()

                if "/services/login" not in curr_url and ("/services/auth" in curr_url or "/returns/auth" in curr_url or "dashboard" in curr_url or "quicklinks" in curr_url or "fowelcome" in curr_url):
                    authenticated = True
                    break

                page.wait_for_timeout(1000)

            if not authenticated and not captured_file:
                raise Exception("Authentication timed out. Please check your credentials and CAPTCHA.")

            print("Portal Bridge: Authentication confirmed!")
            _report(3, f"Authenticated! Navigating to Return Dashboard for {p_info['name']} ({fy})...")

            # Update HUD banner
            try:
                page.evaluate(f"""() => {{
                    const el = document.getElementById('fin-recon-hud-text');
                    if (el) el.innerHTML = 'Authentication Successful! Opening Return Dashboard for <b>{p_info["name"]}</b>...';
                }}""")
            except Exception:
                pass

            # Step 5: Handle fowelcome or quicklinks landing page
            page.wait_for_timeout(2000)
            if "fowelcome" in page.url.lower() or "/services/auth" in page.url.lower():
                navigate_from_welcome_to_returns_dashboard(page)

            # Step 6: On Returns Dashboard - Perform selection exactly ONCE
            dash_wait_deadline = time.time() + 180
            search_attempted = False

            while time.time() < dash_wait_deadline:
                if captured_file and os.path.exists(captured_file):
                    break

                if page.is_closed():
                    break

                curr_url = page.url.lower()

                # If session bounced back to login page unexpectedly
                if "/services/login" in curr_url and search_attempted:
                    print("Portal Bridge: Session bounced to login page. Checking recent downloads before terminating...")
                    recent = scan_recent_downloads(max_age_seconds=int(time.time() - start_time) + 15)
                    if recent:
                        captured_file = recent[0]["file_path"]
                        break
                    raise Exception("GST Portal session disconnected or redirected to login. Please re-run or download directly in the browser.")

                # If on fowelcome
                if "fowelcome" in curr_url and not search_attempted:
                    navigate_from_welcome_to_returns_dashboard(page)
                    page.wait_for_timeout(2000)
                    continue

                if ("returns" in curr_url or "dashboard" in curr_url) and not search_attempted:
                    print(f"Portal Bridge: On Return Dashboard. Targeting GSTR-2B for FY {fy} - {p_info['name']} ({return_type})...")
                    search_attempted = True  # Set immediately so it NEVER repeats or spams dropdowns!
                    select_period_and_trigger_download(page, fy, return_period, return_type)

                # If user navigated to the GSTR-2B download screen, highlight Excel button
                if "download" in curr_url or "gstr2b" in curr_url:
                    try:
                        page.evaluate(r"""() => {
                            const genExcelBtn = Array.from(document.querySelectorAll('button, a, input[type="button"]')).find(el => {
                                const t = (el.innerText || el.value || '').toUpperCase();
                                return t.includes('GENERATE EXCEL') || (t.includes('EXCEL') && t.includes('DOWNLOAD'));
                            });
                            if (genExcelBtn && !genExcelBtn.dataset.highlighted) {
                                genExcelBtn.dataset.highlighted = 'true';
                                genExcelBtn.style.backgroundColor = '#16a34a';
                                genExcelBtn.style.color = '#ffffff';
                                genExcelBtn.style.fontWeight = 'bold';
                                genExcelBtn.style.border = '2px solid #15803d';
                                genExcelBtn.style.boxShadow = '0 0 16px rgba(22, 163, 74, 0.4)';
                            }

                            const dlLink = Array.from(document.querySelectorAll('a, button')).find(el => {
                                const t = (el.innerText || '').toUpperCase();
                                return t.includes('CLICK HERE TO DOWNLOAD') || t.includes('FILE 1');
                            });
                            if (dlLink && !dlLink.dataset.highlighted) {
                                dlLink.dataset.highlighted = 'true';
                                dlLink.style.backgroundColor = '#2563eb';
                                dlLink.style.color = '#ffffff';
                                dlLink.style.fontWeight = 'bold';
                                dlLink.style.padding = '8px 14px';
                                dlLink.style.borderRadius = '4px';
                            }

                            const hud = document.getElementById('fin-recon-hud-text');
                            if (hud && !hud.innerHTML.includes('GENERATE EXCEL')) {
                                hud.innerHTML = 'GSTR-2B Statement Page: Click <b>GENERATE EXCEL FILE</b> (or File 1 to download Excel). The app will capture it automatically!';
                            }
                        }""")
                    except Exception:
                        pass

                # Check user's Downloads folder for new files created since bridge started (strictly prioritizing Excel)
                user_downloads = scan_recent_downloads(max_age_seconds=int(time.time() - start_time) + 10)
                if user_downloads:
                    captured_file = user_downloads[0]["file_path"]
                    print(f"Portal Bridge: Detected GSTR-2B statement in Downloads folder: {captured_file}")
                    break

                page.wait_for_timeout(1000)

            # Final check in downloads before giving up
            if not captured_file or not os.path.exists(captured_file):
                recent = scan_recent_downloads(max_age_seconds=180)
                if recent:
                    captured_file = recent[0]["file_path"]

            if not captured_file or not os.path.exists(captured_file):
                raise Exception(
                    f"GSTR-2B download for {p_info['name']} ({fy}) was not captured within the timeout. "
                    "You can also download the file directly in the browser window and click 'Scan Recent Downloads' in the app."
                )

            # Ingest and validate parsed DataFrame
            print(f"Portal Bridge: Ingesting captured file: {captured_file}")
            df = ingest_gstr2b_file(captured_file)

            if df.empty:
                raise Exception(f"The downloaded GSTR-2B file for {p_info['name']} contains 0 invoice records or is empty.")

            _report(4, f"Captured & parsed {len(df)} GSTR-2B invoices from {os.path.basename(captured_file)}!")
            browser.close()
            return {
                "status": "success",
                "file_path": captured_file,
                "rows_count": len(df),
                "data_preview": sanitize_df_for_json(df)
            }

        except Exception as e:
            if not page.is_closed():
                try:
                    browser.close()
                except Exception:
                    pass
            raise e


def _resolve_sample_file(filename: str) -> Optional[str]:
    meipass = getattr(sys, "_MEIPASS", None)
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    candidates = [
        os.path.join(meipass, "sample_data", filename) if meipass else "",
        os.path.join(project_root, "sample_data", filename),
        os.path.join(os.getcwd(), "sample_data", filename),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def simulate_portal_download(fy: str, return_period: str) -> pd.DataFrame:
    """Simulation mode: Returns benchmark GSTR-2B dataset for demonstration."""
    sample_file = _resolve_sample_file("sample_gstr2b.xlsx")
    if sample_file and os.path.exists(sample_file):
        return pd.read_excel(sample_file, sheet_name="B2B")
    return pd.DataFrame()


def simulate_multi_month_download(fy: str = "2026-27", start_month: str = "04", end_month: str = "10") -> pd.DataFrame:
    """Simulation mode: Generates consolidated multi-month GSTR-2B dataset across April to October."""
    df_base = simulate_portal_download(fy, "042026")
    if df_base.empty:
        return pd.DataFrame()

    months = get_fy_month_sequence(start_month, end_month, fy)
    dfs = []
    for idx, m in enumerate(months):
        df_copy = df_base.copy()
        # Stagger invoice numbers and return periods across months
        inv_col = [c for c in df_copy.columns if "invoice" in c.lower() and "number" in c.lower()]
        if inv_col:
            df_copy[inv_col[0]] = df_copy[inv_col[0]].astype(str) + f"-{m['code']}"
        df_copy["Return Period"] = m["period_label"]
        dfs.append(df_copy)

    merged = pd.concat(dfs, ignore_index=True)
    return merged


def execute_multi_month_portal_bridge_sync(
    fy: str = "2026-27",
    start_month: str = "04",
    end_month: str = "10",
    username: str = "",
    password: str = "",
    timeout_seconds: int = 480,
    return_type: str = "GSTR-2B",
    progress_cb: Optional[Callable[[int, str], None]] = None
) -> Dict[str, Any]:
    """
    Automated Multi-Month Batch GSTR-2B Sync across Financial Year sequence (e.g. April to October).
    1. Connects to services.gst.gov.in/services/login, autofills credentials.
    2. Waits for single user CAPTCHA entry and authentication.
    3. Navigates to Returns Dashboard.
    4. Loops through each month (April, May, June, July, August, September, October):
       - Selects Quarter and target Month
       - Clicks SEARCH
       - Clicks DOWNLOAD specifically on GSTR-2B tile
       - Triggers Excel download (File 1 or Generate Excel)
       - Catches file download
       - Safely returns to Dashboard via in-page navigation (never page.go_back)
    5. Consolidates all 7 monthly Excel statements into a single unified audit dataset.
    """
    def _report(step: int, msg: str):
        if progress_cb:
            try:
                progress_cb(step, msg)
            except Exception:
                pass

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "Playwright browser automation requires a local desktop environment (via TallyWalls Connector or desktop app)."
        )

    login_url = "https://services.gst.gov.in/services/login"
    start_time = time.time()
    captured_files: List[str] = []
    month_sequence = get_fy_month_sequence(start_month, end_month, fy)

    if not username or not password:
        stored_creds = load_credentials()
        username = username or stored_creds.get("username", "")
        password = password or stored_creds.get("password", "")

    _report(1, f"Launching desktop browser for {len(month_sequence)}-month batch sync ({fy})...")
    with sync_playwright() as p:
        browser = _launch_browser(p)
        context = browser.new_context(
            no_viewport=True,
            accept_downloads=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )

        def handle_batch_download(download):
            nonlocal captured_files
            fn = download.suggested_filename.upper()
            if "GSTR1" in fn or "GSTR-1" in fn or "_R1_" in fn or "_R1." in fn:
                print(f"Portal Bridge: REJECTED GSTR-1 download: {download.suggested_filename}")
                return
            try:
                dest = os.path.join(DOWNLOADS_DIR, f"GSTR2B_Batch_{download.suggested_filename}")
                download.save_as(dest)
                if dest not in captured_files:
                    captured_files.append(dest)
                print(f"Portal Bridge: Captured statement via download event: {dest}")
            except Exception as ex:
                print(f"Batch Download Event note: {ex}")

        context.on("download", handle_batch_download)
        page = context.new_page()
        page.on("download", handle_batch_download)

        try:
            print("Portal Bridge: Starting Multi-Month Batch Session (April - October)...")
            page.goto(login_url, timeout=30000, wait_until="domcontentloaded")

            # Inject HUD Banner
            try:
                page.evaluate(f"""() => {{
                    let hud = document.getElementById('fin-recon-hud');
                    if (!hud) {{
                        hud = document.createElement('div');
                        hud.id = 'fin-recon-hud';
                        document.body.prepend(hud);
                    }}
                    hud.style.cssText = 'position:fixed; top:0; left:0; right:0; z-index:99999999; background:#0f172a; color:#f8fafc; padding:12px 24px; font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif; font-size:13px; font-weight:600; display:flex; align-items:center; justify-content:space-between; box-shadow:0 4px 16px rgba(0,0,0,0.5); border-bottom:2px solid #2563eb;';
                    hud.innerHTML = `
                        <div style="display:flex; align-items:center; gap:12px;">
                            <span style="background:#2563eb; color:#fff; font-size:11px; font-weight:700; letter-spacing:0.04em; text-transform:uppercase; padding:4px 10px; border-radius:4px;">GST Batch Sync Engine</span>
                            <span id="fin-recon-hud-text" style="color:#e2e8f0;">Target: <b>Multi-Month Batch ({len(month_sequence)} Months: {month_sequence[0]['name']} - {month_sequence[-1]['name']} FY {fy})</b>. Mounting login...</span>
                        </div>
                        <span style="font-size:11px; color:#10b981; font-weight:600;">Status: Ready for Login</span>
                    `;
                }}""")
            except Exception as e:
                print(f"HUD injection note: {e}")

            # Autofill credentials
            page.wait_for_selector("#username", state="visible", timeout=20000)
            page.wait_for_selector("#user_pass", state="visible", timeout=10000)
            page.wait_for_timeout(600)

            if username:
                u_loc = page.locator("#username").first
                u_loc.scroll_into_view_if_needed()
                u_loc.click()
                u_loc.fill(username)
                page.evaluate("""(u) => {
                    const el = document.getElementById('username');
                    if (el) {
                        el.value = u;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }""", username)

            if password:
                p_loc = page.locator("#user_pass").first
                p_loc.scroll_into_view_if_needed()
                p_loc.click()
                p_loc.fill(password)
                page.evaluate("""(p) => {
                    const el = document.getElementById('user_pass');
                    if (el) {
                        el.value = p;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }""", password)

            try:
                c_el = page.wait_for_selector("#captcha", state="visible", timeout=10000)
                if c_el:
                    c_el.click()
                    c_el.focus()
            except Exception:
                pass

            page.evaluate(f"""() => {{
                const el = document.getElementById('fin-recon-hud-text');
                if (el) el.innerHTML = 'Credentials Autofilled! Enter the 6 CAPTCHA letters and click <b>LOGIN</b>. The agent will then download all {len(month_sequence)} months automatically.';
            }}""")

            _report(2, "Credentials autofilled! Enter CAPTCHA & click LOGIN in the browser...")

            # Await user authentication
            authenticated = False
            deadline = time.time() + timeout_seconds
            while time.time() < deadline:
                if page.is_closed():
                    raise Exception("Browser was closed before batch download began.")
                curr_url = page.url.lower()
                if "/services/login" not in curr_url and ("/services/auth" in curr_url or "/returns/auth" in curr_url or "dashboard" in curr_url or "quicklinks" in curr_url or "fowelcome" in curr_url):
                    authenticated = True
                    break
                page.wait_for_timeout(1000)

            if not authenticated:
                raise Exception("Login timed out. Please check your credentials and CAPTCHA.")

            print("Portal Bridge: Authenticated! Preparing multi-month batch execution...")
            _report(3, f"Authenticated! Preparing {len(month_sequence)}-month download sequence...")
            page.wait_for_timeout(2000)

            # Navigate from fowelcome if needed
            if "fowelcome" in page.url.lower() or "/services/auth" in page.url.lower():
                navigate_from_welcome_to_returns_dashboard(page)
                page.wait_for_timeout(2000)

            # -------------------------------------------------------------
            # Execute Sequential Multi-Month Loop (April to October)
            # -------------------------------------------------------------
            for idx, m_info in enumerate(month_sequence, 1):
                if page.is_closed():
                    break

                m_name = m_info["name"]
                m_code = m_info["code"]
                q_label = m_info["quarter_label"]
                print(f"\n=======================================================")
                print(f"Portal Bridge: Processing Month {idx}/{len(month_sequence)}: {m_name} (Quarter: {q_label}, FY: {fy})")
                print(f"=======================================================")
                _report(3, f"Downloading month {idx}/{len(month_sequence)}: {m_name} ({fy})...")

                # Update HUD
                try:
                    page.evaluate(f"""() => {{
                        const el = document.getElementById('fin-recon-hud-text');
                        if (el) el.innerHTML = 'Batch Progress ({idx}/{len(month_sequence)}): Selecting <b>{m_name} (Quarter {m_info['quarter_num']})</b>. Initiating search...';
                    }}""")
                except Exception:
                    pass

                # Ensure on Returns Dashboard before starting selection
                if not ("returns/auth/dashboard" in page.url.lower()):
                    navigate_back_to_returns_dashboard(page)
                    page.wait_for_timeout(1500)

                # Step 1: Period Selection & Search
                select_period_and_trigger_download(page, fy, m_code, return_type, force_select=True)
                page.wait_for_timeout(1500)

                # Step 2: Click GSTR-2B DOWNLOAD Button
                try:
                    page.evaluate(f"""() => {{
                        const el = document.getElementById('fin-recon-hud-text');
                        if (el) el.innerHTML = 'Batch Progress ({idx}/{len(month_sequence)}): Clicking <b>DOWNLOAD</b> on GSTR-2B for {m_name}...';
                    }}""")
                except Exception:
                    pass

                clicked_dl = click_gstr2b_download_button(page)
                page.wait_for_timeout(2500)

                # Step 3: Trigger Excel Download on Statement Screen
                try:
                    page.evaluate(f"""() => {{
                        const el = document.getElementById('fin-recon-hud-text');
                        if (el) el.innerHTML = 'Batch Progress ({idx}/{len(month_sequence)}): Triggering Excel statement download for <b>{m_name}</b>...';
                    }}""")
                except Exception:
                    pass

                m_file = trigger_excel_download_on_statement_page(page, m_info["full_period_code"], timeout_seconds=12)
                if m_file and m_file not in captured_files:
                    captured_files.append(m_file)
                    print(f"Portal Bridge: Successfully recorded statement for {m_name}: {m_file}")

                # Step 4: Safely Navigate Back to Returns Dashboard for next month
                if idx < len(month_sequence):
                    try:
                        page.evaluate(f"""() => {{
                            const el = document.getElementById('fin-recon-hud-text');
                            if (el) el.innerHTML = 'Batch Progress ({idx}/{len(month_sequence)}): {m_name} captured. Returning to Returns Dashboard...';
                        }}""")
                    except Exception:
                        pass
                    navigate_back_to_returns_dashboard(page)
                    page.wait_for_timeout(2000)

            # Scan downloads directory for any other statements downloaded during this session
            recent_all = scan_recent_downloads(max_age_seconds=int(time.time() - start_time) + 30)
            for r in recent_all:
                if r["file_path"] not in captured_files:
                    captured_files.append(r["file_path"])

            print(f"\nPortal Bridge: Multi-month batch download loop finished. Total files captured: {len(captured_files)}")

            # Consolidate all captured files
            df_merged = consolidate_gstr2b_statements(captured_files, fy=fy)

            # If still empty, attempt to read any cached files in cache/downloads
            if df_merged.empty:
                cached_files = [
                    os.path.join(DOWNLOADS_DIR, f) for f in os.listdir(DOWNLOADS_DIR)
                    if f.lower().endswith((".xlsx", ".xls", ".zip")) and "2b" in f.lower()
                ]
                if cached_files:
                    df_merged = consolidate_gstr2b_statements(cached_files, fy=fy)

            try:
                page.evaluate(f"""() => {{
                    const el = document.getElementById('fin-recon-hud-text');
                    if (el) el.innerHTML = 'Batch Complete! Consolidated <b>{len(df_merged)} invoices</b> across statutory months. Ingesting into Reconciliation Engine...';
                }}""")
                page.wait_for_timeout(2000)
            except Exception:
                pass

            _report(4, f"Consolidated {len(df_merged)} GSTR-2B invoices across {len(month_sequence)} months!")
            browser.close()

            return {
                "status": "success",
                "batch_mode": True,
                "months_processed": [m["name"] for m in month_sequence],
                "captured_files": captured_files,
                "rows_count": len(df_merged),
                "data_preview": sanitize_df_for_json(df_merged)
            }

        except Exception as e:
            if not page.is_closed():
                try:
                    browser.close()
                except Exception:
                    pass
            raise e
