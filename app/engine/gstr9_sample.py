"""
Realistic 12-Month FY 2025-26 (+ Next-FY Apr-Nov Sec 16(4) Cutoff) Sample Generator
for immediate demonstration and regression testing of the GSTR-9 Annual Return Hub.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict

import pandas as pd

from app.engine.gstr9_engine import compute_gstr9_return
from app.engine.gstr9_parsers import empty_gstr3b_structure


def build_sample_gstr9_datasets(fy: str = "2025-26") -> Dict[str, Any]:
    """Build deterministic full-year Books + Portal 1/2B/3B sample DataFrames."""

    # 1. Company Sales Register (Apr 2025 - Mar 2026 + 2 Next-FY Amendments)
    sales_rows = [
        # B2B Intra-State & Inter-State
        {"Party Name": "Reliance Retail Ventures Ltd", "GSTIN": "27AAACR5055K1Z5", "Invoice No": "INV/25-26/001", "Date": "12-04-2025", "Voucher Type": "Sales", "Supply Category": "B2B", "HSN": "84713010", "UQC": "NOS", "Qty": 120, "Rate (%)": 18, "Taxable": 2400000.0, "IGST": 432000.0, "CGST": 0.0, "SGST": 0.0, "Cess": 0.0},
        {"Party Name": "Bharti Enterprise Hub Delhi", "GSTIN": "07AAACB1234M1Z8", "Invoice No": "INV/25-26/002", "Date": "18-06-2025", "Voucher Type": "Sales", "Supply Category": "B2B", "HSN": "85176290", "UQC": "NOS", "Qty": 85, "Rate (%)": 18, "Taxable": 1850000.0, "IGST": 0.0, "CGST": 166500.0, "SGST": 166500.0, "Cess": 0.0},
        {"Party Name": "Tata Steel Precision Div", "GSTIN": "20AAACT2727Q1ZW", "Invoice No": "INV/25-26/003", "Date": "22-09-2025", "Voucher Type": "Sales", "Supply Category": "B2B", "HSN": "730890", "UQC": "KGS", "Qty": 4500, "Rate (%)": 18, "Taxable": 3200000.0, "IGST": 576000.0, "CGST": 0.0, "SGST": 0.0, "Cess": 0.0},
        {"Party Name": "Apollo MedTech Solutions", "GSTIN": "29AAACA9981K1Z1", "Invoice No": "INV/25-26/004", "Date": "14-12-2025", "Voucher Type": "Sales", "Supply Category": "B2B", "HSN": "9018", "UQC": "NOS", "Qty": 30, "Rate (%)": 12, "Taxable": 900000.0, "IGST": 108000.0, "CGST": 0.0, "SGST": 0.0, "Cess": 0.0},  # Intentionally 4-digit HSN to trigger >5Cr warning
        # B2C
        {"Party Name": "Retail Walk-in Consolidated Q1-Q4", "GSTIN": "", "Invoice No": "B2C/25-26/ALL", "Date": "15-01-2026", "Voucher Type": "Sales", "Supply Category": "B2C", "HSN": "850440", "UQC": "NOS", "Qty": 420, "Rate (%)": 18, "Taxable": 1250000.0, "IGST": 0.0, "CGST": 112500.0, "SGST": 112500.0, "Cess": 0.0},
        # Export With Payment of Tax & Without Payment (LUT)
        {"Party Name": "EuroTrade GmbH (Export IGST)", "GSTIN": "", "Invoice No": "EXP/25-26/011", "Date": "09-08-2025", "Voucher Type": "Sales", "Supply Category": "Export With Tax", "HSN": "848180", "UQC": "NOS", "Qty": 200, "Rate (%)": 18, "Taxable": 1600000.0, "IGST": 288000.0, "CGST": 0.0, "SGST": 0.0, "Cess": 0.0},
        {"Party Name": "Singapore Logistics LUT", "GSTIN": "", "Invoice No": "EXP/25-26/012", "Date": "19-11-2025", "Voucher Type": "Sales", "Supply Category": "Export LUT", "HSN": "998314", "UQC": "OTH", "Qty": 1, "Rate (%)": 0, "Taxable": 800000.0, "IGST": 0.0, "CGST": 0.0, "SGST": 0.0, "Cess": 0.0},
        # Exempt / Nil Turnover (drives Rule 42 ratio)
        {"Party Name": "Agri & Educational Exempt Goods", "GSTIN": "", "Invoice No": "EXM/25-26/001", "Date": "11-02-2026", "Voucher Type": "Sales", "Supply Category": "Exempt", "HSN": "490110", "UQC": "NOS", "Qty": 5000, "Rate (%)": 0, "Taxable": 600000.0, "IGST": 0.0, "CGST": 0.0, "SGST": 0.0, "Cess": 0.0},
        # Credit Note
        {"Party Name": "Bharti Enterprise Hub Delhi", "GSTIN": "07AAACB1234M1Z8", "Invoice No": "CN/25-26/001", "Date": "20-02-2026", "Voucher Type": "Credit Note", "Supply Category": "Credit Note", "HSN": "85176290", "UQC": "NOS", "Qty": -5, "Rate (%)": 18, "Taxable": -150000.0, "IGST": 0.0, "CGST": -13500.0, "SGST": -13500.0, "Cess": 0.0},
        # Next-FY Apr-Nov Amendment (Table 10 addition reported in Jul 2026)
        {"Party Name": "Reliance Retail Ventures Ltd", "GSTIN": "27AAACR5055K1Z5", "Invoice No": "INV/25-26/099", "Date": "28-03-2026", "Return Period": "10-07-2026", "Voucher Type": "Sales", "Supply Category": "B2B", "HSN": "84713010", "UQC": "NOS", "Qty": 10, "Rate (%)": 18, "Taxable": 250000.0, "IGST": 45000.0, "CGST": 0.0, "SGST": 0.0, "Cess": 0.0},
    ]
    sales_df = pd.DataFrame(sales_rows)

    # 2. GSTR-1 Annual Export (One B2B invoice un-uploaded in FY -> reported in Next-FY Apr-Nov)
    g1_rows = list(sales_rows)
    # Remove INV/25-26/004 or simulate minor Rs. 25,000 timing difference so Table 4 shows real variance detection
    g1_rows[4] = dict(sales_rows[4], Taxable=1225000.0, CGST=110250.0, SGST=110250.0)
    gstr1_df = pd.DataFrame(g1_rows)

    # 3. Purchase Register (Books) & GSTR-2B Consolidated across FY + Cutoff
    pur_rows = [
        {"Party Name": "Siemens Industrial Motors", "GST No": "27AAACS0764L1ZT", "Invoice no": "SIE/25-26/101", "Date": "10-05-2025", "HSN": "850152", "UQC": "NOS", "Qty": 20, "Rate (%)": 18, "Taxable": 1800000.0, "CGST": 0.0, "SGST": 0.0, "IGST": 324000.0, "ITC Category": "Inputs", "Unpaid Days": 45},
        {"Party Name": "Larsen & Toubro Heavy Fab", "GST No": "27AAACL0140P1Z8", "Invoice no": "LNT/25-26/220", "Date": "14-07-2025", "HSN": "842952", "UQC": "NOS", "Qty": 2, "Rate (%)": 18, "Taxable": 1400000.0, "CGST": 0.0, "SGST": 0.0, "IGST": 252000.0, "ITC Category": "Capital Goods", "Unpaid Days": 30},
        {"Party Name": "CloudVault ERP Services", "GST No": "07AAACC4120F1Z2", "Invoice no": "CLD/25-26/055", "Date": "18-09-2025", "HSN": "998314", "UQC": "OTH", "Qty": 1, "Rate (%)": 18, "Taxable": 600000.0, "CGST": 54000.0, "SGST": 54000.0, "IGST": 0.0, "ITC Category": "Common Credit Input Services (Rule 42)", "Unpaid Days": 25},
        # Rule 37 > 180 Days Unpaid Creditor
        {"Party Name": "Kaveri Components (Overdue >180d)", "GST No": "29AAACK8821D1Z9", "Invoice no": "KAV/25-26/312", "Date": "04-06-2025", "HSN": "870899", "UQC": "NOS", "Qty": 500, "Rate (%)": 28, "Taxable": 300000.0, "CGST": 0.0, "SGST": 0.0, "IGST": 84000.0, "ITC Category": "Inputs - Rule 37", "Unpaid Days": 240},
        # Blocked Credit Sec 17(5) & Personal Sec 17(1)
        {"Party Name": "Royal Motors Passenger SUV Repair", "GST No": "07AAACR3311K1Z4", "Invoice no": "RMG/25-26/088", "Date": "22-11-2025", "HSN": "998729", "UQC": "OTH", "Qty": 1, "Rate (%)": 18, "Taxable": 120000.0, "CGST": 10800.0, "SGST": 10800.0, "IGST": 0.0, "ITC Category": "Blocked Sec 17(5) Car Repair", "ITC Eligibility": "No", "Unpaid Days": 10},
        {"Party Name": "Celebration Caterers Club (Personal)", "GST No": "07AAACC9011L1Z1", "Invoice no": "CCC/25-26/404", "Date": "19-12-2025", "HSN": "996331", "UQC": "OTH", "Qty": 1, "Rate (%)": 5, "Taxable": 80000.0, "CGST": 2000.0, "SGST": 2000.0, "IGST": 0.0, "ITC Category": "Personal Non-Business Sec 17(1)", "ITC Eligibility": "No", "Unpaid Days": 5},
        # RCM Inward GTA Freight
        {"Party Name": "Gati Express GTA Carriers", "GST No": "27AAACG7744B1Z6", "Invoice no": "GAT/25-26/501", "Date": "08-01-2026", "HSN": "996511", "UQC": "OTH", "Qty": 1, "Rate (%)": 5, "Taxable": 400000.0, "CGST": 0.0, "SGST": 0.0, "IGST": 20000.0, "RCM Applicable": "Yes", "ITC Category": "Input Services RCM", "Unpaid Days": 15},
        # Vendor Default Missing in 2B
        {"Party Name": "Apex Unfiled Traders Pvt Ltd", "GST No": "06AAACA6655E1Z3", "Invoice no": "APX/25-26/909", "Date": "12-02-2026", "HSN": "7318", "UQC": "KGS", "Qty": 150, "Rate (%)": 18, "Taxable": 200000.0, "CGST": 0.0, "SGST": 0.0, "IGST": 36000.0, "ITC Category": "Inputs", "Unpaid Days": 20},
        # Next-FY Apr-Nov Claimed (Table 8C / Table 13)
        {"Party Name": "Siemens Industrial Motors", "GST No": "27AAACS0764L1ZT", "Invoice no": "SIE/25-26/888", "Date": "27-03-2026", "Return Period": "12-05-2026", "HSN": "850152", "UQC": "NOS", "Qty": 5, "Rate (%)": 18, "Taxable": 500000.0, "CGST": 0.0, "SGST": 0.0, "IGST": 90000.0, "ITC Category": "Inputs", "Unpaid Days": 12},
    ]
    purchase_df = pd.DataFrame(pur_rows)

    g2b_rows = [
        {"GSTIN of supplier": "27AAACS0764L1ZT", "Trade/Legal name": "Siemens Industrial Motors", "Invoice number": "SIE/25-26/101", "Invoice Date": "10/05/2025", "Place of supply": "07-Delhi", "Supply Attract Reverse Charge": "N", "Rate (%)": 18, "Taxable Value (₹)": 1800000.0, "Integrated Tax (₹)": 324000.0, "Central Tax (₹)": 0.0, "State/UT Tax (₹)": 0.0, "Cess (₹)": 0.0, "ITC Availability": "Yes"},
        {"GSTIN of supplier": "27AAACL0140P1Z8", "Trade/Legal name": "Larsen & Toubro Heavy Fab", "Invoice number": "LNT/25-26/220", "Invoice Date": "14/07/2025", "Place of supply": "07-Delhi", "Supply Attract Reverse Charge": "N", "Rate (%)": 18, "Taxable Value (₹)": 1400000.0, "Integrated Tax (₹)": 252000.0, "Central Tax (₹)": 0.0, "State/UT Tax (₹)": 0.0, "Cess (₹)": 0.0, "ITC Availability": "Yes"},
        {"GSTIN of supplier": "07AAACC4120F1Z2", "Trade/Legal name": "CloudVault ERP Services", "Invoice number": "CLD/25-26/055", "Invoice Date": "18/09/2025", "Place of supply": "07-Delhi", "Supply Attract Reverse Charge": "N", "Rate (%)": 18, "Taxable Value (₹)": 600000.0, "Integrated Tax (₹)": 0.0, "Central Tax (₹)": 54000.0, "State/UT Tax (₹)": 54000.0, "Cess (₹)": 0.0, "ITC Availability": "Yes"},
        {"GSTIN of supplier": "29AAACK8821D1Z9", "Trade/Legal name": "Kaveri Components (Overdue >180d)", "Invoice number": "KAV/25-26/312", "Invoice Date": "04/06/2025", "Place of supply": "07-Delhi", "Supply Attract Reverse Charge": "N", "Rate (%)": 28, "Taxable Value (₹)": 300000.0, "Integrated Tax (₹)": 84000.0, "Central Tax (₹)": 0.0, "State/UT Tax (₹)": 0.0, "Cess (₹)": 0.0, "ITC Availability": "Yes"},
        {"GSTIN of supplier": "07AAACR3311K1Z4", "Trade/Legal name": "Royal Motors Passenger SUV Repair", "Invoice number": "RMG/25-26/088", "Invoice Date": "22/11/2025", "Place of supply": "07-Delhi", "Supply Attract Reverse Charge": "N", "Rate (%)": 18, "Taxable Value (₹)": 120000.0, "Integrated Tax (₹)": 0.0, "Central Tax (₹)": 10800.0, "State/UT Tax (₹)": 10800.0, "Cess (₹)": 0.0, "ITC Availability": "No"},
        {"GSTIN of supplier": "07AAACC9011L1Z1", "Trade/Legal name": "Celebration Caterers Club (Personal)", "Invoice number": "CCC/25-26/404", "Invoice Date": "19/12/2025", "Place of supply": "07-Delhi", "Supply Attract Reverse Charge": "N", "Rate (%)": 5, "Taxable Value (₹)": 80000.0, "Integrated Tax (₹)": 0.0, "Central Tax (₹)": 2000.0, "State/UT Tax (₹)": 2000.0, "Cess (₹)": 0.0, "ITC Availability": "No"},
        {"GSTIN of supplier": "27AAACG7744B1Z6", "Trade/Legal name": "Gati Express GTA Carriers", "Invoice number": "GAT/25-26/501", "Invoice Date": "08/01/2026", "Place of supply": "07-Delhi", "Supply Attract Reverse Charge": "Y", "Rate (%)": 5, "Taxable Value (₹)": 400000.0, "Integrated Tax (₹)": 20000.0, "Central Tax (₹)": 0.0, "State/UT Tax (₹)": 0.0, "Cess (₹)": 0.0, "ITC Availability": "Yes"},
        # Unbooked in Books -> Table 8E
        {"GSTIN of supplier": "27AAACB5522N1Z7", "Trade/Legal name": "BlueDart Air Freight (Unbooked)", "Invoice number": "BDT/25-26/774", "Invoice Date": "24/03/2026", "Place of supply": "07-Delhi", "Supply Attract Reverse Charge": "N", "Rate (%)": 18, "Taxable Value (₹)": 150000.0, "Integrated Tax (₹)": 27000.0, "Central Tax (₹)": 0.0, "State/UT Tax (₹)": 0.0, "Cess (₹)": 0.0, "ITC Availability": "Yes"},
        {"GSTIN of supplier": "27AAACS0764L1ZT", "Trade/Legal name": "Siemens Industrial Motors", "Invoice number": "SIE/25-26/888", "Invoice Date": "27/03/2026", "Return Period": "12-05-2026", "Place of supply": "07-Delhi", "Supply Attract Reverse Charge": "N", "Rate (%)": 18, "Taxable Value (₹)": 500000.0, "Integrated Tax (₹)": 90000.0, "Central Tax (₹)": 0.0, "State/UT Tax (₹)": 0.0, "Cess (₹)": 0.0, "ITC Availability": "Yes"},
    ]
    g2b_df = pd.DataFrame(g2b_rows)

    # 4. Filed Annual GSTR-3B Summary
    g3b = empty_gstr3b_structure()
    g3b["outward_3_1"]["a_taxable"] = {"taxable": 9425000.0, "igst": 1116000.0, "cgst": 263250.0, "sgst": 263250.0, "cess": 0.0}
    g3b["outward_3_1"]["b_zero_rated"] = {"taxable": 2400000.0, "igst": 288000.0, "cgst": 0.0, "sgst": 0.0, "cess": 0.0}
    g3b["outward_3_1"]["c_nil_exempt"] = {"taxable": 600000.0, "igst": 0.0, "cgst": 0.0, "sgst": 0.0, "cess": 0.0}
    g3b["outward_3_1"]["d_inward_rcm"] = {"taxable": 400000.0, "igst": 20000.0, "cgst": 0.0, "sgst": 0.0, "cess": 0.0}
    g3b["itc_4"]["4A_3_rcm"] = {"igst": 20000.0, "cgst": 0.0, "sgst": 0.0, "cess": 0.0}
    g3b["itc_4"]["4A_5_all_other"] = {"igst": 696000.0, "cgst": 54000.0, "sgst": 54000.0, "cess": 0.0}
    g3b["itc_4"]["4D_1_sec_17_5"] = {"igst": 0.0, "cgst": 12800.0, "sgst": 12800.0, "cess": 0.0}

    # Simulate realistic Cash + ITC payment with a slight Rule 88A warning + Rs. 2,500 interest variance
    g3b["tax_paid_6_1"]["itc_utilized"] = {"igst": 615000.0, "cgst": 50000.0, "sgst": 50000.0, "cess": 0.0}
    g3b["tax_paid_6_1"]["cash_paid"] = {"igst": 809000.0, "cgst": 213250.0, "sgst": 213250.0, "cess": 0.0}
    g3b["tax_paid_6_1"]["interest_paid"] = 4500.0
    g3b["tax_paid_6_1"]["late_fee_paid"] = 1000.0

    return {
        "sales_df": sales_df,
        "gstr1_df": gstr1_df,
        "pur_df": purchase_df,
        "g2b_df": g2b_df,
        "g3b": g3b,
        "books_interest_total": 8000.0,
    }


def run_sample_gstr9_fy2526(turnover_above_5cr: bool = True, cutoff_month: int = 11) -> Dict[str, Any]:
    """Build deterministic full-year Books + Portal 1/2B/3B datasets and execute GSTR-9 computation."""
    ds = build_sample_gstr9_datasets(fy="2025-26")
    return compute_gstr9_return(
        fy="2025-26",
        turnover_above_5cr=turnover_above_5cr,
        cutoff_month=cutoff_month,
        sales_df=ds["sales_df"],
        gstr1_df=ds["gstr1_df"],
        gstr3b_data=ds["g3b"],
        raw_books_purchase_df=ds["pur_df"],
        raw_2b_df=ds["g2b_df"],
        manual_overrides={"books_interest": 7000.0, "books_late_fee": 1000.0},
    )

