"""
Data Normalization & Cleaning Module for Indian GST Reconciliation.
Handles string sanitization, leading-zero stripping, special character removal,
and fuzzy typo distance calculation.
"""

import re
from typing import Optional, Union, Tuple
import difflib


def clean_gstin(val: Union[str, float, int]) -> str:
    """
    Standardize 15-character GSTIN.
    Strips spaces, dashes, converts to uppercase.
    """
    if val is None or val != val:  # check for NaN
        return ""
    gstin = str(val).strip().upper()
    gstin = re.sub(r'[^A-Z0-9]', '', gstin)
    return gstin


def clean_amount(val: Union[str, float, int]) -> float:
    """
    Parse monetary values: handles commas, rupee symbols, negative brackets.
    Returns float rounded to 2 decimal places.
    """
    if val is None or val != val:  # NaN check
        return 0.0
    if isinstance(val, (int, float)):
        return round(float(val), 2)

    val_str = str(val).strip()
    if not val_str or val_str.lower() in ("nan", "none", "-", "nil"):
        return 0.0

    # Handle negative brackets e.g. (1,200.50)
    is_negative = False
    if val_str.startswith("(") and val_str.endswith(")"):
        is_negative = True
        val_str = val_str[1:-1]
    elif val_str.startswith("-"):
        is_negative = True
        val_str = val_str[1:]

    # Remove currency symbols, commas, spaces
    val_str = re.sub(r'[₹$,\s]', '', val_str)

    try:
        amt = float(val_str)
        return round(-amt if is_negative else amt, 2)
    except ValueError:
        return 0.0


def clean_invoice_number(val: Union[str, float, int]) -> Tuple[str, str, str]:
    """
    Standardize invoice number. Returns a 3-tuple:
    1. raw_inv: trimmed raw string for display
    2. clean_inv: stripped of prefixes, symbols (/,-,#), leading zeros
    3. core_digits: numeric-only sequence (e.g. '0049' -> '49')

    Examples:
    - '0049' -> ('0049', '49', '49')
    - 'INV/2023-24/0049' -> ('INV/2023-24/0049', 'INV20232449', '49')
    - '49' -> ('49', '49', '49')
    - 'BILL # 94' -> ('BILL # 94', 'BILL94', '94')
    """
    if val is None or val != val:
        return ("", "", "")

    raw_inv = str(val).strip()
    if raw_inv.endswith(".0"):  # Excel auto-float issue (e.g. 104 -> 104.0)
        raw_inv = raw_inv[:-2]

    # Upper case
    inv_upper = raw_inv.upper()

    # Cleaned: remove all non-alphanumeric
    alphanumeric = re.sub(r'[^A-Z0-9]', '', inv_upper)

    # Strip leading zeros from alphanumeric string if it starts with zeros
    # e.g. '00049' -> '49', '00A12' -> 'A12'
    stripped_zeros = re.sub(r'^0+', '', alphanumeric)
    clean_inv = stripped_zeros if stripped_zeros else alphanumeric

    # Extract core digits / suffix digits for fallback matching
    digit_matches = re.findall(r'\d+', inv_upper)
    if digit_matches:
        # Take the last contiguous digit group (usually the bill serial)
        last_digits = digit_matches[-1]
        core_digits = str(int(last_digits)) if last_digits.isdigit() else last_digits
    else:
        core_digits = clean_inv

    return raw_inv, clean_inv, core_digits


def calculate_similarity(str1: str, str2: str) -> float:
    """
    Calculates Levenshtein-like string similarity ratio between 0.0 and 1.0.
    """
    if not str1 or not str2:
        return 0.0
    if str1 == str2:
        return 1.0
    return difflib.SequenceMatcher(None, str1, str2).ratio()


def is_probable_typo(inv1: str, inv2: str) -> bool:
    """
    Checks if two invoice strings differ by an obvious typo
    (e.g. '49' vs '94', or 'INV104' vs 'INV140', or single char edit).
    """
    if not inv1 or not inv2:
        return False
    if inv1 == inv2:
        return False

    # Check digit transposition (e.g. sorted characters match)
    if sorted(inv1) == sorted(inv2) and len(inv1) == len(inv2):
        return True

    # Check character edit distance (length diff <= 1 and similarity >= 0.70)
    sim = calculate_similarity(inv1, inv2)
    return sim >= 0.70

