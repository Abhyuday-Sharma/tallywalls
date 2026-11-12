"""
Core GST Multi-Tier Reconciliation Engine.
Executes sequential matching passes:
1. Exact Match (1-to-1: GSTIN + Inv No + Taxable + Tax)
2. Normalized Match (1-to-1: Cleaned/Zero-stripped Inv No + Amount)
2.5. Multi-Rate Invoice Consolidation (N-to-1 / 1-to-N / N-to-M sharing GSTIN + Inv No)
3. Probable Typo Match (Exact Amount & GSTIN, genuine Invoice Typo with sequential-bill guard)
4. Value / Tax Discrepancy (Multi-Rate Consolidated + 1-to-1 GSTIN + Inv Match, Amount Diff > Tolerance)
5. Missing in 2B (ITC at Risk, with Duplicate-Booking guard) / Missing in Books (Unbooked)
Also flags Ineligible/Blocked ITC, Reverse Charge (RCM), and Tax-Head Swaps (IGST vs CGST+SGST).
"""

from collections import defaultdict
from typing import Dict, List, Any, Optional, Tuple
import pandas as pd
from app.engine.column_mapper import detect_columns
from app.engine.normalizer import (
    clean_gstin,
    clean_amount,
    clean_invoice_number,
    is_probable_typo,
    parse_invoice_date,
    is_sequential_periodic_pair,
    parse_itc_ineligible,
    parse_rcm_flag,
    infer_gst_rate_label,
)


def prepare_records(df: pd.DataFrame, source_type: str) -> List[Dict[str, Any]]:
    """
    Standardize raw DataFrame into list of structured record dictionaries.
    """
    mapping = detect_columns(df)
    records = []

    gstin_col = mapping.get("gstin", "")
    inv_col = mapping.get("invoice_number", "")
    party_col = mapping.get("trade_name", "")
    date_col = mapping.get("invoice_date", "")
    taxable_col = mapping.get("taxable_value", "")
    igst_col = mapping.get("igst", "")
    cgst_col = mapping.get("cgst", "")
    sgst_col = mapping.get("sgst", "")
    cess_col = mapping.get("cess", "")
    total_tax_col = mapping.get("total_tax", "")
    inv_val_col = mapping.get("invoice_value", "")
    itc_col = mapping.get("itc_availability", "")
    rcm_col = mapping.get("reverse_charge", "")
    rate_col = mapping.get("tax_rate", "")

    for idx, row in df.iterrows():
        raw_gstin = row.get(gstin_col, "") if gstin_col else ""
        gstin = clean_gstin(raw_gstin)

        raw_inv_val = row.get(inv_col, "") if inv_col else ""
        raw_inv, clean_inv, core_digits = clean_invoice_number(raw_inv_val)

        raw_party = row.get(party_col, "") if party_col else ""
        party = "" if (raw_party is None or raw_party != raw_party) else str(raw_party).strip()

        raw_date = row.get(date_col, "") if date_col else ""
        date_obj = parse_invoice_date(raw_date)
        date_val = "" if (raw_date is None or raw_date != raw_date) else str(raw_date).strip()
        if "00:00:00" in date_val:
            date_val = date_val.replace("00:00:00", "").strip()

        taxable = clean_amount(row.get(taxable_col, 0.0) if taxable_col else 0.0)
        igst = clean_amount(row.get(igst_col, 0.0) if igst_col else 0.0)
        cgst = clean_amount(row.get(cgst_col, 0.0) if cgst_col else 0.0)
        sgst = clean_amount(row.get(sgst_col, 0.0) if sgst_col else 0.0)
        cess = clean_amount(row.get(cess_col, 0.0) if cess_col else 0.0)
        computed_tax = round(igst + cgst + sgst + cess, 2)
        if computed_tax > 0.0:
            total_tax = computed_tax
        elif total_tax_col:
            total_tax = clean_amount(row.get(total_tax_col, 0.0))
        else:
            total_tax = 0.0
        total_inv_val = clean_amount(
            row.get(inv_val_col, taxable + total_tax) if inv_val_col else (taxable + total_tax)
        )

        # Skip completely empty rows
        if not gstin and not raw_inv and taxable == 0.0 and total_tax == 0.0:
            continue

        itc_ineligible = parse_itc_ineligible(itc_col, row.get(itc_col, "")) if itc_col else False
        is_rcm = parse_rcm_flag(row.get(rcm_col, "")) if rcm_col else False
        rate_label = infer_gst_rate_label(
            row.get(rate_col) if rate_col else None, taxable, total_tax
        )

        records.append({
            "id": f"{source_type}_{idx}",
            "source": source_type,
            "raw_gstin": "" if (raw_gstin is None or raw_gstin != raw_gstin) else str(raw_gstin),
            "gstin": gstin,
            "party_name": party,
            "raw_inv": raw_inv,
            "clean_inv": clean_inv,
            "core_digits": core_digits,
            "inv_date": date_val,
            "_date_obj": date_obj,
            "taxable": taxable,
            "igst": igst,
            "cgst": cgst,
            "sgst": sgst,
            "cess": cess,
            "total_tax": total_tax,
            "total_inv_val": total_inv_val,
            "itc_ineligible": itc_ineligible,
            "is_rcm": is_rcm,
            "rate_label": rate_label,
            "line_count": 1,
            "slabs": [rate_label] if rate_label else [],
        })

    return records


def _sort_slabs(slabs: List[str]) -> List[str]:
    def _key(s: str) -> Tuple[int, float, str]:
        try:
            return (0, float(s.rstrip("%")), s)
        except Exception:
            return (1, 0.0, s)

    seen = []
    for sl in slabs:
        if sl and sl not in seen:
            seen.append(sl)
    return sorted(seen, key=_key)


def _aggregate_group(recs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Combine one or more line items belonging to the same invoice into one aggregated record."""
    if len(recs) == 1:
        r = dict(recs[0])
        r["line_count"] = 1
        r["slabs"] = [r["rate_label"]] if r.get("rate_label") else []
        return r

    first = recs[0]
    slabs = _sort_slabs([r.get("rate_label", "") for r in recs if r.get("rate_label")])
    raw_inv = next((r["raw_inv"] for r in recs if r.get("raw_inv")), "")
    party_name = next((r["party_name"] for r in recs if r.get("party_name")), "")
    inv_date = next((r["inv_date"] for r in recs if r.get("inv_date")), "")
    date_obj = next((r.get("_date_obj") for r in recs if r.get("_date_obj") is not None), None)

    return {
        "id": "|".join(r["id"] for r in recs),
        "source": first["source"],
        "raw_gstin": first["raw_gstin"],
        "gstin": first["gstin"],
        "party_name": party_name,
        "raw_inv": raw_inv,
        "clean_inv": first["clean_inv"],
        "core_digits": first["core_digits"],
        "inv_date": inv_date,
        "_date_obj": date_obj,
        "taxable": round(sum(r["taxable"] for r in recs), 2),
        "igst": round(sum(r["igst"] for r in recs), 2),
        "cgst": round(sum(r["cgst"] for r in recs), 2),
        "sgst": round(sum(r["sgst"] for r in recs), 2),
        "cess": round(sum(r["cess"] for r in recs), 2),
        "total_tax": round(sum(r["total_tax"] for r in recs), 2),
        "total_inv_val": round(sum(r["total_inv_val"] for r in recs), 2),
        "itc_ineligible": any(r.get("itc_ineligible") for r in recs),
        "is_rcm": any(r.get("is_rcm") for r in recs),
        "rate_label": ", ".join(slabs),
        "line_count": len(recs),
        "slabs": slabs,
    }


def _inv_normalized_match(b: Dict[str, Any], p: Dict[str, Any]) -> bool:
    if not b.get("clean_inv") or not p.get("clean_inv"):
        return False
    return bool(
        (b["core_digits"] and b["core_digits"] == p["core_digits"])
        or b["clean_inv"].lstrip("0") == p["clean_inv"].lstrip("0")
    )


def _check_tax_head_swap(
    b: Optional[Dict[str, Any]],
    p: Optional[Dict[str, Any]],
    tolerance: float,
) -> bool:
    if not b or not p:
        return False
    b_split = b["igst"] + b["cgst"] + b["sgst"]
    p_split = p["igst"] + p["cgst"] + p["sgst"]
    if b_split <= 0.01 or p_split <= 0.01:
        return False

    b_cs = b["cgst"] + b["sgst"]
    p_cs = p["cgst"] + p["sgst"]
    books_igst_vs_portal_cs = b["igst"] > tolerance and p_cs > tolerance and p["igst"] <= tolerance
    books_cs_vs_portal_igst = b_cs > tolerance and p["igst"] > tolerance and b["igst"] <= tolerance
    return bool(books_igst_vs_portal_cs or books_cs_vs_portal_igst)


def _finalize_result(
    status: str,
    status_label: str,
    status_badge: str,
    color_code: str,
    b: Optional[Dict[str, Any]],
    p: Optional[Dict[str, Any]],
    base_remark: str,
    tolerance: float,
) -> Dict[str, Any]:
    tax_head_swap = _check_tax_head_swap(b, p, tolerance)
    itc_ineligible = bool((b and b.get("itc_ineligible")) or (p and p.get("itc_ineligible")))
    is_rcm = bool((b and b.get("is_rcm")) or (p and p.get("is_rcm")))

    notes = [base_remark]
    if tax_head_swap:
        notes.append("Warning: Tax-head swap (IGST vs CGST+SGST) - verify Place of Supply.")
    if itc_ineligible:
        notes.append("[INELIGIBLE ITC: Marked 'No' / Blocked in GSTR-2B - do not claim]")
    if is_rcm:
        notes.append("[RCM: Reverse Charge supply]")

    b_taxable = b["taxable"] if b else 0.0
    p_taxable = p["taxable"] if p else 0.0
    b_tax = b["total_tax"] if b else 0.0
    p_tax = p["total_tax"] if p else 0.0

    return {
        "status": status,
        "status_label": status_label,
        "status_badge": status_badge,
        "color_code": color_code,
        "books": b,
        "portal": p,
        "books_line_count": b.get("line_count", 1) if b else 0,
        "portal_line_count": p.get("line_count", 1) if p else 0,
        "books_slabs": ", ".join(b.get("slabs", [])) if b else "",
        "portal_slabs": ", ".join(p.get("slabs", [])) if p else "",
        "itc_ineligible": itc_ineligible,
        "is_rcm": is_rcm,
        "tax_head_mismatch": tax_head_swap,
        "taxable_diff": round(b_taxable - p_taxable, 2),
        "tax_diff": round(b_tax - p_tax, 2),
        "remarks": " ".join(notes),
    }


def run_reconciliation(
    df_2b: pd.DataFrame,
    df_books: pd.DataFrame,
    tolerance: float = 1.00,
    include_results: bool = True,
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
    matched_books_keys = set()  # (gstin, clean_inv) that had at least 1 matched line
    matched_2b_keys = set()
    results: List[Dict[str, Any]] = []

    def amounts_match(b: Dict[str, Any], p: Dict[str, Any]) -> bool:
        taxable_diff = abs(b["taxable"] - p["taxable"])
        tax_diff = abs(b["total_tax"] - p["total_tax"])
        return taxable_diff <= tolerance and tax_diff <= tolerance

    def mark_matched(b_recs: List[Dict[str, Any]], p_recs: List[Dict[str, Any]]) -> None:
        for r in b_recs:
            matched_books_ids.add(r["id"])
            if r["gstin"] and r["clean_inv"]:
                matched_books_keys.add((r["gstin"], r["clean_inv"]))
        for r in p_recs:
            matched_2b_ids.add(r["id"])
            if r["gstin"] and r["clean_inv"]:
                matched_2b_keys.add((r["gstin"], r["clean_inv"]))

    # -------------------------------------------------------------
    # PASS 1: 1-to-1 Exact Match (GSTIN + Exact Cleaned Inv No + Amounts)
    # -------------------------------------------------------------
    for b in records_books:
        if b["id"] in matched_books_ids or not b["gstin"]:
            continue
        for p in records_2b_by_gstin.get(b["gstin"], ()):
            if p["id"] in matched_2b_ids:
                continue
            if b["clean_inv"] and b["clean_inv"] == p["clean_inv"] and amounts_match(b, p):
                mark_matched([b], [p])
                results.append(_finalize_result(
                    "EXACT_MATCH", "Exact Match", "success", "#15803d",
                    _aggregate_group([b]), _aggregate_group([p]),
                    "100% Match across GSTIN, Invoice Number, and Amounts.",
                    tolerance,
                ))
                break

    # -------------------------------------------------------------
    # PASS 2: 1-to-1 Normalized Match (Leading zeros / separators stripped)
    # -------------------------------------------------------------
    for b in records_books:
        if b["id"] in matched_books_ids or not b["gstin"]:
            continue
        for p in records_2b_by_gstin.get(b["gstin"], ()):
            if p["id"] in matched_2b_ids:
                continue
            if _inv_normalized_match(b, p) and amounts_match(b, p):
                mark_matched([b], [p])
                results.append(_finalize_result(
                    "NORMALIZED_MATCH", "Normalized Match", "warning", "#b45309",
                    _aggregate_group([b]), _aggregate_group([p]),
                    f"Matched after normalizing invoice formatting ('{b['raw_inv']}' vs '{p['raw_inv']}').",
                    tolerance,
                ))
                break

    # -------------------------------------------------------------
    # Helper to build remaining unmatched (gstin, clean_inv) groups
    # -------------------------------------------------------------
    def build_unmatched_groups(recs: List[Dict[str, Any]], matched_ids: set) -> List[Tuple[Tuple[str, str], List[Dict[str, Any]]]]:
        grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
        ordered_keys: List[Tuple[str, str]] = []
        for r in recs:
            if r["id"] in matched_ids or not r["gstin"] or not r["clean_inv"]:
                continue
            k = (r["gstin"], r["clean_inv"])
            if k not in grouped:
                ordered_keys.append(k)
            grouped[k].append(r)
        return [(k, grouped[k]) for k in ordered_keys]

    def _describe_consolidation(agg_b: Dict[str, Any], agg_p: Dict[str, Any]) -> str:
        b_slabs = f" ({', '.join(agg_b['slabs'])})" if agg_b.get("slabs") else ""
        p_slabs = f" ({', '.join(agg_p['slabs'])})" if agg_p.get("slabs") else ""
        b_part = f"{agg_b['line_count']} Books lines{b_slabs}" if agg_b["line_count"] > 1 else "1 Books line"
        p_part = f"{agg_p['line_count']} Portal lines{p_slabs}" if agg_p["line_count"] > 1 else "1 Portal line"
        return f"Multi-rate invoice consolidated: {b_part} vs {p_part}."

    # -------------------------------------------------------------
    # PASS 2.5A: Multi-Rate Consolidation — Exact Clean Invoice Match
    # -------------------------------------------------------------
    rem_b_groups = build_unmatched_groups(records_books, matched_books_ids)
    rem_2b_dict: Dict[Tuple[str, str], List[Dict[str, Any]]] = dict(
        build_unmatched_groups(records_2b, matched_2b_ids)
    )

    for k, b_list in rem_b_groups:
        p_list = rem_2b_dict.get(k)
        if not p_list or not (len(b_list) > 1 or len(p_list) > 1):
            continue
        if any(r["id"] in matched_books_ids for r in b_list) or any(r["id"] in matched_2b_ids for r in p_list):
            continue
        agg_b = _aggregate_group(b_list)
        agg_p = _aggregate_group(p_list)
        if amounts_match(agg_b, agg_p):
            mark_matched(b_list, p_list)
            results.append(_finalize_result(
                "EXACT_MATCH",
                "Exact Match (Multi-Rate)",
                "success",
                "#15803d",
                agg_b,
                agg_p,
                _describe_consolidation(agg_b, agg_p) + " 100% Match on total Taxable & Tax.",
                tolerance,
            ))

    # -------------------------------------------------------------
    # PASS 2.5B: Multi-Rate Consolidation — Normalized Invoice Match
    # -------------------------------------------------------------
    rem_b_groups = build_unmatched_groups(records_books, matched_books_ids)
    rem_2b_by_gstin: Dict[str, List[Tuple[Tuple[str, str], List[Dict[str, Any]]]]] = defaultdict(list)
    for k, p_list in build_unmatched_groups(records_2b, matched_2b_ids):
        rem_2b_by_gstin[k[0]].append((k, p_list))

    for (gstin, _), b_list in rem_b_groups:
        if any(r["id"] in matched_books_ids for r in b_list):
            continue
        agg_b = _aggregate_group(b_list)
        for (_, _), p_list in rem_2b_by_gstin.get(gstin, ()):
            if any(r["id"] in matched_2b_ids for r in p_list):
                continue
            if not (len(b_list) > 1 or len(p_list) > 1):
                continue
            agg_p = _aggregate_group(p_list)
            if _inv_normalized_match(agg_b, agg_p) and amounts_match(agg_b, agg_p):
                mark_matched(b_list, p_list)
                results.append(_finalize_result(
                    "NORMALIZED_MATCH",
                    "Normalized Match (Multi-Rate)",
                    "warning",
                    "#b45309",
                    agg_b,
                    agg_p,
                    _describe_consolidation(agg_b, agg_p)
                    + f" Format resolved ('{agg_b['raw_inv']}' vs '{agg_p['raw_inv']}').",
                    tolerance,
                ))
                break

    # -------------------------------------------------------------
    # PASS 3: Probable Typo Match (Strict Typo Check + Sequential Bill Guard)
    # -------------------------------------------------------------
    def build_typo_candidates(
        recs: List[Dict[str, Any]],
        matched_ids: set,
        matched_keys: set,
    ) -> List[Tuple[Dict[str, Any], List[Dict[str, Any]]]]:
        grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
        order_tokens: List[Tuple[str, Any]] = []

        for r in recs:
            if r["id"] in matched_ids or not r["gstin"]:
                continue
            k = (r["gstin"], r["clean_inv"])
            if r["clean_inv"] and k not in matched_keys:
                if k not in grouped:
                    order_tokens.append(("GRP", k))
                grouped[k].append(r)
            else:
                order_tokens.append(("ONE", r))

        candidates: List[Tuple[Dict[str, Any], List[Dict[str, Any]]]] = []
        for kind, ref in order_tokens:
            if kind == "ONE":
                candidates.append((_aggregate_group([ref]), [ref]))
            else:
                g = grouped[ref]
                distinct_slabs = {x.get("rate_label") for x in g if x.get("rate_label")}
                if len(g) > 1 and len(distinct_slabs) > 1:
                    candidates.append((_aggregate_group(g), g))
                else:
                    for item in g:
                        candidates.append((_aggregate_group([item]), [item]))
        return candidates

    b_typo_cands = build_typo_candidates(records_books, matched_books_ids, matched_books_keys)
    p_typo_cands = build_typo_candidates(records_2b, matched_2b_ids, matched_2b_keys)
    p_typo_by_gstin: Dict[str, List[Tuple[Dict[str, Any], List[Dict[str, Any]]]]] = defaultdict(list)
    for agg_p, p_list in p_typo_cands:
        p_typo_by_gstin[agg_p["gstin"]].append((agg_p, p_list))

    for agg_b, b_list in b_typo_cands:
        if any(r["id"] in matched_books_ids for r in b_list):
            continue
        for agg_p, p_list in p_typo_by_gstin.get(agg_b["gstin"], ()):
            if any(r["id"] in matched_2b_ids for r in p_list):
                continue
            if not amounts_match(agg_b, agg_p):
                continue
            if is_probable_typo(
                agg_b["clean_inv"],
                agg_p["clean_inv"],
                agg_b["core_digits"],
                agg_p["core_digits"],
            ) and not is_sequential_periodic_pair(
                agg_b["core_digits"],
                agg_p["core_digits"],
                agg_b["clean_inv"],
                agg_p["clean_inv"],
                agg_b.get("_date_obj"),
                agg_p.get("_date_obj"),
            ):
                mark_matched(b_list, p_list)
                prefix = (_describe_consolidation(agg_b, agg_p) + " ") if (agg_b["line_count"] > 1 or agg_p["line_count"] > 1) else ""
                results.append(_finalize_result(
                    "PROBABLE_TYPO",
                    "Probable Typo (Verify)",
                    "warning",
                    "#d97706",
                    agg_b,
                    agg_p,
                    f"{prefix}GSTIN & Amount match; Invoice No differ (Books: '{agg_b['raw_inv']}' vs Portal: '{agg_p['raw_inv']}').",
                    tolerance,
                ))
                break

    # -------------------------------------------------------------
    # PASS 4A: Multi-Rate Consolidated Value / Tax Mismatch
    # -------------------------------------------------------------
    rem_b_groups = build_unmatched_groups(records_books, matched_books_ids)
    rem_2b_groups_list = build_unmatched_groups(records_2b, matched_2b_ids)
    rem_2b_by_gstin = defaultdict(list)
    for k, p_list in rem_2b_groups_list:
        rem_2b_by_gstin[k[0]].append((k, p_list))

    for (gstin, b_clean), b_list in rem_b_groups:
        if any(r["id"] in matched_books_ids for r in b_list):
            continue
        agg_b = _aggregate_group(b_list)
        for (_, p_clean), p_list in rem_2b_by_gstin.get(gstin, ()):
            if any(r["id"] in matched_2b_ids for r in p_list):
                continue
            if not (len(b_list) > 1 or len(p_list) > 1):
                continue
            agg_p = _aggregate_group(p_list)
            inv_same = (b_clean == p_clean) or _inv_normalized_match(agg_b, agg_p)
            if inv_same:
                mark_matched(b_list, p_list)
                taxable_diff = round(agg_b["taxable"] - agg_p["taxable"], 2)
                tax_diff = round(agg_b["total_tax"] - agg_p["total_tax"], 2)
                results.append(_finalize_result(
                    "VALUE_MISMATCH",
                    "Value Mismatch (Multi-Rate)",
                    "orange",
                    "#ea580c",
                    agg_b,
                    agg_p,
                    _describe_consolidation(agg_b, agg_p)
                    + f" Value difference: Taxable diff ₹{taxable_diff}, Tax diff ₹{tax_diff}.",
                    tolerance,
                ))
                break

    # -------------------------------------------------------------
    # PASS 4B: 1-to-1 Value / Tax Discrepancy (GSTIN + Inv match, Amount differs)
    # -------------------------------------------------------------
    for b in records_books:
        if b["id"] in matched_books_ids or not b["gstin"]:
            continue
        for p in records_2b_by_gstin.get(b["gstin"], ()):
            if p["id"] in matched_2b_ids:
                continue
            if b["clean_inv"] and (b["clean_inv"] == p["clean_inv"] or _inv_normalized_match(b, p)):
                mark_matched([b], [p])
                taxable_diff = round(b["taxable"] - p["taxable"], 2)
                tax_diff = round(b["total_tax"] - p["total_tax"], 2)
                results.append(_finalize_result(
                    "VALUE_MISMATCH",
                    "Value Mismatch",
                    "orange",
                    "#ea580c",
                    _aggregate_group([b]),
                    _aggregate_group([p]),
                    f"Invoice match, but value difference: Taxable diff ₹{taxable_diff}, Tax diff ₹{tax_diff}.",
                    tolerance,
                ))
                break

    # -------------------------------------------------------------
    # PASS 5A: Missing in 2B (Books Only -> ITC at Risk, preserving Multi-Rate bills & Duplicate detection)
    # -------------------------------------------------------------
    unmatched_books_by_key: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    seen_books_keys: set = set()

    for b in records_books:
        if b["id"] in matched_books_ids:
            continue
        k = (b["gstin"], b["clean_inv"])
        if b["gstin"] and b["clean_inv"]:
            unmatched_books_by_key[k].append(b)

    for b in records_books:
        if b["id"] in matched_books_ids:
            continue
        k = (b["gstin"], b["clean_inv"])
        if not (b["gstin"] and b["clean_inv"]):
            results.append(_finalize_result(
                "MISSING_IN_2B",
                "Missing in 2B (ITC at Risk)",
                "danger",
                "#dc2626",
                _aggregate_group([b]),
                None,
                "Vendor has not filed GSTR-1. Ineligible for ITC claim until uploaded by supplier.",
                tolerance,
            ))
            continue

        group = unmatched_books_by_key[k]
        if k in matched_books_keys:
            results.append(_finalize_result(
                "MISSING_IN_2B",
                "Missing in 2B (ITC at Risk)",
                "danger",
                "#dc2626",
                _aggregate_group([b]),
                None,
                f"Possible duplicate booking in Books: Invoice '{b['raw_inv']}' already matched on another line; extra ₹{b['total_tax']:,.2f} tax is missing in 2B.",
                tolerance,
            ))
            continue

        if k in seen_books_keys:
            continue
        seen_books_keys.add(k)

        distinct_slabs = {x.get("rate_label") for x in group if x.get("rate_label")}
        distinct_taxables = {(x["taxable"], x["total_tax"]) for x in group}
        if len(group) > 1 and (len(distinct_slabs) > 1 or len(distinct_taxables) > 1):
            agg_b = _aggregate_group(group)
            slabs_txt = ", ".join(agg_b["slabs"]) if agg_b.get("slabs") else f"{agg_b['line_count']} lines"
            results.append(_finalize_result(
                "MISSING_IN_2B",
                "Missing in 2B (ITC at Risk)",
                "danger",
                "#dc2626",
                agg_b,
                None,
                f"Consolidated {agg_b['line_count']} Books rate lines ({slabs_txt}). Vendor has not filed GSTR-1. Ineligible for ITC claim until uploaded by supplier.",
                tolerance,
            ))
        else:
            for idx_dup, item in enumerate(group):
                remark = (
                    "Duplicate entry in Books (identical invoice number & amount repeated) and missing in 2B."
                    if idx_dup > 0
                    else "Vendor has not filed GSTR-1. Ineligible for ITC claim until uploaded by supplier."
                )
                results.append(_finalize_result(
                    "MISSING_IN_2B",
                    "Missing in 2B (ITC at Risk)",
                    "danger",
                    "#dc2626",
                    _aggregate_group([item]),
                    None,
                    remark,
                    tolerance,
                ))

    # -------------------------------------------------------------
    # PASS 5B: Missing in Books (2B Only -> Unbooked Purchases)
    # -------------------------------------------------------------
    unmatched_2b_by_key: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    seen_2b_keys: set = set()
    for p in records_2b:
        if p["id"] in matched_2b_ids:
            continue
        k = (p["gstin"], p["clean_inv"])
        if p["gstin"] and p["clean_inv"]:
            unmatched_2b_by_key[k].append(p)

    for p in records_2b:
        if p["id"] in matched_2b_ids:
            continue
        k = (p["gstin"], p["clean_inv"])
        if not (p["gstin"] and p["clean_inv"]):
            results.append(_finalize_result(
                "MISSING_IN_BOOKS",
                "Missing in Books (Unbooked)",
                "info",
                "#0284c7",
                None,
                _aggregate_group([p]),
                "Invoice present on GST portal but omitted from internal ERP/Tally.",
                tolerance,
            ))
            continue

        group = unmatched_2b_by_key[k]
        if k in matched_2b_keys:
            results.append(_finalize_result(
                "MISSING_IN_BOOKS",
                "Missing in Books (Unbooked)",
                "info",
                "#0284c7",
                None,
                _aggregate_group([p]),
                f"Extra unbooked Portal line for invoice '{p['raw_inv']}' (another line already matched in Books).",
                tolerance,
            ))
            continue

        if k in seen_2b_keys:
            continue
        seen_2b_keys.add(k)

        distinct_slabs = {x.get("rate_label") for x in group if x.get("rate_label")}
        distinct_taxables = {(x["taxable"], x["total_tax"]) for x in group}
        if len(group) > 1 and (len(distinct_slabs) > 1 or len(distinct_taxables) > 1):
            agg_p = _aggregate_group(group)
            slabs_txt = ", ".join(agg_p["slabs"]) if agg_p.get("slabs") else f"{agg_p['line_count']} lines"
            results.append(_finalize_result(
                "MISSING_IN_BOOKS",
                "Missing in Books (Unbooked)",
                "info",
                "#0284c7",
                None,
                agg_p,
                f"Consolidated {agg_p['line_count']} Portal rate lines ({slabs_txt}) omitted from internal ERP/Tally.",
                tolerance,
            ))
        else:
            for item in group:
                results.append(_finalize_result(
                    "MISSING_IN_BOOKS",
                    "Missing in Books (Unbooked)",
                    "info",
                    "#0284c7",
                    None,
                    _aggregate_group([item]),
                    "Invoice present on GST portal but omitted from internal ERP/Tally.",
                    tolerance,
                ))

    # -------------------------------------------------------------
    # Calculate Executive Summary KPI Metrics
    # -------------------------------------------------------------
    raw_books_rows = len(records_books)
    raw_2b_rows = len(records_2b)
    total_books_invoices = sum(1 for r in results if r["books"] is not None)
    total_2b_invoices = sum(1 for r in results if r["portal"] is not None)

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

    multi_rate_matched_count = sum(
        1 for r in results
        if r["status"] in ("EXACT_MATCH", "NORMALIZED_MATCH", "PROBABLE_TYPO")
        and (r.get("books_line_count", 0) > 1 or r.get("portal_line_count", 0) > 1)
    )
    tax_head_mismatch_count = sum(1 for r in results if r.get("tax_head_mismatch"))

    gross_matched_tax = sum(
        r["books"]["total_tax"] for r in results
        if r["status"] in ("EXACT_MATCH", "NORMALIZED_MATCH") and r["books"]
    )
    ineligible_matched_tax = sum(
        r["books"]["total_tax"] for r in results
        if r["status"] in ("EXACT_MATCH", "NORMALIZED_MATCH") and r["books"] and r.get("itc_ineligible")
    )
    eligible_matched_tax = round(gross_matched_tax - ineligible_matched_tax, 2)

    ineligible_count = sum(1 for r in results if r.get("itc_ineligible"))
    ineligible_tax = round(sum(
        (r["books"] or r["portal"])["total_tax"]
        for r in results
        if r.get("itc_ineligible") and (r["books"] or r["portal"])
    ), 2)

    rcm_count = sum(1 for r in results if r.get("is_rcm"))
    rcm_tax = round(sum(
        (r["books"] or r["portal"])["total_tax"]
        for r in results
        if r.get("is_rcm") and (r["books"] or r["portal"])
    ), 2)

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
        "raw_books_rows": raw_books_rows,
        "raw_2b_rows": raw_2b_rows,
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
        "multi_rate_matched_count": multi_rate_matched_count,
        "tax_head_mismatch_count": tax_head_mismatch_count,
        "ineligible_count": ineligible_count,
        "ineligible_tax": ineligible_tax,
        "rcm_count": rcm_count,
        "rcm_tax": rcm_tax,
        "gross_matched_tax": round(gross_matched_tax, 2),
        "matched_tax": eligible_matched_tax,
        "itc_at_risk_tax": round(itc_at_risk_tax, 2),
        "unbooked_tax": round(unbooked_tax, 2),
        "mismatch_variance_tax": round(mismatch_variance_tax, 2),
        "reconciliation_rate": reconciliation_rate,
        "tolerance": tolerance,
    }

    kpis = {
        "total_tax_books": summary["books_tax_total"],
        "total_invoices_books": summary["total_books_invoices"],
        "raw_books_rows": summary["raw_books_rows"],
        "total_tax_2b": summary["portal_tax_total"],
        "total_invoices_2b": summary["total_2b_invoices"],
        "raw_2b_rows": summary["raw_2b_rows"],
        "matched_itc": summary["matched_tax"],
        "gross_matched_itc": summary["gross_matched_tax"],
        "matched_count": summary["matched_count"],
        "multi_rate_matched_count": summary["multi_rate_matched_count"],
        "ineligible_count": summary["ineligible_count"],
        "ineligible_itc": summary["ineligible_tax"],
        "rcm_count": summary["rcm_count"],
        "rcm_tax": summary["rcm_tax"],
        "tax_head_mismatch_count": summary["tax_head_mismatch_count"],
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
            "MISSING_IN_BOOKS": summary["missing_in_books_count"],
        },
    }

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
            "books_line_count": r.get("books_line_count", 0),
            "portal_line_count": r.get("portal_line_count", 0),
            "books_slabs": r.get("books_slabs", ""),
            "portal_slabs": r.get("portal_slabs", ""),
            "itc_ineligible": r.get("itc_ineligible", False),
            "is_rcm": r.get("is_rcm", False),
            "tax_head_mismatch": r.get("tax_head_mismatch", False),
            "tax_diff": r.get("tax_diff", 0.0),
            "taxable_diff": r.get("taxable_diff", 0.0),
            "audit_remark": r.get("remarks", ""),
        })

    payload = {
        "summary": summary,
        "kpis": kpis,
        "ledger": ledger,
    }
    if include_results:
        payload["results"] = results
    return payload
