"""
Data Normalization & Cleaning Module for Indian GST Reconciliation.
Handles string sanitization, leading-zero stripping, special character removal,
date parsing, ITC/RCM flags, and fuzzy typo distance calculation.
"""

from datetime import date, datetime
import re
from typing import Any, Optional, Union, Tuple
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


def is_probable_typo(inv1: str, inv2: str, core1: str = "", core2: str = "") -> bool:
    """
    Checks if two invoice strings differ by an obvious typo
    (e.g. '49' vs '94', or 'INV104' vs 'INV140', or multi-digit suffix slip).
    Avoids flagging distinct sequential 1-2 digit monthly bills sharing a prefix
    (e.g. 'BB252604' vs 'BB252605' or 'RET2601' vs 'RET2608').
    """
    if not inv1 or not inv2:
        return False
    if inv1 == inv2:
        return False

    # Check full-string character transposition (e.g. '49' vs '94', 'INV104' vs 'INV140')
    if sorted(inv1) == sorted(inv2) and len(inv1) == len(inv2):
        return True

    # If core serial digits are both available and differ:
    if core1 and core2 and core1 != core2:
        # Check transposition in trailing serial digits (e.g. 'INV26049' vs 'BILL94')
        if sorted(core1) == sorted(core2) and len(core1) == len(core2):
            return True

        # Single- or two-digit core numbers that aren't transpositions (e.g. '1' vs '8', '4' vs '5')
        # are distinct sequential bills sharing a long prefix rather than a typo.
        if len(core1) <= 2 and len(core2) <= 2:
            return False

        # For 3+ digit invoice serials, compare core serial similarity directly
        return calculate_similarity(core1, core2) >= 0.66

    # Fallback full-string similarity check when core digits aren't distinct
    return calculate_similarity(inv1, inv2) >= 0.75


_DATE_FORMATS = (
    "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y",
    "%d-%b-%Y", "%d-%B-%Y", "%d %b %Y", "%d %B %Y",
    "%d-%b-%y", "%d/%m/%y", "%d-%m-%y", "%Y/%m/%d",
    "%m/%d/%Y",
)


def parse_invoice_date(val: Any) -> Optional[date]:
    """Parse common Indian / ISO / Excel invoice date values into datetime.date."""
    if val is None or val != val:
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val

    s = str(val).strip()
    if not s or s.lower() in ("nan", "none", "nat", "-", "nil"):
        return None
    if " " in s and ("00:00:00" in s or re.match(r"^\d{4}-\d{2}-\d{2}\s+\d{2}:", s)):
        s = s.split(" ")[0].strip()

    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def is_sequential_periodic_pair(
    b_core: str,
    p_core: str,
    b_clean: str,
    p_clean: str,
    b_date: Optional[date],
    p_date: Optional[date],
) -> bool:
    """
    Detect distinct recurring monthly/periodic fixed bills (e.g. 'BB/25-26/04' in Apr vs
    'BB/25-26/05' in May) so Pass 3 never falsely merges them as a clerical typo.
    """
    if sorted(b_clean) == sorted(p_clean) and len(b_clean) == len(p_clean):
        return False
    if b_core and p_core and sorted(b_core) == sorted(p_core) and len(b_core) == len(p_core):
        return False

    if b_date and p_date and abs((b_date - p_date).days) >= 20:
        return True

    if b_core.isdigit() and p_core.isdigit() and abs(int(b_core) - int(p_core)) == 1:
        if b_date and p_date and (b_date.year != p_date.year or b_date.month != p_date.month):
            return True

    return False


def parse_itc_ineligible(col_name: str, val: Any) -> bool:
    """
    Determine whether an invoice line carries blocked / ineligible ITC status.
    Supports official GSTR-2B 'ITC Availability' ('Yes'/'No') as well as ERP
    'Blocked ITC' or Section 17(5) remarks.
    """
    if val is None or val != val:
        return False
    s = str(val).strip().lower()
    if not s or s in ("nan", "none", "-", "nil"):
        return False

    if any(k in s for k in ("ineligible", "blocked", "17(5)", "175", "personal", "disallowed", "reverse")):
        return True

    col_l = (col_name or "").lower()
    if "blocked" in col_l:
        return s in ("yes", "y", "true", "1")
    return s in ("no", "n", "false", "0")


def parse_rcm_flag(val: Any) -> bool:
    """Determine whether an invoice line attracts Reverse Charge Mechanism (RCM)."""
    if val is None or val != val:
        return False
    s = str(val).strip().lower()
    if not s or s in ("nan", "none", "-", "nil"):
        return False
    return s in ("y", "yes", "true", "1", "rcm", "applicable") or s.startswith("yes")


_GST_SLABS = (0.0, 0.25, 3.0, 5.0, 12.0, 18.0, 28.0, 40.0)


def infer_gst_rate_label(raw_rate: Any, taxable: float, total_tax: float) -> str:
    """
    Return clean GST rate slab label (e.g. '5%', '18%') from explicit Rate column
    or inferred from (total_tax / taxable) * 100.
    """
    parsed_rate: Optional[float] = None
    if raw_rate is not None and raw_rate == raw_rate:
        s = str(raw_rate).strip().replace("%", "").strip()
        if s and s.lower() not in ("nan", "none", "-", "nil"):
            try:
                val = float(s)
                if 0 < val < 1.0 and "%" in str(raw_rate):
                    val *= 100.0
                elif val in (0.03, 0.05, 0.12, 0.18, 0.28):
                    val *= 100.0
                parsed_rate = round(val, 2)
            except ValueError:
                parsed_rate = None

    if (parsed_rate is None or (parsed_rate == 0.0 and total_tax > 0.01)) and taxable > 0:
        inferred = (total_tax / taxable) * 100.0
        nearest = min(_GST_SLABS, key=lambda sl: abs(sl - inferred))
        if abs(nearest - inferred) <= 0.35:
            parsed_rate = nearest
        else:
            parsed_rate = round(inferred, 1)

    if parsed_rate is None:
        return ""
    if abs(parsed_rate - round(parsed_rate)) < 0.01:
        return f"{int(round(parsed_rate))}%"
    return f"{parsed_rate:g}%"


