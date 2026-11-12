"""
GSTR-9 Annual Return Computation Engine (Tables 4 - 18) & GSTR-9C Audit Bridge.
Integrates existing multi-tier GSTR-2B vs Purchase Register reconciliation
with annual Outward (Sales vs GSTR-1 vs GSTR-3B), Rule-based ITC reversals
(Sec 17(5), Rule 37, Rule 42/43), Rule 88A tax utilization, and HSN validation.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
import pandas as pd

from app.engine.reconciler import run_reconciliation
from app.engine.gstr9_parsers import (
    empty_gstr3b_structure,
    extract_purchase_rule_flags,
    parse_sales_register_df,
)


def _r2(val: float) -> float:
    return round(float(val or 0.0), 2)


def _zero_tax() -> Dict[str, float]:
    return {"taxable": 0.0, "igst": 0.0, "cgst": 0.0, "sgst": 0.0, "cess": 0.0}


def _zero_heads() -> Dict[str, float]:
    return {"igst": 0.0, "cgst": 0.0, "sgst": 0.0, "cess": 0.0}


def _sum_heads(d: Dict[str, float]) -> float:
    return _r2(d.get("igst", 0.0) + d.get("cgst", 0.0) + d.get("sgst", 0.0) + d.get("cess", 0.0))


def _sum_df_bucket(df: pd.DataFrame, categories: Tuple[str, ...], period: str = "IN_FY", abs_sign: bool = False) -> Dict[str, float]:
    res = _zero_tax()
    if df is None or df.empty:
        return res
    mask = df["category"].isin(categories)
    if period != "ALL":
        mask = mask & (df["period_bucket"] == period)
    sub = df[mask]
    if sub.empty:
        return res
    for col in ("taxable", "igst", "cgst", "sgst", "cess"):
        v = float(sub[col].sum())
        res[col] = _r2(abs(v) if abs_sign else v)
    return res


def _build_hsn_table(
    df: pd.DataFrame,
    turnover_above_5cr: bool,
    side: str = "Outward",
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """
    Aggregate HSN/SAC summary and validate digit lengths (4 vs 6 digits), UQC, and rate accuracy.
    Returns (rows_list, validation_stats).
    """
    if df is None or df.empty or "hsn" not in df.columns:
        return [], {"total_rows": 0, "error_count": 0, "digit_errors": 0, "rate_errors": 0, "uqc_errors": 0}

    work = df.copy()
    work["hsn"] = work["hsn"].fillna("").astype(str).str.strip()
    work["uqc"] = work["uqc"].fillna("").astype(str).str.strip() if "uqc" in work.columns else "NOS"
    work = work[(work["taxable"].abs() > 0) | (work["hsn"] != "")]
    if work.empty:
        return [], {"total_rows": 0, "error_count": 0, "digit_errors": 0, "rate_errors": 0, "uqc_errors": 0}

    required_digits = 6 if turnover_above_5cr else 4
    grouped = (
        work.groupby(["hsn", "uqc", "rate"], dropna=False)
        .agg(
            qty=("qty", "sum"),
            taxable=("taxable", "sum"),
            igst=("igst", "sum"),
            cgst=("cgst", "sum"),
            sgst=("sgst", "sum"),
            cess=("cess", "sum"),
            has_b2b=("is_b2b", "max"),
        )
        .reset_index()
    )

    rows: List[Dict[str, Any]] = []
    digit_errors = 0
    rate_errors = 0
    uqc_errors = 0

    for _, r in grouped.iterrows():
        hsn = str(r["hsn"] or "").strip()
        uqc = str(r["uqc"] or "").strip()
        qty = _r2(r["qty"])
        rate = _r2(r["rate"])
        taxable = _r2(r["taxable"])
        igst = _r2(r["igst"])
        cgst = _r2(r["cgst"])
        sgst = _r2(r["sgst"])
        cess = _r2(r["cess"])
        is_b2b = bool(r.get("has_b2b", False))

        issues: List[str] = []
        if not hsn or not hsn.isdigit():
            issues.append("Missing/Non-numeric HSN")
            digit_errors += 1
        elif (is_b2b or side == "Inward") and len(hsn) < required_digits:
            issues.append(f"Need >= {required_digits}-digit HSN (found {len(hsn)})")
            digit_errors += 1

        if not hsn.startswith("99") and (not uqc or uqc == "-"):
            issues.append("Missing UQC")
            uqc_errors += 1

        total_gst = igst + cgst + sgst
        total_tax_all = _r2(total_gst + cess)
        eff_rate = round((abs(total_gst) / abs(taxable)) * 100.0, 2) if abs(taxable) > 0 else 0.0
        if abs(taxable) > 10.0 and rate > 0:
            expected_tax = abs(taxable) * (rate / 100.0)
            if abs(abs(total_gst) - expected_tax) > max(2.0, expected_tax * 0.02):
                issues.append(f"Tax/Taxable effective rate {eff_rate}% != {rate}%")
                rate_errors += 1

        rows.append({
            "hsn": hsn or "MISSING",
            "uqc": uqc or "OTH",
            "qty": qty,
            "rate": rate,
            "taxable": taxable,
            "igst": igst,
            "cgst": cgst,
            "sgst": sgst,
            "cess": cess,
            "total_tax": total_tax_all,
            "effective_rate": eff_rate,
            "status": "VALID" if not issues else "WARNING",
            "issues": "; ".join(issues) if issues else "Compliant",
        })

    rows.sort(key=lambda x: (0 if x["status"] == "WARNING" else 1, -abs(x["taxable"])))
    stats = {
        "total_rows": len(rows),
        "error_count": sum(1 for x in rows if x["status"] == "WARNING"),
        "digit_errors": digit_errors,
        "rate_errors": rate_errors,
        "uqc_errors": uqc_errors,
        "required_digits": required_digits,
    }
    return rows, stats


def compute_gstr9_return(
    fy: str = "2025-26",
    turnover_above_5cr: bool = True,
    cutoff_month: int = 11,
    sales_df: Optional[pd.DataFrame] = None,
    gstr1_df: Optional[pd.DataFrame] = None,
    gstr1_hsn_df: Optional[pd.DataFrame] = None,
    gstr3b_data: Optional[Dict[str, Any]] = None,
    raw_books_purchase_df: Optional[pd.DataFrame] = None,
    raw_2b_df: Optional[pd.DataFrame] = None,
    manual_overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Execute end-to-end GSTR-9 Annual Return computation across Tables 4-18.
    """
    overrides = manual_overrides if isinstance(manual_overrides, dict) else {}
    gstr3b = gstr3b_data or empty_gstr3b_structure()

    # Ensure sales_df & gstr1_df are normalized if raw frames were passed
    s_df = sales_df if (sales_df is not None and "period_bucket" in sales_df.columns) else parse_sales_register_df(sales_df, fy=fy, cutoff_month=cutoff_month)
    g1_df = gstr1_df if (gstr1_df is not None and "period_bucket" in gstr1_df.columns) else parse_sales_register_df(gstr1_df, fy=fy, cutoff_month=cutoff_month)

    # ------------------------------------------------------------------
    # 1. OUTWARD RECONCILIATION (TABLES 4 & 5 + NEXT-FY CUTOFF TABLES 10 & 11)
    # ------------------------------------------------------------------
    # Books Outward buckets (In FY)
    bk_b2c = _sum_df_bucket(s_df, ("B2C",), "IN_FY")
    bk_b2b = _sum_df_bucket(s_df, ("B2B",), "IN_FY")
    bk_exp_wp = _sum_df_bucket(s_df, ("EXPORT_WP",), "IN_FY")
    bk_sez_wp = _sum_df_bucket(s_df, ("SEZ_WP",), "IN_FY")
    bk_deemed = _sum_df_bucket(s_df, ("DEEMED_EXP",), "IN_FY")
    bk_cn = _sum_df_bucket(s_df, ("CREDIT_NOTE",), "IN_FY", abs_sign=True)
    bk_dn = _sum_df_bucket(s_df, ("DEBIT_NOTE",), "IN_FY", abs_sign=True)

    bk_exp_wop = _sum_df_bucket(s_df, ("EXPORT_WOP",), "IN_FY")
    bk_sez_wop = _sum_df_bucket(s_df, ("SEZ_WOP",), "IN_FY")
    bk_exempt = _sum_df_bucket(s_df, ("EXEMPT",), "IN_FY")
    bk_nil = _sum_df_bucket(s_df, ("NIL",), "IN_FY")
    bk_nongst = _sum_df_bucket(s_df, ("NON_GST",), "IN_FY")

    # GSTR-1 Outward buckets (In FY)
    g1_b2c = _sum_df_bucket(g1_df, ("B2C",), "IN_FY")
    g1_b2b = _sum_df_bucket(g1_df, ("B2B",), "IN_FY")
    g1_exp_wp = _sum_df_bucket(g1_df, ("EXPORT_WP",), "IN_FY")
    g1_sez_wp = _sum_df_bucket(g1_df, ("SEZ_WP",), "IN_FY")
    g1_deemed = _sum_df_bucket(g1_df, ("DEEMED_EXP",), "IN_FY")
    g1_cn = _sum_df_bucket(g1_df, ("CREDIT_NOTE",), "IN_FY", abs_sign=True)
    g1_dn = _sum_df_bucket(g1_df, ("DEBIT_NOTE",), "IN_FY", abs_sign=True)

    g1_exp_wop = _sum_df_bucket(g1_df, ("EXPORT_WOP",), "IN_FY")
    g1_sez_wop = _sum_df_bucket(g1_df, ("SEZ_WOP",), "IN_FY")
    g1_exempt = _sum_df_bucket(g1_df, ("EXEMPT",), "IN_FY")
    g1_nil = _sum_df_bucket(g1_df, ("NIL",), "IN_FY")
    g1_nongst = _sum_df_bucket(g1_df, ("NON_GST",), "IN_FY")

    # Next-FY Cutoff window (Apr-Nov) for Table 10 (+) & Table 11 (-)
    cut_src = g1_df if (g1_df is not None and not g1_df.empty) else s_df
    t10_pos = _sum_df_bucket(cut_src, ("B2B", "B2C", "EXPORT_WP", "SEZ_WP", "DEEMED_EXP", "DEBIT_NOTE"), "NEXT_FY_CUTOFF")
    t11_neg = _sum_df_bucket(cut_src, ("CREDIT_NOTE",), "NEXT_FY_CUTOFF", abs_sign=True)

    # Apply explicit manual Table 10/11 overrides if supplied
    if isinstance(overrides.get("table_10"), dict):
        for k in t10_pos:
            if k in overrides["table_10"]:
                t10_pos[k] = _r2(overrides["table_10"][k])
    if isinstance(overrides.get("table_11"), dict):
        for k in t11_neg:
            if k in overrides["table_11"]:
                t11_neg[k] = _r2(overrides["table_11"][k])

    # ------------------------------------------------------------------
    # 2. INWARD RECONCILIATION + RULE FLAGS (TABLES 6, 7, 8 & 12, 13)
    # ------------------------------------------------------------------
    recon_res: Optional[Dict[str, Any]] = None
    if raw_books_purchase_df is not None and not raw_books_purchase_df.empty and raw_2b_df is not None and not raw_2b_df.empty:
        recon_res = run_reconciliation(raw_2b_df, raw_books_purchase_df, tolerance=1.00, include_results=True)

    pur_flags_df = extract_purchase_rule_flags(raw_books_purchase_df, fy=fy, cutoff_month=cutoff_month)

    # Inward RCM tax from Purchase Register / 3B
    pur_rcm = _zero_tax()
    if not pur_flags_df.empty:
        rcm_sub = pur_flags_df[pur_flags_df["is_rcm"] & (pur_flags_df["period_bucket"] == "IN_FY")]
        if not rcm_sub.empty:
            for col in ("taxable", "igst", "cgst", "sgst", "cess"):
                pur_rcm[col] = _r2(rcm_sub[col].sum())

    g3b_out = gstr3b["outward_3_1"]
    g3b_rcm = g3b_out["d_inward_rcm"]

    # Build Table 4 rows (4A..4N)
    def _net_outward(b2c, b2b, exp_wp, sez_wp, deemed, rcm, cn, dn):
        out = _zero_tax()
        for k in out:
            out[k] = _r2(b2c[k] + b2b[k] + exp_wp[k] + sez_wp[k] + deemed[k] + rcm[k] - cn[k] + dn[k])
        return out

    bk_4n = _net_outward(bk_b2c, bk_b2b, bk_exp_wp, bk_sez_wp, bk_deemed, pur_rcm, bk_cn, bk_dn)
    g1_4n = _net_outward(g1_b2c, g1_b2b, g1_exp_wp, g1_sez_wp, g1_deemed, g3b_rcm, g1_cn, g1_dn)
    g3b_4n = _zero_tax()
    for k in g3b_4n:
        g3b_4n[k] = _r2(g3b_out["a_taxable"][k] + g3b_out["b_zero_rated"][k] + g3b_out["d_inward_rcm"][k])

    # Fallback: if GSTR-1 wasn't uploaded separately, show 0 in GSTR-1 column so variance alerts user;
    # if Sales Register was uploaded without GSTR-1, Books drives Table 4 recommendation.
    raw_t4 = [
        ("4A", "Supplies made to Un-registered persons (B2C)", bk_b2c, g1_b2c, g1_b2c if not g1_df.empty else bk_b2c, False),
        ("4B", "Supplies made to Registered persons (B2B)", bk_b2b, g1_b2b, g1_b2b if not g1_df.empty else bk_b2b, False),
        ("4C", "Zero rated supply (Export) on payment of tax", bk_exp_wp, g1_exp_wp, g1_exp_wp if not g1_df.empty else bk_exp_wp, False),
        ("4D", "Supply to SEZs on payment of tax", bk_sez_wp, g1_sez_wp, g1_sez_wp if not g1_df.empty else bk_sez_wp, False),
        ("4E", "Deemed Exports", bk_deemed, g1_deemed, g1_deemed if not g1_df.empty else bk_deemed, False),
        ("4G", "Inward supplies liable to Reverse Charge (RCM)", pur_rcm, g3b_rcm, g3b_rcm if _sum_heads(g3b_rcm) > 0 else pur_rcm, False),
        ("4I", "Less: Credit Notes issued in respect of B2B/Exports (-)", bk_cn, g1_cn, g1_cn if not g1_df.empty else bk_cn, False),
        ("4J", "Add: Debit Notes issued in respect of above (+)", bk_dn, g1_dn, g1_dn if not g1_df.empty else bk_dn, False),
        ("4N", "Total Supplies on which tax is payable (Table 4N)", bk_4n, g1_4n, g1_4n if not g1_df.empty else bk_4n, True),
    ]
    table_4_rows = []
    for code, desc, bk_d, g1_d, g9_d, is_tot in raw_t4:
        table_4_rows.append({
            "code": code,
            "desc": desc,
            "books": bk_d,
            "gstr1": g1_d,
            "gstr9": g9_d,
            "books_taxable": bk_d["taxable"],
            "books_tax": _sum_heads(bk_d),
            "gstr1_taxable": g1_d["taxable"],
            "gstr1_tax": _sum_heads(g1_d),
            "gstr9_taxable": g9_d["taxable"],
            "gstr9_tax": _sum_heads(g9_d),
            "variance_books_vs_gstr1": _r2(bk_d["taxable"] - g1_d["taxable"]),
            "is_total": is_tot,
        })

    # Table 5 (Exempt / Nil / Non-GST & Total Turnover 5N)
    bk_5m = _r2(bk_exp_wop["taxable"] + bk_sez_wop["taxable"] + bk_exempt["taxable"] + bk_nil["taxable"] + bk_nongst["taxable"])
    g1_5m = _r2(g1_exp_wop["taxable"] + g1_sez_wop["taxable"] + g1_exempt["taxable"] + g1_nil["taxable"] + g1_nongst["taxable"])
    g3b_5m = _r2(g3b_out["c_nil_exempt"]["taxable"] + g3b_out["e_non_gst"]["taxable"])

    # Outward Turnover excludes 4G inward RCM
    bk_5n_turnover = _r2((bk_4n["taxable"] - pur_rcm["taxable"]) + bk_5m)
    g1_5n_turnover = _r2((g1_4n["taxable"] - g3b_rcm["taxable"]) + g1_5m)
    g3b_5n_turnover = _r2(g3b_out["a_taxable"]["taxable"] + g3b_out["b_zero_rated"]["taxable"] + g3b_5m)
    exempt_total_turnover = _r2(bk_exempt["taxable"] + bk_nil["taxable"] + bk_nongst["taxable"])

    table_5_rows = [
        {"code": "5A", "desc": "Zero rated supply (Export) without payment of tax", "books_taxable": bk_exp_wop["taxable"], "gstr1_taxable": g1_exp_wop["taxable"], "gstr3b_taxable": 0.0},
        {"code": "5B", "desc": "Supply to SEZs without payment of tax", "books_taxable": bk_sez_wop["taxable"], "gstr1_taxable": g1_sez_wop["taxable"], "gstr3b_taxable": 0.0},
        {"code": "5D", "desc": "Exempted Supply", "books_taxable": bk_exempt["taxable"], "gstr1_taxable": g1_exempt["taxable"], "gstr3b_taxable": g3b_out["c_nil_exempt"]["taxable"]},
        {"code": "5E", "desc": "Nil Rated Supply", "books_taxable": bk_nil["taxable"], "gstr1_taxable": g1_nil["taxable"], "gstr3b_taxable": 0.0},
        {"code": "5F", "desc": "Non-GST Supply", "books_taxable": bk_nongst["taxable"], "gstr1_taxable": g1_nongst["taxable"], "gstr3b_taxable": g3b_out["e_non_gst"]["taxable"]},
        {"code": "5M", "desc": "Sub-total Non-Taxable Turnover (5A to 5F)", "books_taxable": bk_5m, "gstr1_taxable": g1_5m, "gstr3b_taxable": g3b_5m, "is_subtotal": True},
        {"code": "5N", "desc": "Total Turnover incl. Advances [(4N - 4G) + 5M]", "books_taxable": bk_5n_turnover, "gstr1_taxable": g1_5n_turnover, "gstr3b_taxable": g3b_5n_turnover, "is_total": True},
    ]

    # Outward 3-way tie-out comparison
    g1_out_tax_total = _sum_heads(g1_4n) - _sum_heads(g3b_rcm)
    bk_out_tax_total = _sum_heads(bk_4n) - _sum_heads(pur_rcm)
    g3b_out_tax_total = _sum_heads(g3b_out["a_taxable"]) + _sum_heads(g3b_out["b_zero_rated"])
    turnover_variance = _r2(bk_5n_turnover - (g1_5n_turnover if not g1_df.empty else g3b_5n_turnover))
    outward_tax_variance = _r2(bk_out_tax_total - (g3b_out_tax_total if g3b_out_tax_total > 0 else g1_out_tax_total))

    # ------------------------------------------------------------------
    # 3. ITC BRIDGE (TABLES 6, 7 & 8)
    # ------------------------------------------------------------------
    g3b_itc = gstr3b["itc_4"]

    # Calculate component breakdown from Purchase Register rules
    bk_inputs_itc = _zero_heads()
    bk_capex_itc = _zero_heads()
    bk_services_itc = _zero_heads()
    bk_rcm_itc = _zero_heads()
    bk_import_itc = _zero_heads()
    bk_sec17_5 = _zero_heads()
    bk_personal = _zero_heads()
    bk_rule37 = _zero_heads()
    bk_common_credit = _zero_heads()
    bk_next_fy_claimed = _zero_heads()
    bk_time_barred = _zero_heads()

    reversals_schedule: List[Dict[str, Any]] = []

    if not pur_flags_df.empty:
        for _, pr in pur_flags_df.iterrows():
            heads = {h: float(pr[h]) for h in ("igst", "cgst", "sgst", "cess")}
            tot = _sum_heads(heads)
            if tot == 0:
                continue

            if pr["period_bucket"] == "TIME_BARRED":
                for h in heads:
                    bk_time_barred[h] = _r2(bk_time_barred[h] + heads[h])
                reversals_schedule.append({
                    "rule": "Sec 16(4) Time-Barred",
                    "party": pr["party_name"],
                    "gstin": pr["gstin"],
                    "inv_no": pr["inv_no"],
                    "doc_date": pr["doc_date"],
                    "taxable": pr["taxable"],
                    "tax_amount": tot,
                    "reason": "Reported / claimed past Sec 16(4) annual cut-off window.",
                })
                continue

            if pr["period_bucket"] == "NEXT_FY_CUTOFF":
                for h in heads:
                    bk_next_fy_claimed[h] = _r2(bk_next_fy_claimed[h] + heads[h])

            if pr["is_personal"]:
                for h in heads:
                    bk_personal[h] = _r2(bk_personal[h] + heads[h])
                reversals_schedule.append({
                    "rule": "Table 7E - Personal / Non-Business (Sec 17(1))",
                    "party": pr["party_name"],
                    "gstin": pr["gstin"],
                    "inv_no": pr["inv_no"],
                    "doc_date": pr["doc_date"],
                    "taxable": pr["taxable"],
                    "tax_amount": tot,
                    "reason": pr["nature_remarks"] or "Non-business / personal consumption expense.",
                })
                continue

            if pr["is_sec_17_5"]:
                for h in heads:
                    bk_sec17_5[h] = _r2(bk_sec17_5[h] + heads[h])
                reversals_schedule.append({
                    "rule": "Table 7E - Blocked Credit (Sec 17(5))",
                    "party": pr["party_name"],
                    "gstin": pr["gstin"],
                    "inv_no": pr["inv_no"],
                    "doc_date": pr["doc_date"],
                    "taxable": pr["taxable"],
                    "tax_amount": tot,
                    "reason": pr["nature_remarks"] or "Ineligible under Sec 17(5).",
                })
                continue

            if pr["is_rule_37"]:
                for h in heads:
                    bk_rule37[h] = _r2(bk_rule37[h] + heads[h])
                reversals_schedule.append({
                    "rule": "Table 7A - Rule 37 (>180 Days Unpaid)",
                    "party": pr["party_name"],
                    "gstin": pr["gstin"],
                    "inv_no": pr["inv_no"],
                    "doc_date": pr["doc_date"],
                    "taxable": pr["taxable"],
                    "tax_amount": tot,
                    "reason": f"Creditor unpaid for {pr['unpaid_days'] or '>180'} days; ITC reversible under Rule 37.",
                })

            if pr["is_common_credit"]:
                for h in heads:
                    bk_common_credit[h] = _r2(bk_common_credit[h] + heads[h])

            if pr["period_bucket"] == "IN_FY":
                if pr["is_rcm"]:
                    for h in heads:
                        bk_rcm_itc[h] = _r2(bk_rcm_itc[h] + heads[h])
                elif pr["itc_class"] == "IMPORT":
                    for h in heads:
                        bk_import_itc[h] = _r2(bk_import_itc[h] + heads[h])
                elif pr["itc_class"] == "CAPITAL_GOODS":
                    for h in heads:
                        bk_capex_itc[h] = _r2(bk_capex_itc[h] + heads[h])
                elif pr["itc_class"] == "INPUT_SERVICES":
                    for h in heads:
                        bk_services_itc[h] = _r2(bk_services_itc[h] + heads[h])
                else:
                    for h in heads:
                        bk_inputs_itc[h] = _r2(bk_inputs_itc[h] + heads[h])

    # Determine Table 6 figures: prefer 3B when present, enriched by Books classification split
    t6_4a5_3b = g3b_itc["4A_5_all_other"]
    bk_6b_sum = {h: _r2(bk_inputs_itc[h] + bk_capex_itc[h] + bk_services_itc[h]) for h in ("igst", "cgst", "sgst", "cess")}
    use_3b_itc = _sum_heads(t6_4a5_3b) > 0

    t6_6b = dict(t6_4a5_3b) if use_3b_itc else bk_6b_sum
    t6_6c_d = dict(g3b_itc["4A_3_rcm"]) if _sum_heads(g3b_itc["4A_3_rcm"]) > 0 else bk_rcm_itc
    t6_6e = dict(g3b_itc["4A_1_import_goods"]) if _sum_heads(g3b_itc["4A_1_import_goods"]) > 0 else bk_import_itc
    t6_6f = dict(g3b_itc["4A_2_import_services"])
    t6_6g = dict(g3b_itc["4A_4_isd"])
    t6_6h = {
        h: _r2(float((overrides.get("reclaimed_rule37") or {}).get(h, 0.0)))
        for h in ("igst", "cgst", "sgst", "cess")
    }
    t6_6a = {
        h: _r2(t6_6b[h] + t6_6c_d[h] + t6_6e[h] + t6_6f[h] + t6_6g[h])
        for h in ("igst", "cgst", "sgst", "cess")
    }
    t6_6o = {h: _r2(t6_6a[h] + t6_6h[h]) for h in ("igst", "cgst", "sgst", "cess")}

    # Rule 42 calculation: Exempt / Total Turnover * Common ITC Pool (auto or override)
    rule42_ratio = (exempt_total_turnover / bk_5n_turnover) if bk_5n_turnover > 0 and exempt_total_turnover > 0 else 0.0
    if "rule42_exempt_ratio" in overrides and overrides["rule42_exempt_ratio"] not in (None, ""):
        rule42_ratio = max(0.0, min(1.0, float(overrides["rule42_exempt_ratio"])))

    common_pool = bk_common_credit if _sum_heads(bk_common_credit) > 0 else t6_6b
    auto_rule42 = {h: _r2(common_pool[h] * rule42_ratio) for h in ("igst", "cgst", "sgst", "cess")}
    # Prefer 3B 4B(1) if higher or manual override supplied; else auto_rule42
    manual_r42 = overrides.get("rule42") or {}
    t7_7c_rule42 = {
        h: _r2(manual_r42[h]) if h in manual_r42 else (_r2(g3b_itc["4B_1_rule_42_43"][h]) if _sum_heads(g3b_itc["4B_1_rule_42_43"]) > 0 else auto_rule42[h])
        for h in ("igst", "cgst", "sgst", "cess")
    }
    if _sum_heads(t7_7c_rule42) > 0 and exempt_total_turnover > 0:
        reversals_schedule.append({
            "rule": "Table 7C - Rule 42 Common Credit Reversal",
            "party": "Computed Common ITC Pool",
            "gstin": "-",
            "inv_no": f"Exempt Ratio {round(rule42_ratio * 100, 2)}%",
            "doc_date": fy,
            "taxable": exempt_total_turnover,
            "tax_amount": _sum_heads(t7_7c_rule42),
            "reason": f"Exempt Turnover Rs.{exempt_total_turnover:,.2f} / Total Rs.{bk_5n_turnover:,.2f} x Common ITC Rs.{_sum_heads(common_pool):,.2f}",
        })

    manual_r37 = overrides.get("rule37") or {}
    t7_7a_rule37 = {
        h: _r2(manual_r37[h]) if h in manual_r37 else bk_rule37[h]
        for h in ("igst", "cgst", "sgst", "cess")
    }
    manual_r43 = overrides.get("rule43") or {}
    t7_7d_rule43 = {
        h: _r2(manual_r43[h]) if h in manual_r43 else 0.0
        for h in ("igst", "cgst", "sgst", "cess")
    }

    # Combine Books Sec 17(5) + Personal Sec 17(1) + 2B Ineligible items (avoid double count via reconciler)
    t7_7e_sec17_5 = {
        h: _r2(bk_sec17_5[h] + bk_personal[h])
        for h in ("igst", "cgst", "sgst", "cess")
    }
    if _sum_heads(t7_7e_sec17_5) == 0 and _sum_heads(g3b_itc["4D_1_sec_17_5"]) > 0:
        t7_7e_sec17_5 = dict(g3b_itc["4D_1_sec_17_5"])

    t7_7h_other = dict(g3b_itc["4B_2_other_reversals"])
    t7_7i_total = {
        h: _r2(t7_7a_rule37[h] + t7_7c_rule42[h] + t7_7d_rule43[h] + t7_7e_sec17_5[h] + t7_7h_other[h])
        for h in ("igst", "cgst", "sgst", "cess")
    }
    t7_7j_net = {h: _r2(t6_6o[h] - t7_7i_total[h]) for h in ("igst", "cgst", "sgst", "cess")}

    # Table 8: GSTR-2B 8A auto-populated comparison
    t8_8a_2b = _zero_heads()
    t8_8e_unbooked = _zero_heads()
    t8_8f_inelig_2b = _zero_heads()
    table_8d_drilldown: List[Dict[str, Any]] = []

    if raw_2b_df is not None and not raw_2b_df.empty:
        # Derive exact component heads from consolidated 2B B2B (non-RCM)
        p_flags = extract_purchase_rule_flags(raw_2b_df, fy=fy, cutoff_month=cutoff_month)
        if not p_flags.empty:
            in_2b_fy = p_flags[p_flags["period_bucket"].isin(["IN_FY", "NEXT_FY_CUTOFF"])]
            for _, r2b in in_2b_fy.iterrows():
                if not r2b["is_rcm"]:
                    for h in ("igst", "cgst", "sgst", "cess"):
                        t8_8a_2b[h] = _r2(t8_8a_2b[h] + float(r2b[h]))
                if r2b["is_ineligible"] or r2b["is_personal"]:
                    for h in ("igst", "cgst", "sgst", "cess"):
                        t8_8f_inelig_2b[h] = _r2(t8_8f_inelig_2b[h] + float(r2b[h]))

    if recon_res:
        for row in recon_res.get("results", []):
            st = row["status"]
            if st == "MISSING_IN_BOOKS" and row.get("portal"):
                p = row["portal"]
                # Proportionate split from portal row
                tot_p = float(p.get("total_tax", 0.0))
                pos_val = ""
                if raw_2b_df is not None and not raw_2b_df.empty and "Place of supply" in raw_2b_df.columns:
                    raw_pos = raw_2b_df.iloc[0].get("Place of supply")
                    pos_val = "" if raw_pos is None or (isinstance(raw_pos, float) and raw_pos != raw_pos) else str(raw_pos)
                if str(p.get("gstin") or "")[:2] == pos_val[:2] and len(pos_val) >= 2:
                    t8_8e_unbooked["cgst"] = _r2(t8_8e_unbooked["cgst"] + tot_p / 2)
                    t8_8e_unbooked["sgst"] = _r2(t8_8e_unbooked["sgst"] + tot_p / 2)
                else:
                    t8_8e_unbooked["igst"] = _r2(t8_8e_unbooked["igst"] + tot_p)
                table_8d_drilldown.append({
                    "category": "Table 8E - Available in 2B, Not Booked",
                    "party": p.get("party_name", ""),
                    "gstin": p.get("gstin", ""),
                    "inv_no": p.get("raw_inv", ""),
                    "books_tax": 0.0,
                    "portal_tax": tot_p,
                    "diff_tax": _r2(-tot_p),
                    "action": "Book missing vendor invoice or confirm lapsed in Table 8E.",
                })
            elif st == "MISSING_IN_2B" and row.get("books"):
                b = row["books"]
                tot_b = float(b.get("total_tax", 0.0))
                table_8d_drilldown.append({
                    "category": "Vendor Default - Claimed in Books, Missing in 2B",
                    "party": b.get("party_name", ""),
                    "gstin": b.get("gstin", ""),
                    "inv_no": b.get("raw_inv", ""),
                    "books_tax": tot_b,
                    "portal_tax": 0.0,
                    "diff_tax": tot_b,
                    "action": "Follow up supplier GSTR-1 or reverse excess ITC + interest.",
                })
            elif row.get("itc_ineligible") and row.get("portal"):
                p = row["portal"]
                table_8d_drilldown.append({
                    "category": "Table 8F - Ineligible ITC in 2B (Sec 17(5))",
                    "party": p.get("party_name", ""),
                    "gstin": p.get("gstin", ""),
                    "inv_no": p.get("raw_inv", ""),
                    "books_tax": float((row.get("books") or {}).get("total_tax", 0.0)),
                    "portal_tax": float(p.get("total_tax", 0.0)),
                    "diff_tax": 0.0,
                    "action": "Report under Table 7E / Table 8F (Do not avail).",
                })
            elif st == "VALUE_MISMATCH":
                b = row.get("books") or {}
                p = row.get("portal") or {}
                table_8d_drilldown.append({
                    "category": "Value Variance - 2B vs Books",
                    "party": b.get("party_name") or p.get("party_name", ""),
                    "gstin": b.get("gstin") or p.get("gstin", ""),
                    "inv_no": b.get("raw_inv") or p.get("raw_inv", ""),
                    "books_tax": float(b.get("total_tax", 0.0)),
                    "portal_tax": float(p.get("total_tax", 0.0)),
                    "diff_tax": _r2(float(row.get("tax_diff", 0.0))),
                    "action": row.get("remarks", "Reconcile excess/short tax claim."),
                })

    t8_8b = {h: _r2(t6_6b[h] + t6_6h[h]) for h in ("igst", "cgst", "sgst", "cess")}
    t8_8c = dict(bk_next_fy_claimed)
    t8_8d_diff = {h: _r2(t8_8a_2b[h] - (t8_8b[h] + t8_8c[h])) for h in ("igst", "cgst", "sgst", "cess")}

    table_6_rows = [
        {"code": "6A", "desc": "Total ITC availed through Form GSTR-3B (sum of 4A)", **t6_6a},
        {"code": "6B", "desc": "Inward supplies (other than imports & RCM) — Inputs/Services/Capex", **t6_6b},
        {"code": "6C/6D", "desc": "Inward supplies liable to Reverse Charge (RCM)", **t6_6c_d},
        {"code": "6E", "desc": "Import of Goods (including SEZ)", **t6_6e},
        {"code": "6F", "desc": "Import of Services", **t6_6f},
        {"code": "6G", "desc": "Input Tax Credit received from ISD", **t6_6g},
        {"code": "6H", "desc": "Amount of ITC reclaimed under Rule 37", **t6_6h},
        {"code": "6O", "desc": "Total ITC Availed (Table 6O)", **t6_6o, "is_total": True},
    ]

    table_7_rows = [
        {"code": "7A", "desc": "As per Rule 37 (Creditor unpaid > 180 days)", **t7_7a_rule37},
        {"code": "7C", "desc": "As per Rule 42 (Proportionate Exempt Turnover reversal)", **t7_7c_rule42},
        {"code": "7D", "desc": "As per Rule 43 (Capital Goods exempt reversal)", **t7_7d_rule43},
        {"code": "7E", "desc": "As per Sec 17(5) Blocked Credit & Sec 17(1) Personal", **t7_7e_sec17_5},
        {"code": "7H", "desc": "Other Reversals (GSTR-3B 4B(2))", **t7_7h_other},
        {"code": "7I", "desc": "Total ITC Reversed (Sum of 7A to 7H)", **t7_7i_total, "is_subtotal": True},
        {"code": "7J", "desc": "Net ITC Available for Annual Utilization (6O - 7I)", **t7_7j_net, "is_total": True},
    ]

    table_8_rows = [
        {"code": "8A", "desc": "ITC as per GSTR-2B (Table 3 & 5 Auto-populated FY B2B)", **t8_8a_2b},
        {"code": "8B", "desc": "ITC as per sum total of 6(B) and 6(H) above", **t8_8b},
        {"code": "8C", "desc": "ITC on inward supplies received in FY but availed Apr-Nov next FY", **t8_8c},
        {"code": "8D", "desc": "Difference [8A - (8B + 8C)]", **t8_8d_diff, "is_total": True},
        {"code": "8E", "desc": "ITC available in 2B but not availed (Unbooked Purchases)", **t8_8e_unbooked},
        {"code": "8F", "desc": "ITC available in 2B but ineligible (Sec 17(5) / Time-Barred)", **t8_8f_inelig_2b},
    ]

    # ------------------------------------------------------------------
    # 4. TAX PAID (TABLE 9), RULE 88A & PRIOR-FY TABLES 10-14
    # ------------------------------------------------------------------
    tax_payable_ref = g1_4n if not g1_df.empty else bk_4n
    tp_61 = gstr3b["tax_paid_6_1"]
    paid_itc = tp_61["itc_utilized"]
    paid_cash = tp_61["cash_paid"]

    # If user didn't supply explicit 6.1 payment breakdown, simulate compliant Rule 88A utilization
    # when 3B outward tax is present or leave 0 to show exact payable shortfall cleanly
    table_9_rows = []
    total_payable_sum = 0.0
    total_paid_sum = 0.0
    for head, label in [("igst", "Integrated Tax (IGST)"), ("cgst", "Central Tax (CGST)"), ("sgst", "State/UT Tax (SGST)"), ("cess", "Cess")]:
        payable = _r2(tax_payable_ref[head])
        p_cash = _r2(paid_cash[head])
        p_itc = _r2(paid_itc[head])
        tot_paid = _r2(p_cash + p_itc)
        shortfall = _r2(payable - tot_paid)
        total_payable_sum = _r2(total_payable_sum + payable)
        total_paid_sum = _r2(total_paid_sum + tot_paid)
        table_9_rows.append({
            "head": label,
            "code": head.upper(),
            "tax_payable": payable,
            "paid_through_cash": p_cash,
            "paid_through_itc": p_itc,
            "total_paid": tot_paid,
            "shortfall_excess": shortfall,
        })

    # Rule 88A Cross-Utilization check:
    # Did taxpayer utilize CGST/SGST ITC while IGST net eligible ITC still exceeded IGST ITC utilized?
    net_igst_avail = t7_7j_net["igst"]
    igst_utilized = paid_itc["igst"]
    cgst_sgst_utilized = paid_itc["cgst"] + paid_itc["sgst"]
    rule88a_warning = False
    rule88a_msg = "Rule 88A Order Compliant (IGST ITC exhausted before CGST/SGST or zero cross-utilization issue)."
    if igst_utilized < net_igst_avail - 1.0 and cgst_sgst_utilized > 0:
        unspent_igst = _r2(net_igst_avail - igst_utilized)
        rule88a_warning = True
        rule88a_msg = f"Rule 88A Check: Rs. {unspent_igst:,.2f} IGST ITC remained unutilized while Rs. {cgst_sgst_utilized:,.2f} CGST/SGST ITC was consumed."

    books_interest = _r2(overrides.get("books_interest", 0.0))
    books_late_fee = _r2(overrides.get("books_late_fee", 0.0))
    portal_interest = _r2(tp_61.get("interest_paid", 0.0))
    portal_late_fee = _r2(tp_61.get("late_fee_paid", 0.0))

    tables_10_14 = [
        {"code": "10", "desc": "Supplies / tax declared through Amendments (+) in Next FY (Apr-Nov)", **t10_pos},
        {"code": "11", "desc": "Supplies / tax reduced through Amendments (-) in Next FY (Apr-Nov)", **t11_neg},
        {"code": "12", "desc": "Reversal of ITC availed during previous FY", **_zero_tax()},
        {"code": "13", "desc": "ITC availed for the previous FY in Apr-Nov next FY", "taxable": 0.0, **t8_8c},
        {"code": "14", "desc": "Differential Tax Payable on Table 10 - Table 11", **{k: _r2(t10_pos[k] - t11_neg[k]) for k in ("taxable", "igst", "cgst", "sgst", "cess")}},
    ]

    # ------------------------------------------------------------------
    # 5. HSN SUMMARY VALIDATION (TABLES 17 & 18)
    # ------------------------------------------------------------------
    hsn_out_src = gstr1_hsn_df if (gstr1_hsn_df is not None and not gstr1_hsn_df.empty) else (g1_df if not g1_df.empty else s_df)
    table_17_rows, hsn17_stats = _build_hsn_table(hsn_out_src, turnover_above_5cr, side="Outward")
    table_18_rows, hsn18_stats = _build_hsn_table(pur_flags_df, turnover_above_5cr, side="Inward")

    # ------------------------------------------------------------------
    # 6. GSTR-9C AUDIT TIE-OUT & EXECUTIVE SUMMARY
    # ------------------------------------------------------------------
    net_itc_7j_total = _sum_heads(t7_7j_net)
    books_eligible_itc_total = _r2(_sum_heads(bk_6b_sum) + _sum_heads(bk_rcm_itc) + _sum_heads(bk_import_itc) - _sum_heads(bk_rule37))
    gstr9c_bridge = {
        "table_5_turnover": {
            "books_annual_turnover": bk_5n_turnover,
            "gstr9_declared_turnover": g1_5n_turnover if not g1_df.empty else bk_5n_turnover,
            "unreconciled_turnover_diff": turnover_variance,
        },
        "table_12_itc": {
            "books_net_eligible_itc": books_eligible_itc_total,
            "gstr9_table_7j_net_itc": net_itc_7j_total,
            "unreconciled_itc_diff": _r2(books_eligible_itc_total - net_itc_7j_total),
        },
    }

    summary = {
        "financial_year": fy,
        "cutoff_window": f"Apr - {'Oct 31' if int(cutoff_month) == 10 else 'Nov 30'} (Next FY)",
        "turnover_above_5cr": bool(turnover_above_5cr),
        "books_turnover": bk_5n_turnover,
        "gstr1_turnover": g1_5n_turnover,
        "gstr3b_turnover": g3b_5n_turnover,
        "turnover_variance": turnover_variance,
        "outward_tax_books": _r2(bk_out_tax_total),
        "outward_tax_3b": _r2(g3b_out_tax_total),
        "outward_tax_variance": outward_tax_variance,
        "table_6o_gross_itc": _sum_heads(t6_6o),
        "table_7i_reversals": _sum_heads(t7_7i_total),
        "table_7j_net_itc": net_itc_7j_total,
        "table_8a_2b_itc": _sum_heads(t8_8a_2b),
        "table_8c_next_fy_itc": _sum_heads(t8_8c),
        "table_8d_diff_itc": _sum_heads(t8_8d_diff),
        "sec_17_5_blocked_tax": _sum_heads(t7_7e_sec17_5),
        "rule_37_unpaid_tax": _sum_heads(t7_7a_rule37),
        "rule_42_exempt_ratio_pct": round(rule42_ratio * 100.0, 2),
        "rule_42_reversal_tax": _sum_heads(t7_7c_rule42),
        "tax_payable_total": total_payable_sum,
        "tax_paid_total": total_paid_sum,
        "tax_shortfall_excess": _r2(total_payable_sum - total_paid_sum),
        "rule88a_warning": rule88a_warning,
        "rule88a_message": rule88a_msg,
        "portal_interest_late_fee": _r2(portal_interest + portal_late_fee),
        "books_interest_late_fee": _r2(books_interest + books_late_fee),
        "interest_late_fee_diff": _r2((books_interest + books_late_fee) - (portal_interest + portal_late_fee)),
        "hsn_total_errors": hsn17_stats["error_count"] + hsn18_stats["error_count"],
        "reversals_count": len(reversals_schedule),
        "drilldown_count": len(table_8d_drilldown),
    }

    return {
        "status": "success",
        "summary": summary,
        "tables": {
            "table_4": table_4_rows,
            "table_5": table_5_rows,
            "table_6": table_6_rows,
            "table_7": table_7_rows,
            "table_8": table_8_rows,
            "table_9": table_9_rows,
            "tables_10_14": tables_10_14,
            "table_17": table_17_rows,
            "table_17_stats": hsn17_stats,
            "table_18": table_18_rows,
            "table_18_stats": hsn18_stats,
        },
        "gstr9c_bridge": gstr9c_bridge,
        "reversals_schedule": reversals_schedule,
        "table_8d_drilldown": table_8d_drilldown,
        "gstr3b_parsed": gstr3b,
    }
