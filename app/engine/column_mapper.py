"""
Smart column mapping module for GSTR-2B and Purchase Register (Books).
Auto-detects varied column names from Tally, Busy, Marg, Zoho, SAP,
and official GST Portal Excel/CSV templates.
"""

from typing import Dict, List, Optional
import re
import pandas as pd

# Synonyms for standard fields
COLUMN_SYNONYMS = {
    "gstin": [
        "gstin of supplier", "supplier gstin", "gstin", "party gstin", "vendor gstin",
        "gstin/uin of supplier", "gstin/uin", "party's gstin", "seller gstin", "supplier_gstin"
    ],
    "trade_name": [
        "trade/legal name", "legal name", "trade name", "supplier name", "party name",
        "vendor name", "party", "supplier", "vendor", "account name", "particulars", "name"
    ],
    "invoice_number": [
        "invoice number", "invoice no.", "invoice no", "inv no.", "inv no", "bill no.",
        "bill no", "voucher no.", "voucher no", "voucher ref no", "ref no.", "ref no",
        "document number", "doc no", "invoice_no", "bill_number"
    ],
    "invoice_date": [
        "invoice date", "bill date", "date", "voucher date", "doc date", "document date",
        "inv date", "invoice_date", "bill_date"
    ],
    "invoice_value": [
        "invoice value", "total value", "total amount", "invoice amount", "inv value",
        "bill amount", "total bill value", "grand total", "net amount", "gross total"
    ],
    "taxable_value": [
        "taxable value", "taxable amount", "taxable amt", "basic amount", "ass. value",
        "assessable value", "net taxable", "base amount", "taxable"
    ],
    "total_tax": [
        "total tax", "tax amount", "total tax amount", "tax", "gst amount",
        "total gst", "tax value", "total tax (₹)", "gst (₹)"
    ],
    "igst": [
        "integrated tax", "igst", "igst amount", "integrated tax (₹)", "integrated tax amount",
        "integrated gst", "i-gst"
    ],
    "cgst": [
        "central tax", "cgst", "cgst amount", "central tax (₹)", "central tax amount",
        "central gst", "c-gst"
    ],
    "sgst": [
        "state/ut tax", "sgst", "sgst amount", "sgst/utgst", "state tax (₹)",
        "state tax amount", "state gst", "s-gst", "utgst"
    ],
    "cess": [
        "cess", "cess amount", "cess (₹)", "compensation cess"
    ]
}


def normalize_column_name(col: str) -> str:
    """Clean and standardize column name string for robust comparison."""
    if not isinstance(col, str):
        col = str(col)
    col = col.lower().strip()
    col = re.sub(r'[\r\n\t_]', ' ', col)
    col = re.sub(r'[₹()\[\].]', '', col)
    col = re.sub(r'\s+', ' ', col).strip()
    return col


def detect_columns(df: pd.DataFrame) -> Dict[str, str]:
    """
    Given a DataFrame, detect which raw column corresponds to each standard field.
    Returns dict: { standard_field: raw_column_name }
    """
    mapping: Dict[str, str] = {}
    normalized_cols = {col: normalize_column_name(col) for col in df.columns}
    used_cols = set()

    # Pass 1: Exact matches against synonyms
    for standard_field, synonyms in COLUMN_SYNONYMS.items():
        for raw_col, norm_col in normalized_cols.items():
            if raw_col in used_cols:
                continue
            if norm_col in synonyms:
                mapping[standard_field] = raw_col
                used_cols.add(raw_col)
                break

    # Pass 2: Substring matching for unmapped essential fields
    for standard_field, synonyms in COLUMN_SYNONYMS.items():
        if standard_field in mapping:
            continue
        for raw_col, norm_col in normalized_cols.items():
            if raw_col in used_cols:
                continue
            for syn in synonyms:
                # Substring check with word boundary caution
                if re.search(r'\b' + re.escape(syn) + r'\b', norm_col):
                    mapping[standard_field] = raw_col
                    used_cols.add(raw_col)
                    break
            if standard_field in mapping:
                break

    return mapping


def find_table_header_row(file_path_or_buffer, sheet_name=0) -> int:
    """
    Finds header row in GSTR-2B or Tally exports that often have 3-5 rows of metadata/titles.
    Looks for row containing 'GSTIN' or 'Invoice' or 'Bill'.
    """
    preview = pd.read_excel(file_path_or_buffer, sheet_name=sheet_name, nrows=15, header=None)
    for idx, row in preview.iterrows():
        row_str = " ".join([str(v).lower() for v in row.values if pd.notna(v)])
        if "gstin" in row_str or "invoice" in row_str or "voucher" in row_str:
            return idx
    return 0

