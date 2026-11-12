"""
GSTR-9 Annual Return Ingestion & Statutory Parsers.
Parses Full-Year Outward (Sales Register, GSTR-1, GSTR-3B), Inward (12M Purchase Register,
12M Consolidated GSTR-2B + Sec 16(4) cutoff window), and HSN summaries into canonical
DataFrames / dicts ready for gstr9_engine.py.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from app.engine.column_mapper import detect_columns
from app.engine.normalizer import (
    clean_gstin,
    clean_invoice_number,
    infer_gst_rate_label,
    parse_invoice_date,
    parse_itc_ineligible,
    parse_rcm_flag,
)
from app.engine.portal_bridge import parse_gstr2b_excel


def _to_float(val: Any) -> float:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).replace(",", "").replace("₹", "").strip()
    if not s or s.lower() in ("nan", "none", "-", "nil"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _norm_col(col: Any) -> str:
    return re.sub(r"\s+", " ", str(col).replace("\n", " ").strip().lower())


def _find_col(columns: List[str], candidates: List[str]) -> Optional[str]:
    norm_map = {_norm_col(c): c for c in columns}
    # Exact match in candidate priority order
    for cand in candidates:
        c_low = cand.lower().strip()
        if c_low in norm_map:
            return norm_map[c_low]
    # Substring match in candidate priority order
    for cand in candidates:
        c_low = cand.lower().strip()
        for nc, orig in norm_map.items():
            if c_low in nc:
                return orig
    return None


def fy_bounds(fy: str, cutoff_month: int = 11) -> Tuple[date, date, date, date]:
    """
    Returns (fy_start, fy_end, cutoff_start, cutoff_end) for an Indian FY string like '2025-26'.
    cutoff_start = Apr 1 of next FY; cutoff_end = Nov 30 (or Oct 31) of next FY per Sec 16(4).
    """
    m = re.match(r"^\s*(\d{4})\s*[-/]\s*(\d{2,4})\s*$", str(fy or "2025-26"))
    start_year = int(m.group(1)) if m else 2025
    fy_start = date(start_year, 4, 1)
    fy_end = date(start_year + 1, 3, 31)
    cutoff_start = date(start_year + 1, 4, 1)
    if int(cutoff_month) == 10:
        cutoff_end = date(start_year + 1, 10, 31)
    else:
        cutoff_end = date(start_year + 1, 11, 30)
    return fy_start, fy_end, cutoff_start, cutoff_end


def classify_period_bucket(
    doc_date_val: Any,
    filing_period_val: Any,
    fy: str,
    cutoff_month: int = 11,
) -> str:
    """
    Classify a record into:
      - 'IN_FY': Pertains to FY and reported/booked inside Apr-Mar of FY.
      - 'NEXT_FY_CUTOFF': Pertains to FY (or reported in Apr-Nov next FY window).
      - 'TIME_BARRED': Reported after Sec 16(4) cutoff (post Nov 30 of next FY).
      - 'OUTSIDE_FY': Belongs to a different FY completely.
    """
    fy_start, fy_end, cutoff_start, cutoff_end = fy_bounds(fy, cutoff_month)
    doc_dt = parse_invoice_date(doc_date_val)
    rep_dt = parse_invoice_date(filing_period_val) if filing_period_val not in (None, "", "-") else None

    if doc_dt is None and rep_dt is None:
        return "IN_FY"

    if doc_dt is not None:
        if doc_dt < fy_start:
            return "OUTSIDE_FY"
        if fy_start <= doc_dt <= fy_end:
            if rep_dt is not None:
                if rep_dt > cutoff_end:
                    return "TIME_BARRED"
                if cutoff_start <= rep_dt <= cutoff_end:
                    return "NEXT_FY_CUTOFF"
            return "IN_FY"
        if cutoff_start <= doc_dt <= cutoff_end:
            return "NEXT_FY_CUTOFF"
        return "OUTSIDE_FY"

    # Fallback when only filing/reporting date is present
    if rep_dt is not None:
        if fy_start <= rep_dt <= fy_end:
            return "IN_FY"
        if cutoff_start <= rep_dt <= cutoff_end:
            return "NEXT_FY_CUTOFF"
        return "OUTSIDE_FY"
    return "IN_FY"


def _clean_hsn(val: Any) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    if isinstance(val, float) and val.is_integer():
        return str(int(val))
    s = str(val).strip()
    if re.fullmatch(r"\d+\.0+", s):
        s = s.split(".")[0]
    return re.sub(r"[^A-Za-z0-9]", "", s)


def _infer_rate_pct(row_taxable: float, total_tax: float, explicit_rate: Any) -> float:
    r = _to_float(explicit_rate)
    if r > 0:
        return round(r, 2)
    if row_taxable > 0 and total_tax > 0:
        raw_pct = (total_tax / row_taxable) * 100.0
        for slab in (0.0, 0.25, 3.0, 5.0, 12.0, 18.0, 28.0):
            if abs(raw_pct - slab) <= 0.35:
                return slab
        return round(raw_pct, 2)
    return 0.0


def _detect_outward_category(
    vch_type: str,
    sup_cat: str,
    gstin: str,
    taxable: float,
    total_tax: float,
    rcm_flag: bool,
) -> str:
    """
    Map an outward supply row to canonical GSTR-9 buckets:
      B2B, B2C, EXPORT_WP, EXPORT_WOP, SEZ_WP, SEZ_WOP, DEEMED_EXP,
      CREDIT_NOTE, DEBIT_NOTE, EXEMPT, NIL, NON_GST, RCM_INWARD
    """
    sc = (sup_cat or "").upper().strip()
    vt = (vch_type or "").upper().strip()

    if "RCM" in sc or "REVERSE" in sc or rcm_flag:
        return "RCM_INWARD"
    if "CREDIT" in vt or "CN" == vt or "CDNR" in sc or "CREDIT NOTE" in sc or taxable < 0:
        return "CREDIT_NOTE"
    if "DEBIT" in vt or "DN" == vt or "DEBIT NOTE" in sc:
        return "DEBIT_NOTE"
    if "NON-GST" in sc or "NONGST" in sc or "NON GST" in sc:
        return "NON_GST"
    if "EXEMPT" in sc:
        return "EXEMPT"
    if "NIL" in sc:
        return "NIL"
    if "SEZ" in sc:
        return "SEZ_WP" if total_tax > 0 or "WP" in sc or "WITH TAX" in sc or "WITH PAY" in sc else "SEZ_WOP"
    if "EXPORT" in sc or "EXP" == sc:
        return "EXPORT_WP" if total_tax > 0 or "WP" in sc or "WITH TAX" in sc or "WITH PAY" in sc else "EXPORT_WOP"
    if "DEEMED" in sc:
        return "DEEMED_EXP"

    if total_tax == 0 and taxable > 0 and ("0%" in sc or "EXEMPT" in vt):
        return "EXEMPT"
    if gstin and len(gstin) == 15:
        return "B2B"
    return "B2C"


def _read_excel_or_csv_bytes(file_bytes: bytes, filename: str) -> Dict[str, pd.DataFrame]:
    """Read all sheets from xlsx/xls/csv/zip into {sheet_name: DataFrame}."""
    fname = (filename or "").lower()
    sheets: Dict[str, pd.DataFrame] = {}

    if fname.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            for member in zf.namelist():
                if member.lower().endswith((".xlsx", ".xls", ".csv", ".json")) and not member.startswith("__MACOSX"):
                    sub = _read_excel_or_csv_bytes(zf.read(member), member)
                    sheets.update(sub)
        return sheets

    if fname.endswith(".csv"):
        df = pd.read_csv(io.BytesIO(file_bytes))
        return {"CSV": df}

    if fname.endswith(".json"):
        raw = json.loads(file_bytes.decode("utf-8", errors="ignore"))
        return {"__JSON__": pd.DataFrame([{"__payload__": raw}])}

    engine = "xlrd" if fname.endswith(".xls") else "openpyxl"
    with pd.ExcelFile(io.BytesIO(file_bytes), engine=engine) as xls:
        for sname in xls.sheet_names:
            df_raw = pd.read_excel(xls, sheet_name=sname, header=None)
            if df_raw.empty:
                continue
            # Auto-detect real header row within first 12 rows
            header_idx = 0
            best_score = -1
            keywords = {
                "gstin", "gst", "invoice", "party", "taxable", "integrated", "igst",
                "central", "cgst", "state", "sgst", "hsn", "rate", "supply", "particular",
                "voucher", "3.1", "4(", "nature", "description", "uqc", "turnover",
            }
            for r_idx in range(min(12, len(df_raw))):
                row_vals = [str(v).lower().strip() for v in df_raw.iloc[r_idx].tolist() if pd.notna(v)]
                score = sum(1 for v in row_vals if any(k in v for k in keywords))
                if score > best_score and len(row_vals) >= 2:
                    best_score = score
                    header_idx = r_idx

            headers = [str(v).strip() if pd.notna(v) else f"Col_{i}" for i, v in enumerate(df_raw.iloc[header_idx].tolist())]
            # Ensure unique column names
            seen: Dict[str, int] = {}
            uniq_headers = []
            for h in headers:
                if h in seen:
                    seen[h] += 1
                    uniq_headers.append(f"{h}_{seen[h]}")
                else:
                    seen[h] = 0
                    uniq_headers.append(h)

            df_clean = df_raw.iloc[header_idx + 1 :].copy()
            df_clean.columns = uniq_headers
            df_clean = df_clean.dropna(how="all").reset_index(drop=True)
            sheets[sname] = df_clean

    return sheets


def parse_sales_register_df(df: pd.DataFrame, fy: str = "2025-26", cutoff_month: int = 11) -> pd.DataFrame:
    """Normalize an outward Sales Register (Books/Tally) DataFrame into canonical GSTR-9 outward rows."""
    if df is None or df.empty:
        return pd.DataFrame()

    cols = list(df.columns)
    c_party = _find_col(cols, ["party name", "party's name", "customer name", "receiver name", "trade/legal name", "particulars", "buyer"])
    c_gstin = _find_col(cols, ["gstin", "gst no", "gst number", "gstno", "uin"])
    c_inv = _find_col(cols, ["invoice no", "invoice number", "inv no", "bill no", "document no", "voucher no"])
    c_date = _find_col(cols, ["invoice date", "inv date", "date", "voucher date", "doc date"])
    c_rep_period = _find_col(cols, ["return period", "filing period", "reported month", "period date"])
    c_vch = _find_col(cols, ["voucher type", "doc type", "document type", "type"])
    c_cat = _find_col(cols, ["supply category", "supply type", "category", "gstr9 table", "nature of supply", "taxability"])
    c_hsn = _find_col(cols, ["hsn/sac", "hsn code", "hsn", "sac"])
    c_uqc = _find_col(cols, ["uqc", "unit", "uom"])
    c_qty = _find_col(cols, ["quantity", "qty", "total quantity"])
    c_rate = _find_col(cols, ["rate (%)", "rate%", "tax rate", "gst rate", "rate"])
    c_taxable = _find_col(cols, ["taxable value", "taxable amt", "taxable", "assessable value", "net amount"])
    c_igst = _find_col(cols, ["integrated tax", "igst", "igst amount"])
    c_cgst = _find_col(cols, ["central tax", "cgst", "cgst amount"])
    c_sgst = _find_col(cols, ["state/ut tax", "state tax", "sgst/utgst", "sgst", "sgst amount"])
    c_cess = _find_col(cols, ["cess", "cess amount"])
    c_rcm = _find_col(cols, ["reverse charge", "rcm", "supply attract reverse charge"])

    records: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        party = str(row.get(c_party, "") or "").strip() if c_party else ""
        if re.match(r"^(?:grand\s+|sub\s*[-\s]*)?total(?:\s*[:\-].*)?$", party.lower()):
            continue

        gstin = clean_gstin(row.get(c_gstin, "")) if c_gstin else ""
        inv = str(row.get(c_inv, "") or "").strip() if c_inv else ""
        raw_dt = row.get(c_date, "") if c_date else ""
        rep_period = row.get(c_rep_period, "") if c_rep_period else ""
        vch_type = str(row.get(c_vch, "") or "").strip() if c_vch else ""
        sup_cat = str(row.get(c_cat, "") or "").strip() if c_cat else ""

        taxable = _to_float(row.get(c_taxable, 0.0)) if c_taxable else 0.0
        igst = _to_float(row.get(c_igst, 0.0)) if c_igst else 0.0
        cgst = _to_float(row.get(c_cgst, 0.0)) if c_cgst else 0.0
        sgst = _to_float(row.get(c_sgst, 0.0)) if c_sgst else 0.0
        cess = _to_float(row.get(c_cess, 0.0)) if c_cess else 0.0

        if taxable == 0 and igst == 0 and cgst == 0 and sgst == 0 and not inv:
            continue

        rcm_flag = parse_rcm_flag(row.get(c_rcm, "")) if c_rcm else False
        total_tax = round(igst + cgst + sgst + cess, 2)
        cat = _detect_outward_category(vch_type, sup_cat, gstin, taxable, total_tax, rcm_flag)

        # Keep signed magnitudes positive for CN/DN when computing table totals cleanly
        bucket = classify_period_bucket(raw_dt, rep_period, fy, cutoff_month)
        dt_obj = parse_invoice_date(raw_dt)
        hsn_code = _clean_hsn(row.get(c_hsn, "")) if c_hsn else ""
        uqc = str(row.get(c_uqc, "") or "").strip().upper() if c_uqc else ""
        qty = _to_float(row.get(c_qty, 0.0)) if c_qty else 0.0
        rate = _infer_rate_pct(abs(taxable), abs(igst + cgst + sgst), row.get(c_rate) if c_rate else 0.0)

        records.append({
            "party_name": party,
            "gstin": gstin,
            "inv_no": inv,
            "clean_inv": clean_invoice_number(inv),
            "doc_date": dt_obj.isoformat() if dt_obj else str(raw_dt),
            "period_bucket": bucket,
            "category": cat,
            "hsn": hsn_code,
            "uqc": uqc or "NOS",
            "qty": qty,
            "rate": rate,
            "taxable": round(taxable, 2),
            "igst": round(igst, 2),
            "cgst": round(cgst, 2),
            "sgst": round(sgst, 2),
            "cess": round(cess, 2),
            "is_b2b": bool(gstin and len(gstin) == 15),
        })

    return pd.DataFrame(records)


def _sheet_category_hint(sheet_name: str) -> str:
    s = sheet_name.lower().strip()
    if "cdnur" in s or "cdnr" in s or "credit" in s:
        return "CREDIT_NOTE"
    if "b2cl" in s or "b2cs" in s or "b2c" in s:
        return "B2C"
    if "b2b" in s:
        return "B2B"
    if "exp" in s:
        return "EXPORT"
    if "sez" in s:
        return "SEZ"
    if "exemp" in s or "nil" in s:
        return "EXEMPT"
    return ""


def parse_gstr1_files(
    file_tuples: List[Tuple[bytes, str]],
    fy: str = "2025-26",
    cutoff_month: int = 11,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Parse one or more GSTR-1 files (.xlsx, .csv, .zip, or GSTN .json).
    Returns (outward_invoices_df, explicit_hsn_df).
    """
    outward_frames: List[pd.DataFrame] = []
    hsn_records: List[Dict[str, Any]] = []

    for fbytes, fname in file_tuples:
        if not fbytes:
            continue
        sheets = _read_excel_or_csv_bytes(fbytes, fname)
        if "__JSON__" in sheets:
            payload = sheets["__JSON__"].iloc[0]["__payload__"]
            inv_df, hsn_df = _parse_gstr1_json(payload, fy, cutoff_month)
            if not inv_df.empty:
                outward_frames.append(inv_df)
            if not hsn_df.empty:
                hsn_records.extend(hsn_df.to_dict("records"))
            continue

        for sname, sdf in sheets.items():
            if sdf.empty:
                continue
            s_low = sname.lower()
            if "read me" in s_low or "help" in s_low or "summary" == s_low:
                continue

            if "hsn" in s_low:
                cols = list(sdf.columns)
                c_hsn = _find_col(cols, ["hsn", "hsn/sac", "hsn code"])
                c_uqc = _find_col(cols, ["uqc", "unit"])
                c_qty = _find_col(cols, ["total quantity", "qty", "quantity"])
                c_rate = _find_col(cols, ["rate", "rate (%)"])
                c_taxable = _find_col(cols, ["taxable value", "taxable"])
                c_igst = _find_col(cols, ["integrated tax", "igst"])
                c_cgst = _find_col(cols, ["central tax", "cgst"])
                c_sgst = _find_col(cols, ["state/ut tax", "sgst", "state tax"])
                c_cess = _find_col(cols, ["cess"])
                for _, r in sdf.iterrows():
                    code = _clean_hsn(r.get(c_hsn, "")) if c_hsn else ""
                    if not code or "total" in code.lower():
                        continue
                    taxable = _to_float(r.get(c_taxable, 0.0)) if c_taxable else 0.0
                    igst = _to_float(r.get(c_igst, 0.0)) if c_igst else 0.0
                    cgst = _to_float(r.get(c_cgst, 0.0)) if c_cgst else 0.0
                    sgst = _to_float(r.get(c_sgst, 0.0)) if c_sgst else 0.0
                    cess = _to_float(r.get(c_cess, 0.0)) if c_cess else 0.0
                    rate = _infer_rate_pct(taxable, igst + cgst + sgst, r.get(c_rate) if c_rate else 0.0)
                    hsn_records.append({
                        "hsn": code,
                        "uqc": str(r.get(c_uqc, "") or "").strip().upper() if c_uqc else "",
                        "qty": _to_float(r.get(c_qty, 0.0)) if c_qty else 0.0,
                        "rate": rate,
                        "taxable": round(taxable, 2),
                        "igst": round(igst, 2),
                        "cgst": round(cgst, 2),
                        "sgst": round(sgst, 2),
                        "cess": round(cess, 2),
                        "is_b2b": True,
                    })
                continue

            hint = _sheet_category_hint(sname)
            df_copy = sdf.copy()
            if hint and not _find_col(list(df_copy.columns), ["supply category", "supply type", "category"]):
                df_copy["Supply Category"] = hint
            parsed = parse_sales_register_df(df_copy, fy=fy, cutoff_month=cutoff_month)
            if not parsed.empty:
                outward_frames.append(parsed)

    out_df = pd.concat(outward_frames, ignore_index=True) if outward_frames else pd.DataFrame()
    hsn_df = pd.DataFrame(hsn_records) if hsn_records else pd.DataFrame()
    return out_df, hsn_df


def _parse_gstr1_json(payload: Dict[str, Any], fy: str, cutoff_month: int) -> Tuple[pd.DataFrame, pd.DataFrame]:
    rows: List[Dict[str, Any]] = []
    hsn_rows: List[Dict[str, Any]] = []
    fp = payload.get("fp", "")

    for supplier_block in payload.get("b2b", []) or []:
        ctin = clean_gstin(supplier_block.get("ctin", ""))
        for inv in supplier_block.get("inv", []) or []:
            inum = inv.get("inum", "")
            idt = inv.get("idt", "")
            for itm in inv.get("itms", []) or []:
                det = itm.get("itm_det", {})
                txval = _to_float(det.get("txval"))
                iamt = _to_float(det.get("iamt"))
                camt = _to_float(det.get("camt"))
                samt = _to_float(det.get("samt"))
                csamt = _to_float(det.get("csamt"))
                rt = _to_float(det.get("rt"))
                rows.append({
                    "party_name": supplier_block.get("cname", ctin),
                    "gstin": ctin,
                    "inv_no": inum,
                    "clean_inv": clean_invoice_number(inum),
                    "doc_date": (parse_invoice_date(idt).isoformat() if parse_invoice_date(idt) else idt),
                    "period_bucket": classify_period_bucket(idt, fp, fy, cutoff_month),
                    "category": "B2B",
                    "hsn": "",
                    "uqc": "NOS",
                    "qty": 0.0,
                    "rate": rt,
                    "taxable": round(txval, 2),
                    "igst": round(iamt, 2),
                    "cgst": round(camt, 2),
                    "sgst": round(samt, 2),
                    "cess": round(csamt, 2),
                    "is_b2b": True,
                })

    for b2cs_itm in payload.get("b2cs", []) or []:
        txval = _to_float(b2cs_itm.get("txval"))
        iamt = _to_float(b2cs_itm.get("iamt"))
        camt = _to_float(b2cs_itm.get("camt"))
        samt = _to_float(b2cs_itm.get("samt"))
        csamt = _to_float(b2cs_itm.get("csamt"))
        rt = _to_float(b2cs_itm.get("rt"))
        rows.append({
            "party_name": f"B2CS POS-{b2cs_itm.get('pos', '')}",
            "gstin": "",
            "inv_no": "B2CS",
            "clean_inv": "B2CS",
            "doc_date": "",
            "period_bucket": "IN_FY",
            "category": "B2C",
            "hsn": "",
            "uqc": "NOS",
            "qty": 0.0,
            "rate": rt,
            "taxable": round(txval, 2),
            "igst": round(iamt, 2),
            "cgst": round(camt, 2),
            "sgst": round(samt, 2),
            "cess": round(csamt, 2),
            "is_b2b": False,
        })

    for hsn_item in (payload.get("hsn", {}) or {}).get("data", []) or []:
        hsn_rows.append({
            "hsn": _clean_hsn(hsn_item.get("hsn_sc", "")),
            "uqc": str(hsn_item.get("uqc", "") or "").upper(),
            "qty": _to_float(hsn_item.get("qty")),
            "rate": _to_float(hsn_item.get("rt")),
            "taxable": round(_to_float(hsn_item.get("txval")), 2),
            "igst": round(_to_float(hsn_item.get("iamt")), 2),
            "cgst": round(_to_float(hsn_item.get("camt")), 2),
            "sgst": round(_to_float(hsn_item.get("samt")), 2),
            "cess": round(_to_float(hsn_item.get("csamt")), 2),
            "is_b2b": True,
        })

    return pd.DataFrame(rows), pd.DataFrame(hsn_rows)


def empty_gstr3b_structure() -> Dict[str, Any]:
    """Return canonical zeroed GSTR-3B summary structure."""
    zero_tax = lambda: {"taxable": 0.0, "igst": 0.0, "cgst": 0.0, "sgst": 0.0, "cess": 0.0}
    return {
        "outward_3_1": {
            "a_taxable": zero_tax(),      # 3.1(a) Outward taxable (other than zero/nil/exempt)
            "b_zero_rated": zero_tax(),   # 3.1(b) Zero rated / SEZ / Export
            "c_nil_exempt": zero_tax(),   # 3.1(c) Nil / Exempt
            "d_inward_rcm": zero_tax(),   # 3.1(d) Inward RCM liability
            "e_non_gst": zero_tax(),      # 3.1(e) Non-GST outward
        },
        "itc_4": {
            "4A_1_import_goods": {"igst": 0.0, "cgst": 0.0, "sgst": 0.0, "cess": 0.0},
            "4A_2_import_services": {"igst": 0.0, "cgst": 0.0, "sgst": 0.0, "cess": 0.0},
            "4A_3_rcm": {"igst": 0.0, "cgst": 0.0, "sgst": 0.0, "cess": 0.0},
            "4A_4_isd": {"igst": 0.0, "cgst": 0.0, "sgst": 0.0, "cess": 0.0},
            "4A_5_all_other": {"igst": 0.0, "cgst": 0.0, "sgst": 0.0, "cess": 0.0},
            "4B_1_rule_42_43": {"igst": 0.0, "cgst": 0.0, "sgst": 0.0, "cess": 0.0},
            "4B_2_other_reversals": {"igst": 0.0, "cgst": 0.0, "sgst": 0.0, "cess": 0.0},
            "4D_1_sec_17_5": {"igst": 0.0, "cgst": 0.0, "sgst": 0.0, "cess": 0.0},
            "4D_2_other_ineligible": {"igst": 0.0, "cgst": 0.0, "sgst": 0.0, "cess": 0.0},
        },
        "tax_paid_6_1": {
            "itc_utilized": {"igst": 0.0, "cgst": 0.0, "sgst": 0.0, "cess": 0.0},
            "cash_paid": {"igst": 0.0, "cgst": 0.0, "sgst": 0.0, "cess": 0.0},
            "interest_paid": 0.0,
            "late_fee_paid": 0.0,
        },
    }


def _add_tax_dict(target: Dict[str, float], src: Dict[str, Any]) -> None:
    for k in list(target.keys()):
        target[k] = round(target[k] + _to_float(src.get(k, 0.0)), 2)


_NUM_OR_DASH_RE = re.compile(r"(?<![A-Za-z0-9.])(?:\d{1,3}(?:,\d{2,3})*(?:\.\d+)?|\d+\.\d+|-(?!\d))(?![A-Za-z0-9.])")


def _extract_row_numbers(text: str) -> List[float]:
    """Extract numeric amounts (treating standalone '-' as 0.0) in reading order."""
    vals: List[float] = []
    for tok in _NUM_OR_DASH_RE.findall(text or ""):
        if tok == "-":
            vals.append(0.0)
        else:
            vals.append(_to_float(tok))
    return vals


def parse_gstr3b_pdf_bytes(pdf_bytes: bytes) -> Dict[str, Any]:
    """
    Parse official GST Portal Form GSTR-3B PDF text into canonical GSTR-3B summary dict.
    Extracts Table 3.1(a)-(e), Table 4(A)-(D), and Table 6.1 tax/cash/ITC/interest/late fee.
    """
    out = empty_gstr3b_structure()
    if not pdf_bytes:
        return out

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    full_text = "\n".join((page.extract_text() or "") for page in reader.pages)
    if not full_text.strip():
        return out

    lines = [re.sub(r"\s+", " ", ln).strip() for ln in full_text.splitlines() if ln and ln.strip()]

    # Join wrapped label lines with their trailing number line if a line has no numbers
    merged_lines: List[str] = []
    for ln in lines:
        if (
            merged_lines
            and not _NUM_OR_DASH_RE.search(merged_lines[-1])
            and re.match(r"^(?:\([a-eA-D1-5]+\)|Integrated\s+Tax|Central\s+Tax|State/UT\s+Tax|Cess)", merged_lines[-1], re.I)
            and _NUM_OR_DASH_RE.search(ln)
            and not re.match(r"^(?:3\.1|4\.|5\.|6\.1|\([a-eA-D1-5]+\))", ln, re.I)
        ):
            merged_lines[-1] = f"{merged_lines[-1]} {ln}"
        else:
            merged_lines.append(ln)

    section = ""
    sub4 = ""
    for ln in merged_lines:
        low = ln.lower()
        if "3.1" in low and ("outward" in low or "details of outward" in low):
            section = "3.1"
            continue
        if ("4." in low or "4 " in low) and "eligible itc" in low:
            section = "4"
            sub4 = ""
            continue
        if ("5." in low or "5 " in low) and "exempt" in low and "nil" in low and "non-gst" in low:
            section = "5"
            continue
        if "6.1" in low and "payment of tax" in low:
            section = "6.1"
            continue

        if section == "3.1":
            nums = _extract_row_numbers(ln)
            # Filter out leading list marker if parsed as number
            if re.match(r"^\(?a\)?\b", low) or "other than zero rated" in low or ("outward taxable" in low and "zero" not in low):
                vals = nums[-5:] if len(nums) >= 5 else nums + [0.0] * (5 - len(nums))
                _add_tax_dict(out["outward_3_1"]["a_taxable"], dict(zip(["taxable", "igst", "cgst", "sgst", "cess"], vals)))
            elif re.match(r"^\(?b\)?\b", low) or "zero rated" in low:
                vals = nums[-5:] if len(nums) >= 5 else nums + [0.0] * (5 - len(nums))
                _add_tax_dict(out["outward_3_1"]["b_zero_rated"], dict(zip(["taxable", "igst", "cgst", "sgst", "cess"], vals)))
            elif re.match(r"^\(?c\)?\b", low) or ("nil rated" in low and "exempted" in low):
                vals = nums[-5:] if len(nums) >= 5 else nums + [0.0] * (5 - len(nums))
                _add_tax_dict(out["outward_3_1"]["c_nil_exempt"], dict(zip(["taxable", "igst", "cgst", "sgst", "cess"], vals)))
            elif re.match(r"^\(?d\)?\b", low) or "reverse charge" in low:
                vals = nums[-5:] if len(nums) >= 5 else nums + [0.0] * (5 - len(nums))
                _add_tax_dict(out["outward_3_1"]["d_inward_rcm"], dict(zip(["taxable", "igst", "cgst", "sgst", "cess"], vals)))
            elif re.match(r"^\(?e\)?\b", low) or "non-gst" in low:
                vals = nums[-5:] if len(nums) >= 5 else nums + [0.0] * (5 - len(nums))
                _add_tax_dict(out["outward_3_1"]["e_non_gst"], dict(zip(["taxable", "igst", "cgst", "sgst", "cess"], vals)))

        elif section == "4":
            if "(a)" in low and "itc available" in low:
                sub4 = "A"
            elif "(b)" in low and "itc reversed" in low:
                sub4 = "B"
            elif "(d)" in low and "ineligible" in low:
                sub4 = "D"

            nums = _extract_row_numbers(ln)
            # Strip leading bullet digit 1..5 if captured before amounts
            if len(nums) >= 5 and nums[0] in (1.0, 2.0, 3.0, 4.0, 5.0):
                nums = nums[1:]
            vals4 = nums[-4:] if len(nums) >= 4 else nums + [0.0] * (4 - len(nums))
            d4 = dict(zip(["igst", "cgst", "sgst", "cess"], vals4))

            if "import of goods" in low:
                _add_tax_dict(out["itc_4"]["4A_1_import_goods"], d4)
            elif "import of services" in low:
                _add_tax_dict(out["itc_4"]["4A_2_import_services"], d4)
            elif "reverse charge" in low and ("sub-section" in low or "(3)" in low or sub4 == "A"):
                _add_tax_dict(out["itc_4"]["4A_3_rcm"], d4)
            elif "from isd" in low or "(4)" in low and sub4 == "A":
                _add_tax_dict(out["itc_4"]["4A_4_isd"], d4)
            elif "all other itc" in low or ("(5)" in low and sub4 == "A"):
                _add_tax_dict(out["itc_4"]["4A_5_all_other"], d4)
            elif "rules 38" in low or "rule 42" in low or "rule 43" in low or ("(1)" in low and sub4 == "B"):
                _add_tax_dict(out["itc_4"]["4B_1_rule_42_43"], d4)
            elif sub4 == "B" and ("others" in low or "(2)" in low) and len(nums) >= 4:
                _add_tax_dict(out["itc_4"]["4B_2_other_reversals"], d4)
            elif "17(5)" in low or ("(1)" in low and sub4 == "D"):
                _add_tax_dict(out["itc_4"]["4D_1_sec_17_5"], d4)
            elif sub4 == "D" and ("others" in low or "(2)" in low) and len(nums) >= 4:
                _add_tax_dict(out["itc_4"]["4D_2_other_ineligible"], d4)

        elif section == "6.1":
            head = None
            if re.search(r"\bintegrated\s+tax\b", low):
                head = "igst"
            elif re.search(r"\bcentral\s+tax\b", low):
                head = "cgst"
            elif re.search(r"\bstate/ut\s+tax\b|\bstate\s+tax\b", low):
                head = "sgst"
            elif re.search(r"\bcess\b", low) and "tax payable" not in low:
                head = "cess"

            if head:
                nums = _extract_row_numbers(ln)
                # Standard GSTR-3B 6.1 layout after head label:
                # [Tax payable, ITC_IGST, ITC_CGST, ITC_SGST, ITC_Cess, Cash_Paid, Interest, Late_Fee] (8 cols)
                # or older 9-col with TDS/TCS. Using tail indexing guarantees Cash/Interest/LateFee alignment.
                if len(nums) >= 8:
                    itc_used = sum(nums[1:5])
                    cash_paid = nums[-3]
                    intr_paid = nums[-2]
                    late_paid = nums[-1]
                    out["tax_paid_6_1"]["itc_utilized"][head] = round(out["tax_paid_6_1"]["itc_utilized"][head] + itc_used, 2)
                    out["tax_paid_6_1"]["cash_paid"][head] = round(out["tax_paid_6_1"]["cash_paid"][head] + cash_paid, 2)
                    out["tax_paid_6_1"]["interest_paid"] = round(out["tax_paid_6_1"]["interest_paid"] + intr_paid, 2)
                    out["tax_paid_6_1"]["late_fee_paid"] = round(out["tax_paid_6_1"]["late_fee_paid"] + late_paid, 2)
                elif len(nums) >= 4:
                    # Simplified 6.1 summary: [Payable, ITC, Cash, Int+LF]
                    out["tax_paid_6_1"]["itc_utilized"][head] = round(out["tax_paid_6_1"]["itc_utilized"][head] + nums[1], 2)
                    out["tax_paid_6_1"]["cash_paid"][head] = round(out["tax_paid_6_1"]["cash_paid"][head] + nums[2], 2)
                    out["tax_paid_6_1"]["interest_paid"] = round(out["tax_paid_6_1"]["interest_paid"] + nums[3], 2)

    return out


def classify_gstr9_file_bytes(file_bytes: bytes, filename: str) -> str:
    """
    Auto-classify an arbitrary GST/ERP file into one of:
      'gstr1', 'gstr2b', 'gstr3b', 'sales', 'purchase', 'unknown'
    Inspects extension, JSON structure, Excel sheet names, and column headers.
    """
    fname = (filename or "").lower()

    # 1. PDFs containing 3B / GSTR-3B markers -> gstr3b
    if fname.endswith(".pdf"):
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(file_bytes))
            sample_txt = " ".join((p.extract_text() or "") for p in reader.pages[:2]).lower()
            if "gstr-3b" in sample_txt or "gstr 3b" in sample_txt or ("3.1" in sample_txt and "eligible itc" in sample_txt):
                return "gstr3b"
        except Exception:
            pass
        if "3b" in fname:
            return "gstr3b"
        return "unknown"

    # 2. Check structured JSON or ZIP-of-JSON
    try:
        sheets = _read_excel_or_csv_bytes(file_bytes, filename)
    except Exception:
        sheets = {}

    if "__JSON__" in sheets:
        raw = sheets["__JSON__"].iloc[0]["__payload__"]
        if isinstance(raw, dict):
            if "sup_details" in raw or "itc_elg" in raw or "outward_3_1" in raw:
                return "gstr3b"
            if "data" in raw and isinstance(raw["data"], dict) and "docdata" in raw["data"]:
                return "gstr2b"
            if "b2cs" in raw or "hsn" in raw or "cdnr" in raw or "exp" in raw or "b2cl" in raw:
                return "gstr1"
            if "b2b" in raw:
                if "gstr1" in fname or "r1" in fname:
                    return "gstr1"
                return "gstr2b"

    # 3. Inspect Excel sheets & headers
    sheet_names_low = " ".join(s.lower() for s in sheets.keys())
    if "b2cs" in sheet_names_low or "b2cl" in sheet_names_low or "exemp" in sheet_names_low:
        return "gstr1"

    all_cols_low = " ".join(
        str(c).lower()
        for sdf in sheets.values()
        if isinstance(sdf, pd.DataFrame) and not sdf.empty
        for c in sdf.columns
    )

    if ("3.1(a)" in all_cols_low or "nature of supplies" in all_cols_low) and ("gstr3b" in fname or "3b" in fname):
        return "gstr3b"
    if "itc availability" in all_cols_low or "gstr-5 filing" in all_cols_low or "source" in all_cols_low and "irn" in all_cols_low:
        return "gstr2b"
    if "receiver name" in all_cols_low or "gstin/uin of recipient" in all_cols_low or "recipient" in all_cols_low:
        if "gstr1" in fname or "r1" in fname or "b2b" in sheet_names_low or "sales" not in fname:
            return "gstr1"
    if "gstin of supplier" in all_cols_low or "supplier gstin" in all_cols_low:
        if "2b" in fname or "gstr2b" in fname or "r2b" in fname or "b2b" in sheet_names_low:
            return "gstr2b"
        if "purchase" in fname or "voucher" in all_cols_low or "unpaid days" in all_cols_low or "itc category" in all_cols_low:
            return "purchase"

    # 4. Filename keyword tie-breakers
    if any(k in fname for k in ("gstr3b", "gstr-3b", "gstr_3b", "_3b_", "r3b")):
        return "gstr3b"
    if any(k in fname for k in ("gstr1", "gstr-1", "gstr_1", "_r1_", "_r1.", "r1_")):
        return "gstr1"
    if any(k in fname for k in ("gstr2b", "gstr-2b", "gstr_2b", "_2b", "2b_", "r2b")):
        return "gstr2b"
    if any(k in fname for k in ("sales", "outward", "revenue")):
        return "sales"
    if any(k in fname for k in ("purchase", "inward", "register", "books", "tally")):
        return "purchase"

    if "b2b" in sheet_names_low:
        return "gstr2b"
    if not sheets or all(sdf.empty for sdf in sheets.values() if isinstance(sdf, pd.DataFrame)):
        return "unknown"
    return "purchase" if "supplier" in all_cols_low else ("sales" if "customer" in all_cols_low or "buyer" in all_cols_low else "unknown")


def _merge_gstr3b_dicts(agg: Dict[str, Any], raw: Dict[str, Any]) -> None:
    """Merge a canonical GSTR-3B dictionary into the running aggregator."""
    for sec in ("outward_3_1", "itc_4"):
        for k, v in (raw.get(sec) or {}).items():
            if k in agg[sec] and isinstance(v, dict):
                _add_tax_dict(agg[sec][k], v)
    tp = raw.get("tax_paid_6_1") or {}
    if isinstance(tp.get("itc_utilized"), dict):
        _add_tax_dict(agg["tax_paid_6_1"]["itc_utilized"], tp["itc_utilized"])
    if isinstance(tp.get("cash_paid"), dict):
        _add_tax_dict(agg["tax_paid_6_1"]["cash_paid"], tp["cash_paid"])
    agg["tax_paid_6_1"]["interest_paid"] = round(
        agg["tax_paid_6_1"]["interest_paid"] + _to_float(tp.get("interest_paid", 0)), 2
    )
    agg["tax_paid_6_1"]["late_fee_paid"] = round(
        agg["tax_paid_6_1"]["late_fee_paid"] + _to_float(tp.get("late_fee_paid", 0)), 2
    )


def parse_gstr3b_inputs(
    file_tuples: List[Tuple[bytes, str]],
    manual_overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Aggregate one or more GSTR-3B Excel/CSV/JSON/PDF (or ZIP of them) files and merge explicit UI manual overrides.
    """
    agg = empty_gstr3b_structure()

    for fbytes, fname in file_tuples:
        if not fbytes:
            continue
        low_fn = (fname or "").lower()
        if low_fn.endswith(".pdf"):
            _merge_gstr3b_dicts(agg, parse_gstr3b_pdf_bytes(fbytes))
            continue
        if low_fn.endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(fbytes)) as zf:
                    pdf_members = [m for m in zf.namelist() if m.lower().endswith(".pdf") and not m.startswith("__MACOSX")]
                    for pm in pdf_members:
                        _merge_gstr3b_dicts(agg, parse_gstr3b_pdf_bytes(zf.read(pm)))
                    non_pdf_members = [m for m in zf.namelist() if m.lower().endswith((".xlsx", ".xls", ".csv", ".json")) and not m.startswith("__MACOSX")]
                    if pdf_members and not non_pdf_members:
                        continue
            except Exception:
                pass
        sheets = _read_excel_or_csv_bytes(fbytes, fname)
        if "__JSON__" in sheets:
            raw = sheets["__JSON__"].iloc[0]["__payload__"]
            # Handle either official GSTN 3B JSON or TallyWalls structured JSON
            if "sup_details" in raw:
                sd = raw["sup_details"]
                for j_key, t_key in [
                    ("osup_det", "a_taxable"),
                    ("osup_zero", "b_zero_rated"),
                    ("osup_nil_exmp", "c_nil_exempt"),
                    ("isup_rev", "d_inward_rcm"),
                    ("osup_nongst", "e_non_gst"),
                ]:
                    blk = sd.get(j_key) or {}
                    _add_tax_dict(agg["outward_3_1"][t_key], {
                        "taxable": blk.get("txval", 0),
                        "igst": blk.get("iamt", 0),
                        "cgst": blk.get("camt", 0),
                        "sgst": blk.get("samt", 0),
                        "cess": blk.get("csamt", 0),
                    })
                itc_elg = raw.get("itc_elg") or {}
                for item in itc_elg.get("itc_avl", []) or []:
                    ty = str(item.get("ty", "")).upper()
                    t_map = {
                        "IMPG": "4A_1_import_goods",
                        "IMPS": "4A_2_import_services",
                        "ISRC": "4A_3_rcm",
                        "ISD": "4A_4_isd",
                        "OTH": "4A_5_all_other",
                    }
                    if ty in t_map:
                        _add_tax_dict(agg["itc_4"][t_map[ty]], {
                            "igst": item.get("iamt", 0),
                            "cgst": item.get("camt", 0),
                            "sgst": item.get("samt", 0),
                            "cess": item.get("csamt", 0),
                        })
                for item in itc_elg.get("itc_rev", []) or []:
                    ty = str(item.get("ty", "")).upper()
                    t_key = "4B_1_rule_42_43" if "RUL" in ty else "4B_2_other_reversals"
                    _add_tax_dict(agg["itc_4"][t_key], {
                        "igst": item.get("iamt", 0),
                        "cgst": item.get("camt", 0),
                        "sgst": item.get("samt", 0),
                        "cess": item.get("csamt", 0),
                    })
                for item in itc_elg.get("itc_inelg", []) or []:
                    ty = str(item.get("ty", "")).upper()
                    t_key = "4D_1_sec_17_5" if "RUL" in ty or "17" in ty else "4D_2_other_ineligible"
                    _add_tax_dict(agg["itc_4"][t_key], {
                        "igst": item.get("iamt", 0),
                        "cgst": item.get("camt", 0),
                        "sgst": item.get("samt", 0),
                        "cess": item.get("csamt", 0),
                    })
                intr = raw.get("intr_ltfee") or {}
                intr_det = intr.get("intr_details") or {}
                lt_det = intr.get("ltfee_details") or {}
                agg["tax_paid_6_1"]["interest_paid"] = round(
                    agg["tax_paid_6_1"]["interest_paid"]
                    + sum(_to_float(intr_det.get(k, 0)) for k in ("iamt", "camt", "samt", "csamt")),
                    2,
                )
                agg["tax_paid_6_1"]["late_fee_paid"] = round(
                    agg["tax_paid_6_1"]["late_fee_paid"]
                    + sum(_to_float(lt_det.get(k, 0)) for k in ("iamt", "camt", "samt", "csamt")),
                    2,
                )
            elif "outward_3_1" in raw or "itc_4" in raw:
                for sec in ("outward_3_1", "itc_4"):
                    for k, v in (raw.get(sec) or {}).items():
                        if k in agg[sec] and isinstance(v, dict):
                            _add_tax_dict(agg[sec][k], v)
                tp = raw.get("tax_paid_6_1") or {}
                if isinstance(tp.get("itc_utilized"), dict):
                    _add_tax_dict(agg["tax_paid_6_1"]["itc_utilized"], tp["itc_utilized"])
                if isinstance(tp.get("cash_paid"), dict):
                    _add_tax_dict(agg["tax_paid_6_1"]["cash_paid"], tp["cash_paid"])
                agg["tax_paid_6_1"]["interest_paid"] = round(
                    agg["tax_paid_6_1"]["interest_paid"] + _to_float(tp.get("interest_paid", 0)), 2
                )
                agg["tax_paid_6_1"]["late_fee_paid"] = round(
                    agg["tax_paid_6_1"]["late_fee_paid"] + _to_float(tp.get("late_fee_paid", 0)), 2
                )
            continue

        # Tabular Excel/CSV GSTR-3B sheet
        for _, sdf in sheets.items():
            if sdf.empty:
                continue
            cols = list(sdf.columns)
            c_desc = _find_col(cols, ["nature of supplies", "description", "section", "table", "particulars", "details"])
            c_tx = _find_col(cols, ["taxable", "total taxable value"])
            c_igst = _find_col(cols, ["integrated tax", "igst"])
            c_cgst = _find_col(cols, ["central tax", "cgst"])
            c_sgst = _find_col(cols, ["state/ut tax", "state tax", "sgst"])
            c_cess = _find_col(cols, ["cess"])
            c_cash = _find_col(cols, ["paid in cash", "cash paid", "tax paid in cash"])
            c_itc_paid = _find_col(cols, ["paid through itc", "itc utilized"])

            if not c_desc:
                continue

            for _, row in sdf.iterrows():
                desc = str(row.get(c_desc, "") or "").lower().strip()
                if not desc:
                    continue
                vals = {
                    "taxable": _to_float(row.get(c_tx, 0.0)) if c_tx else 0.0,
                    "igst": _to_float(row.get(c_igst, 0.0)) if c_igst else 0.0,
                    "cgst": _to_float(row.get(c_cgst, 0.0)) if c_cgst else 0.0,
                    "sgst": _to_float(row.get(c_sgst, 0.0)) if c_sgst else 0.0,
                    "cess": _to_float(row.get(c_cess, 0.0)) if c_cess else 0.0,
                }
                if "3.1(a)" in desc or "outward taxable supplies" in desc and "zero" not in desc:
                    _add_tax_dict(agg["outward_3_1"]["a_taxable"], vals)
                elif "3.1(b)" in desc or "zero rated" in desc:
                    _add_tax_dict(agg["outward_3_1"]["b_zero_rated"], vals)
                elif "3.1(c)" in desc or "nil rated" in desc or "exempted" in desc:
                    _add_tax_dict(agg["outward_3_1"]["c_nil_exempt"], vals)
                elif "3.1(d)" in desc or "inward supplies (liable to reverse charge)" in desc:
                    _add_tax_dict(agg["outward_3_1"]["d_inward_rcm"], vals)
                elif "3.1(e)" in desc or "non-gst outward" in desc:
                    _add_tax_dict(agg["outward_3_1"]["e_non_gst"], vals)
                elif "4(a)(1)" in desc or "import of goods" in desc:
                    _add_tax_dict(agg["itc_4"]["4A_1_import_goods"], vals)
                elif "4(a)(2)" in desc or "import of services" in desc:
                    _add_tax_dict(agg["itc_4"]["4A_2_import_services"], vals)
                elif "4(a)(3)" in desc or "inward supplies liable to reverse charge" in desc:
                    _add_tax_dict(agg["itc_4"]["4A_3_rcm"], vals)
                elif "4(a)(4)" in desc or "inward supplies from isd" in desc:
                    _add_tax_dict(agg["itc_4"]["4A_4_isd"], vals)
                elif "4(a)(5)" in desc or "all other itc" in desc:
                    _add_tax_dict(agg["itc_4"]["4A_5_all_other"], vals)
                elif "4(b)(1)" in desc or "rule 42" in desc or "rule 43" in desc:
                    _add_tax_dict(agg["itc_4"]["4B_1_rule_42_43"], vals)
                elif "4(b)(2)" in desc or ("reversed" in desc and "other" in desc):
                    _add_tax_dict(agg["itc_4"]["4B_2_other_reversals"], vals)
                elif "4(d)(1)" in desc or "17(5)" in desc:
                    _add_tax_dict(agg["itc_4"]["4D_1_sec_17_5"], vals)
                elif "4(d)(2)" in desc or ("ineligible" in desc and "other" in desc):
                    _add_tax_dict(agg["itc_4"]["4D_2_other_ineligible"], vals)
                elif "interest" in desc:
                    agg["tax_paid_6_1"]["interest_paid"] = round(
                        agg["tax_paid_6_1"]["interest_paid"]
                        + (vals["igst"] + vals["cgst"] + vals["sgst"] + vals["cess"])
                        + (_to_float(row.get(c_cash, 0.0)) if c_cash else 0.0),
                        2,
                    )
                elif "late fee" in desc:
                    agg["tax_paid_6_1"]["late_fee_paid"] = round(
                        agg["tax_paid_6_1"]["late_fee_paid"]
                        + (vals["igst"] + vals["cgst"] + vals["sgst"] + vals["cess"])
                        + (_to_float(row.get(c_cash, 0.0)) if c_cash else 0.0),
                        2,
                    )
                elif "tax paid through itc" in desc or ("6.1" in desc and "itc" in desc):
                    _add_tax_dict(agg["tax_paid_6_1"]["itc_utilized"], vals)
                elif "tax paid in cash" in desc or ("6.1" in desc and "cash" in desc):
                    _add_tax_dict(agg["tax_paid_6_1"]["cash_paid"], vals)
                elif "6.1" in desc and c_cash and c_itc_paid:
                    # Tax head specific row like "6.1 IGST Tax Paid"
                    head = "igst" if "igst" in desc else "cgst" if "cgst" in desc else "sgst" if "sgst" in desc else "cess" if "cess" in desc else None
                    if head:
                        agg["tax_paid_6_1"]["itc_utilized"][head] = round(
                            agg["tax_paid_6_1"]["itc_utilized"][head] + _to_float(row.get(c_itc_paid, 0.0)), 2
                        )
                        agg["tax_paid_6_1"]["cash_paid"][head] = round(
                            agg["tax_paid_6_1"]["cash_paid"][head] + _to_float(row.get(c_cash, 0.0)), 2
                        )

    # Apply explicit manual UI overrides if provided (exact replacement on specified keys)
    if manual_overrides and isinstance(manual_overrides, dict):
        m3b = manual_overrides.get("gstr3b") or {}
        for sec in ("outward_3_1", "itc_4"):
            if isinstance(m3b.get(sec), dict):
                for k, v in m3b[sec].items():
                    if k in agg[sec] and isinstance(v, dict):
                        for tk in agg[sec][k]:
                            if tk in v:
                                agg[sec][k][tk] = round(_to_float(v[tk]), 2)
        if isinstance(m3b.get("tax_paid_6_1"), dict):
            tp = m3b["tax_paid_6_1"]
            for sub in ("itc_utilized", "cash_paid"):
                if isinstance(tp.get(sub), dict):
                    for tk in agg["tax_paid_6_1"][sub]:
                        if tk in tp[sub]:
                            agg["tax_paid_6_1"][sub][tk] = round(_to_float(tp[sub][tk]), 2)
            for scalar in ("interest_paid", "late_fee_paid"):
                if scalar in tp:
                    agg["tax_paid_6_1"][scalar] = round(_to_float(tp[scalar]), 2)

    return agg


def extract_purchase_rule_flags(df_books: pd.DataFrame, fy: str = "2025-26", cutoff_month: int = 11) -> pd.DataFrame:
    """
    Extract auxiliary GSTR-9 metadata from the Purchase Register without altering core reconciler inputs:
      - itc_rule_tag (Sec 17(5), Personal Sec 17(1), Rule 42 Common, Capital Goods, Input Services, Rule 37)
      - unpaid_days (>180 days flag for Rule 37)
      - hsn, uqc, qty, rate
      - period_bucket (IN_FY, NEXT_FY_CUTOFF, TIME_BARRED, OUTSIDE_FY)
      - component_tax (igst, cgst, sgst, cess, taxable)
    """
    if df_books is None or df_books.empty:
        return pd.DataFrame()

    col_map = detect_columns(df_books)
    cols = list(df_books.columns)
    c_gstin = col_map.get("gstin") or _find_col(cols, ["gstin", "gst no", "gst number"])
    c_party = col_map.get("trade_name") or _find_col(cols, ["party name", "trade/legal name", "supplier name", "particulars"])
    c_inv = col_map.get("invoice_number") or _find_col(cols, ["invoice no", "invoice number", "inv no", "bill no"])
    c_date = col_map.get("invoice_date") or _find_col(cols, ["invoice date", "inv date", "date"])
    c_taxable = col_map.get("taxable_value") or _find_col(cols, ["taxable value", "taxable amt", "taxable"])
    c_igst = col_map.get("igst") or _find_col(cols, ["integrated tax", "igst"])
    c_cgst = col_map.get("cgst") or _find_col(cols, ["central tax", "cgst"])
    c_sgst = col_map.get("sgst") or _find_col(cols, ["state/ut tax", "state tax", "sgst"])
    c_cess = col_map.get("cess") or _find_col(cols, ["cess"])
    c_itc = col_map.get("itc_availability") or _find_col(cols, ["itc eligibility", "itc availability", "itc available", "eligibility"])
    c_rcm = col_map.get("reverse_charge") or _find_col(cols, ["rcm applicable", "reverse charge", "rcm"])

    c_nature = _find_col(cols, ["itc category", "itc type", "supply nature", "eligibility tag", "rule tag", "expense type", "remarks", "reason", "narration"])
    c_unpaid_days = _find_col(cols, ["unpaid days", "overdue days", "creditor days", "ageing days", "aging days"])
    c_rule37 = _find_col(cols, ["rule 37", "180 days", "unpaid > 180"])
    c_hsn = _find_col(cols, ["hsn/sac", "hsn code", "hsn", "sac"])
    c_uqc = _find_col(cols, ["uqc", "unit", "uom"])
    c_qty = _find_col(cols, ["quantity", "qty"])
    c_rate = col_map.get("tax_rate") or _find_col(cols, ["rate (%)", "rate%", "tax rate", "rate"])
    c_rep_period = _find_col(cols, ["return period", "filing period", "claimed month", "3b period"])

    rows = []
    for _, row in df_books.iterrows():
        party = str(row.get(c_party, "") or "").strip() if c_party else ""
        if re.match(r"^(?:grand\s+|sub\s*[-\s]*)?total(?:\s*[:\-].*)?$", party.lower()):
            continue
        gstin = clean_gstin(row.get(c_gstin, "")) if c_gstin else ""
        inv = str(row.get(c_inv, "") or "").strip() if c_inv else ""
        raw_dt = row.get(c_date, "") if c_date else ""
        rep_period = row.get(c_rep_period, "") if c_rep_period else ""

        taxable = _to_float(row.get(c_taxable, 0.0)) if c_taxable else 0.0
        igst = _to_float(row.get(c_igst, 0.0)) if c_igst else 0.0
        cgst = _to_float(row.get(c_cgst, 0.0)) if c_cgst else 0.0
        sgst = _to_float(row.get(c_sgst, 0.0)) if c_sgst else 0.0
        cess = _to_float(row.get(c_cess, 0.0)) if c_cess else 0.0
        total_tax = round(igst + cgst + sgst + cess, 2)

        if taxable == 0 and total_tax == 0 and not inv:
            continue

        nature_txt = str(row.get(c_nature, "") or "").strip() if c_nature else ""
        n_low = nature_txt.lower()
        itc_inelig = parse_itc_ineligible(c_itc, row.get(c_itc, "")) if c_itc else False
        if not itc_inelig and any(k in n_low for k in ("17(5)", "17 (5)", "blocked", "ineligible", "personal", "non-business", "non business")):
            itc_inelig = True

        is_rcm = parse_rcm_flag(row.get(c_rcm, "")) if c_rcm else ("rcm" in n_low or "reverse charge" in n_low)
        unpaid_days = _to_float(row.get(c_unpaid_days, 0.0)) if c_unpaid_days else 0.0
        rule37_flag = unpaid_days > 180 or (c_rule37 and str(row.get(c_rule37, "")).strip().lower() in ("y", "yes", "true", "1")) or ("rule 37" in n_low or "180" in n_low)

        if "capital" in n_low or "capex" in n_low or "machinery" in n_low:
            itc_class = "CAPITAL_GOODS"
        elif "service" in n_low or "consult" in n_low or "audit" in n_low or "software" in n_low or "freight" in n_low:
            itc_class = "INPUT_SERVICES"
        elif "import" in n_low:
            itc_class = "IMPORT"
        else:
            itc_class = "INPUTS"

        is_common_credit = any(k in n_low for k in ("common", "rule 42", "rule 43"))
        is_personal = any(k in n_low for k in ("personal", "non-business", "17(1)"))

        dt_obj = parse_invoice_date(raw_dt)
        rows.append({
            "party_name": party,
            "gstin": gstin,
            "inv_no": inv,
            "clean_inv": clean_invoice_number(inv),
            "doc_date": dt_obj.isoformat() if dt_obj else str(raw_dt),
            "period_bucket": classify_period_bucket(raw_dt, rep_period, fy, cutoff_month),
            "itc_class": itc_class,
            "is_ineligible": itc_inelig,
            "is_personal": is_personal,
            "is_sec_17_5": itc_inelig and not is_personal,
            "is_rcm": is_rcm,
            "is_rule_37": bool(rule37_flag),
            "unpaid_days": int(unpaid_days),
            "is_common_credit": is_common_credit,
            "nature_remarks": nature_txt,
            "hsn": _clean_hsn(row.get(c_hsn, "")) if c_hsn else "",
            "uqc": str(row.get(c_uqc, "") or "").strip().upper() if c_uqc else "NOS",
            "qty": _to_float(row.get(c_qty, 0.0)) if c_qty else 0.0,
            "rate": _infer_rate_pct(taxable, igst + cgst + sgst, row.get(c_rate) if c_rate else 0.0),
            "taxable": round(taxable, 2),
            "igst": round(igst, 2),
            "cgst": round(cgst, 2),
            "sgst": round(sgst, 2),
            "cess": round(cess, 2),
            "total_tax": total_tax,
            "is_b2b": bool(gstin and len(gstin) == 15),
        })

    return pd.DataFrame(rows)


def extract_generic_files_to_df(file_tuples: List[Tuple[bytes, str]], is_portal_2b: bool = False) -> pd.DataFrame:
    """Read one or more Excel/CSV/ZIP files into a concatenated DataFrame, preserving _tw_src_file_idx."""
    frames: List[pd.DataFrame] = []
    for f_idx, (fbytes, fname) in enumerate(file_tuples):
        if not fbytes:
            continue
        if is_portal_2b:
            df_part = parse_gstr2b_excel(io.BytesIO(fbytes), filename=fname)
        else:
            sheets = _read_excel_or_csv_bytes(fbytes, fname)
            # Pick first non-readme sheet with data
            df_part = pd.DataFrame()
            for sname, sdf in sheets.items():
                if "read me" in sname.lower():
                    continue
                if not sdf.empty:
                    df_part = sdf
                    break
        if df_part is not None and not df_part.empty:
            dc = df_part.copy()
            dc["_tw_src_file_idx"] = f_idx
            frames.append(dc)

    if not frames:
        return pd.DataFrame()
    if len(frames) == 1:
        return frames[0].drop(columns=["_tw_src_file_idx"], errors="ignore")

    combined = pd.concat(frames, ignore_index=True)
    if is_portal_2b:
        gstin_cols = [c for c in combined.columns if "gstin" in str(c).lower() or "gst no" in str(c).lower()]
        inv_cols = [
            c for c in combined.columns
            if ("invoice" in str(c).lower() and "number" in str(c).lower())
            or str(c).lower().strip() in ("invoice number", "inum", "invoice no")
        ]
        if gstin_cols and inv_cols:
            latest_idx = combined.groupby([gstin_cols[0], inv_cols[0]], dropna=False)["_tw_src_file_idx"].transform("max")
            combined = combined[combined["_tw_src_file_idx"] == latest_idx]

    return combined.drop(columns=["_tw_src_file_idx"], errors="ignore").reset_index(drop=True)
