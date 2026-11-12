"""
Core GST Multi-Tier Reconciliation Engine.
Executes 5 sequential matching passes:
1. Exact Match (GSTIN + Inv No + Taxable + Tax)
2. Normalized Match (Cleaned/Zero-stripped Inv No + Amount)
3. Probable Typo Match (Exact Amount & GSTIN, Invoice Typo e.g. 49 vs 94)
4. Value / Tax Discrepancy (GSTIN + Inv No Match, Amount Diff > Tolerance)
5. Missing in 2B (ITC at Risk) / Missing in Books (Unbooked)
"""

from collections import defaultdict
from typing import Dict, List, Any, Tuple
import pandas as pd
from app.engine.column_mapper import detect_columns
from app.engine.normalizer import (
    clean_gstin, clean_amount, clean_invoice_number, is_probable_typo
)


def prepare_records(df: pd.DataFrame, source_type: str) -> List[Dict[str, Any]]:
    """
    Standardize raw DataFrame into list of structured record dictionaries.
    """
    mapping = detect_columns(df)
    records = []

    for idx, row in df.iterrows():
        # GSTIN
        raw_gstin = row.get(mapping.get("gstin", ""), "")
        gstin = clean_gstin(raw_gstin)

        # Invoice Number
        raw_inv_val = row.get(mapping.get("invoice_number", ""), "")
        raw_inv, clean_inv, core_digits = clean_invoice_number(raw_inv_val)

        # Party Name
        party = str(row.get(mapping.get("trade_name", ""), "") or "").strip()

        # Date
        date_val = str(row.get(mapping.get("invoice_date", ""), "") or "").strip()
        if date_val.endswith("00:00:00"):
            date_val = date_val.replace("00:00:00", "").strip()

        # Amounts
        taxable = clean_amount(row.get(mapping.get("taxable_value", 0.0), 0.0))
        igst = clean_amount(row.get(mapping.get("igst", 0.0), 0.0))
        cgst = clean_amount(row.get(mapping.get("cgst", 0.0), 0.0))
        sgst = clean_amount(row.get(mapping.get("sgst", 0.0), 0.0))
        cess = clean_amount(row.get(mapping.get("cess", 0.0), 0.0))
        computed_tax = round(igst + cgst + sgst + cess, 2)
        if computed_tax > 0.0:
            total_tax = computed_tax
        elif "total_tax" in mapping:
            total_tax = clean_amount(row.get(mapping["total_tax"], 0.0))
        else:
            total_tax = 0.0
        total_inv_val = clean_amount(row.get(mapping.get("invoice_value", 0.0), taxable + total_tax))

        # Skip completely empty rows
        if not gstin and not raw_inv and taxable == 0.0 and total_tax == 0.0:
            continue

        records.append({
            "id": f"{source_type}_{idx}",
            "source": source_type,
            "raw_gstin": str(raw_gstin),
            "gstin": gstin,
            "party_name": party,
            "raw_inv": raw_inv,
            "clean_inv": clean_inv,
            "core_digits": core_digits,
            "inv_date": date_val,
            "taxable": taxable,
            "igst": igst,
            "cgst": cgst,
            "sgst": sgst,
            "cess": cess,
            "total_tax": total_tax,
            "total_inv_val": total_inv_val,
        })

    return records


def run_reconciliation(
    df_2b: pd.DataFrame,
    df_books: pd.DataFrame,
    tolerance: float = 1.00,
    include_results: bool = True
) -> Dict[str, Any]:
    """
    Main reconciliation pipeline.
    Compares GSTR-2B against Books and returns detailed reconciled records + summary metrics.
    """
    records_2b = prepare_records(df_2b, "2B")
    records_books = prepare_records(df_books, "BOOKS")

    # Bucket 2B records by GSTIN so passes run in O(N) per vendor instead of O(N*M)
    records_2b_by_gstin: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for p in records_2b:
        if p["gstin"]:
            records_2b_by_gstin[p["gstin"]].append(p)

    matched_2b_ids = set()
    matched_books_ids = set()
    results: List[Dict[str, Any]] = []

    # Helper: Check amount tolerance
    def amounts_match(b: Dict[str, Any], p: Dict[str, Any]) -> bool:
        taxable_diff = abs(b["taxable"] - p["taxable"])
        tax_diff = abs(b["total_tax"] - p["total_tax"])
        return taxable_diff <= tolerance and tax_diff <= tolerance

    # -------------------------------------------------------------
    # PASS 1: Exact Match (GSTIN + Exact Cleaned Inv No + Amounts)
    # -------------------------------------------------------------
    for b in records_books:
        if b["id"] in matched_books_ids or not b["gstin"]:
            continue
        for p in records_2b_by_gstin.get(b["gstin"], ()):
            if p["id"] in matched_2b_ids:
                continue
            if b["clean_inv"] and b["clean_inv"] == p["clean_inv"] and amounts_match(b, p):
                matched_books_ids.add(b["id"])
                matched_2b_ids.add(p["id"])
                results.append({
                    "status": "EXACT_MATCH",
                    "status_label": "Exact Match",
                    "status_badge": "success",
                    "color_code": "#15803d",
                    "books": b,
                    "portal": p,
                    "taxable_diff": round(b["taxable"] - p["taxable"], 2),
                    "tax_diff": round(b["total_tax"] - p["total_tax"], 2),
                    "remarks": "100% Match across GSTIN, Invoice Number, and Amounts."
                })
                break

    # -------------------------------------------------------------
    # PASS 2: Normalized Match (Leading zeros / separators stripped)
    # -------------------------------------------------------------
    for b in records_books:
        if b["id"] in matched_books_ids or not b["gstin"]:
            continue
        for p in records_2b_by_gstin.get(b["gstin"], ()):
            if p["id"] in matched_2b_ids:
                continue
            # Match on core digits or one being substring/stripped zero version
            inv_match = (
                (b["core_digits"] and b["core_digits"] == p["core_digits"]) or
                b["clean_inv"].lstrip("0") == p["clean_inv"].lstrip("0")
            )
            if inv_match and amounts_match(b, p):
                matched_books_ids.add(b["id"])
                matched_2b_ids.add(p["id"])
                results.append({
                    "status": "NORMALIZED_MATCH",
                    "status_label": "Normalized Match",
                    "status_badge": "warning",
                    "color_code": "#b45309",
                    "books": b,
                    "portal": p,
                    "taxable_diff": round(b["taxable"] - p["taxable"], 2),
                    "tax_diff": round(b["total_tax"] - p["total_tax"], 2),
                    "remarks": f"Matched after normalizing invoice formatting ('{b['raw_inv']}' vs '{p['raw_inv']}')."
                })
                break

    # -------------------------------------------------------------
    # PASS 3: Probable Typo Match (Exact Amount & GSTIN, Inv typo)
    # -------------------------------------------------------------
    for b in records_books:
        if b["id"] in matched_books_ids or not b["gstin"]:
            continue
        for p in records_2b_by_gstin.get(b["gstin"], ()):
            if p["id"] in matched_2b_ids:
                continue
            if amounts_match(b, p):
                # Amount matches perfectly, check if invoice has typo (e.g. 49 vs 94)
                if is_probable_typo(b["clean_inv"], p["clean_inv"]) or (
                    len(b["clean_inv"]) > 0 and len(p["clean_inv"]) > 0
                ):
                    matched_books_ids.add(b["id"])
                    matched_2b_ids.add(p["id"])
                    results.append({
                        "status": "PROBABLE_TYPO",
                        "status_label": "Probable Typo (Verify)",
                        "status_badge": "warning",
                        "color_code": "#d97706",
                        "books": b,
                        "portal": p,
                        "taxable_diff": round(b["taxable"] - p["taxable"], 2),
                        "tax_diff": round(b["total_tax"] - p["total_tax"], 2),
                        "remarks": f"GSTIN & Amount match; Invoice No differ (Books: '{b['raw_inv']}' vs Portal: '{p['raw_inv']}')."
                    })
                    break

    # -------------------------------------------------------------
    # PASS 4: Value / Tax Discrepancy (GSTIN + Inv match, Amount differs)
    # -------------------------------------------------------------
    for b in records_books:
        if b["id"] in matched_books_ids or not b["gstin"]:
            continue
        for p in records_2b_by_gstin.get(b["gstin"], ()):
            if p["id"] in matched_2b_ids:
                continue
            if b["clean_inv"] and (b["clean_inv"] == p["clean_inv"] or b["core_digits"] == p["core_digits"]):
                matched_books_ids.add(b["id"])
                matched_2b_ids.add(p["id"])
                taxable_diff = round(b["taxable"] - p["taxable"], 2)
                tax_diff = round(b["total_tax"] - p["total_tax"], 2)
                results.append({
                    "status": "VALUE_MISMATCH",
                    "status_label": "Value Mismatch",
                    "status_badge": "orange",
                    "color_code": "#ea580c",
                    "books": b,
                    "portal": p,
                    "taxable_diff": taxable_diff,
                    "tax_diff": tax_diff,
                    "remarks": f"Invoice match, but value difference: Taxable diff ₹{taxable_diff}, Tax diff ₹{tax_diff}."
                })
                break

    # -------------------------------------------------------------
    # PASS 5A: Missing in 2B (Books Only -> ITC at Risk)
    # -------------------------------------------------------------
    for b in records_books:
        if b["id"] not in matched_books_ids:
            results.append({
                "status": "MISSING_IN_2B",
                "status_label": "Missing in 2B (ITC at Risk)",
                "status_badge": "danger",
                "color_code": "#dc2626",
                "books": b,
                "portal": None,
                "taxable_diff": b["taxable"],
                "tax_diff": b["total_tax"],
                "remarks": "Vendor has not filed GSTR-1. Ineligible for ITC claim until uploaded by supplier."
            })

    # -------------------------------------------------------------
    # PASS 5B: Missing in Books (2B Only -> Unbooked Purchases)
    # -------------------------------------------------------------
    for p in records_2b:
        if p["id"] not in matched_2b_ids:
            results.append({
                "status": "MISSING_IN_BOOKS",
                "status_label": "Missing in Books (Unbooked)",
                "status_badge": "info",
                "color_code": "#0284c7",
                "books": None,
                "portal": p,
                "taxable_diff": -p["taxable"],
                "tax_diff": -p["total_tax"],
                "remarks": "Invoice present on GST portal but omitted from internal ERP/Tally."
            })

    # -------------------------------------------------------------
    # Calculate Executive Summary KPI Metrics
    # -------------------------------------------------------------
    total_books_invoices = len(records_books)
    total_2b_invoices = len(records_2b)
    
    books_taxable_total = sum(r["taxable"] for r in records_books)
    books_tax_total = sum(r["total_tax"] for r in records_books)
    portal_taxable_total = sum(r["taxable"] for r in records_2b)
    portal_tax_total = sum(r["total_tax"] for r in records_2b)

    matched_count = sum(1 for r in results if r["status"] in ("EXACT_MATCH", "NORMALIZED_MATCH", "PROBABLE_TYPO"))
    exact_count = sum(1 for r in results if r["status"] == "EXACT_MATCH")
    normalized_count = sum(1 for r in results if r["status"] == "NORMALIZED_MATCH")
    typo_count = sum(1 for r in results if r["status"] == "PROBABLE_TYPO")
    value_mismatch_count = sum(1 for r in results if r["status"] == "VALUE_MISMATCH")
    missing_in_2b_count = sum(1 for r in results if r["status"] == "MISSING_IN_2B")
    missing_in_books_count = sum(1 for r in results if r["status"] == "MISSING_IN_BOOKS")

    # Financial sums
    matched_tax = sum(
        r["books"]["total_tax"] for r in results
        if r["status"] in ("EXACT_MATCH", "NORMALIZED_MATCH") and r["books"]
    )
    itc_at_risk_tax = sum(
        r["books"]["total_tax"] for r in results
        if r["status"] == "MISSING_IN_2B" and r["books"]
    )
    unbooked_tax = sum(
        r["portal"]["total_tax"] for r in results
        if r["status"] == "MISSING_IN_BOOKS" and r["portal"]
    )
    mismatch_variance_tax = sum(
        abs(r["tax_diff"]) for r in results
        if r["status"] == "VALUE_MISMATCH"
    )

    reconciliation_rate = (
        round((matched_count / total_books_invoices) * 100, 1)
        if total_books_invoices > 0 else 0.0
    )

    summary = {
        "total_books_invoices": total_books_invoices,
        "total_2b_invoices": total_2b_invoices,
        "books_taxable_total": round(books_taxable_total, 2),
        "books_tax_total": round(books_tax_total, 2),
        "portal_taxable_total": round(portal_taxable_total, 2),
        "portal_tax_total": round(portal_tax_total, 2),
        "matched_count": matched_count,
        "exact_count": exact_count,
        "normalized_count": normalized_count,
        "typo_count": typo_count,
        "value_mismatch_count": value_mismatch_count,
        "missing_in_2b_count": missing_in_2b_count,
        "missing_in_books_count": missing_in_books_count,
        "matched_tax": round(matched_tax, 2),
        "itc_at_risk_tax": round(itc_at_risk_tax, 2),
        "unbooked_tax": round(unbooked_tax, 2),
        "mismatch_variance_tax": round(mismatch_variance_tax, 2),
        "reconciliation_rate": reconciliation_rate,
        "tolerance": tolerance,
    }

    # Structure KPI dictionary aligned with frontend expectations
    kpis = {
        "total_tax_books": summary["books_tax_total"],
        "total_invoices_books": summary["total_books_invoices"],
        "total_tax_2b": summary["portal_tax_total"],
        "total_invoices_2b": summary["total_2b_invoices"],
        "matched_itc": summary["matched_tax"],
        "matched_count": summary["matched_count"],
        "reconciliation_percentage": summary["reconciliation_rate"],
        "itc_at_risk": summary["itc_at_risk_tax"],
        "itc_at_risk_count": summary["missing_in_2b_count"],
        "unbooked_itc": summary["unbooked_tax"],
        "unbooked_count": summary["missing_in_books_count"],
        "category_counts": {
            "EXACT_MATCH": summary["exact_count"],
            "NORMALIZED_MATCH": summary["normalized_count"],
            "TYPO_MATCH": summary["typo_count"],
            "PROBABLE_TYPO": summary["typo_count"],
            "VALUE_MISMATCH": summary["value_mismatch_count"],
            "MISSING_IN_2B": summary["missing_in_2b_count"],
            "MISSING_IN_BOOKS": summary["missing_in_books_count"]
        }
    }

    # Flatten results into ledger array for frontend table rendering
    ledger = []
    for r in results:
        b = r.get("books") or {}
        p = r.get("portal") or {}
        status = r.get("status", "")
        cat = "TYPO_MATCH" if status == "PROBABLE_TYPO" else status
        ledger.append({
            "match_category": cat,
            "status": status,
            "status_label": r.get("status_label", ""),
            "supplier_name_2b": p.get("party_name", ""),
            "supplier_name_books": b.get("party_name", ""),
            "gstin_2b": p.get("gstin", ""),
            "gstin_books": b.get("gstin", ""),
            "inv_num_2b": p.get("raw_inv", ""),
            "inv_num_books": b.get("raw_inv", ""),
            "inv_date_books": b.get("inv_date", ""),
            "inv_date_2b": p.get("inv_date", ""),
            "taxable_books": b.get("taxable") if b else None,
            "tax_amount_books": b.get("total_tax") if b else None,
            "total_inv_val_books": b.get("total_inv_val") if b else None,
            "taxable_2b": p.get("taxable") if p else None,
            "tax_amount_2b": p.get("total_tax") if p else None,
            "total_inv_val_2b": p.get("total_inv_val") if p else None,
            "tax_diff": r.get("tax_diff", 0.0),
            "taxable_diff": r.get("taxable_diff", 0.0),
            "audit_remark": r.get("remarks", "")
        })

    payload = {
        "summary": summary,
        "kpis": kpis,
        "ledger": ledger
    }
    if include_results:
        payload["results"] = results
    return payload

