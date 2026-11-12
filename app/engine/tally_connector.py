"""
Tally Prime Direct XML/HTTP Connector for Purchase Register Synchronization.
Connects to local Tally Prime instance on port 9000 via TDL/XML requests
to extract Purchase Vouchers directly into the reconciliation engine without manual Excel export.
"""

import os
import socket
import xml.etree.ElementTree as ET
from typing import Dict, Any, Optional
import urllib.request
import urllib.error
import pandas as pd


def check_tally_status(host: str = "localhost", port: int = 9000, timeout: float = 2.0) -> Dict[str, Any]:
    """Check if Tally Prime XML Server is active and listening on the specified port."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        if result == 0:
            # Query active company name from Tally
            req_xml = """<ENVELOPE>
                <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Data</TYPE><ID>CurrentCompany</ID></HEADER>
                <BODY><DESC><STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES><TDL><TDLMESSAGE><REPORT NAME="CurrentCompany"><FORMS>CurrentCompany</FORMS></REPORT><FORM NAME="CurrentCompany"><PARTS>CurrentCompany</PARTS></FORM><PART NAME="CurrentCompany"><LINES>CurrentCompany</LINES></PART><LINE NAME="CurrentCompany"><FIELDS>CurrentCompany</FIELDS></LINE><FIELD NAME="CurrentCompany"><SET>##SVCURRENTCOMPANY</SET></FIELD></TDLMESSAGE></TDL></DESC></BODY>
            </ENVELOPE>"""
            try:
                req = urllib.request.Request(
                    f"http://{host}:{port}",
                    data=req_xml.encode("utf-8"),
                    headers={"Content-Type": "text/xml;charset=utf-8"}
                )
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    resp_xml = resp.read().decode("utf-8", errors="ignore")
                    company_name = "Connected Company"
                    if "<FIELD>" in resp_xml:
                        company_name = resp_xml.split("<FIELD>")[1].split("</FIELD>")[0].strip() or company_name
                    return {
                        "active": True,
                        "host": host,
                        "port": port,
                        "company": company_name,
                        "message": f"Connected to Tally Prime ({company_name})"
                    }
            except Exception:
                return {
                    "active": True,
                    "host": host,
                    "port": port,
                    "company": "Active Company",
                    "message": "Connected to Tally Prime on port 9000"
                }
        else:
            return {
                "active": False,
                "host": host,
                "port": port,
                "company": "",
                "message": "Tally Prime is not listening on port 9000."
            }
    except Exception as e:
        return {
            "active": False,
            "host": host,
            "port": port,
            "company": "",
            "message": f"Connection error: {str(e)}"
        }


def fetch_purchase_vouchers_from_tally(
    host: str = "localhost",
    port: int = 9000,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    timeout: float = 10.0
) -> pd.DataFrame:
    """
    Export Purchase Vouchers from Tally Prime via XML HTTP request.
    Extracts Date, Voucher No, Supplier Name, Supplier GSTIN, Taxable Value, CGST, SGST, IGST, Total.
    """
    # Check connectivity first
    status = check_tally_status(host, port, timeout=2.0)
    if not status["active"]:
        raise ConnectionError(
            "Tally Prime is not running or HTTP server is disabled on port 9000. "
            "In Tally Prime: F1 (Help) -> Settings -> Connectivity -> Client/Server configuration -> Enable ODBC/HTTP: Yes (Port 9000)."
        )

    # Standard Tally XML query for purchase register
    xml_envelope = """<ENVELOPE>
        <HEADER>
            <VERSION>1</VERSION>
            <TALLYREQUEST>Export</TALLYREQUEST>
            <TYPE>Collection</TYPE>
            <ID>PurchaseRegisterCollection</ID>
        </HEADER>
        <BODY>
            <DESC>
                <STATICVARIABLES>
                    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                </STATICVARIABLES>
                <TDL>
                    <TDLMESSAGE>
                        <COLLECTION NAME="PurchaseRegisterCollection">
                            <TYPE>Voucher</TYPE>
                            <FILTER>IsPurchaseVoucher</FILTER>
                            <FETCH>Date, VoucherNumber, Reference, PartyName, PartyLedgerName, PartyGSTIN, BasicBuyerAddress.List.*, LedgerEntries.List.*</FETCH>
                        </COLLECTION>
                        <SYSTEM TYPE="Formulae" NAME="IsPurchaseVoucher">
                            $$IsPurchase:$VoucherTypeName
                        </SYSTEM>
                    </TDLMESSAGE>
                </TDL>
            </DESC>
        </BODY>
    </ENVELOPE>"""

    req = urllib.request.Request(
        f"http://{host}:{port}",
        data=xml_envelope.encode("utf-8"),
        headers={"Content-Type": "text/xml;charset=utf-8"}
    )

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        xml_content = resp.read().decode("utf-8", errors="ignore")

    root = ET.fromstring(xml_content)
    rows = []

    for voucher in root.findall(".//VOUCHER"):
        v_date = voucher.findtext("DATE", "")
        v_num = voucher.findtext("VOUCHERNUMBER", "")
        v_ref = voucher.findtext("REFERENCE", "") or v_num
        party_name = voucher.findtext("PARTYNAME", "") or voucher.findtext("PARTYLEDGERNAME", "")
        party_gstin = voucher.findtext("PARTYGSTIN", "")

        taxable = 0.0
        igst = 0.0
        cgst = 0.0
        sgst = 0.0
        cess = 0.0
        total = 0.0

        for entry in voucher.findall(".//LEDGERENTRIES.LIST"):
            ledger = entry.findtext("LEDGERNAME", "").lower()
            amt_str = entry.findtext("AMOUNT", "0")
            try:
                amt = abs(float(amt_str))
            except ValueError:
                amt = 0.0

            if "igst" in ledger or "integrated" in ledger:
                igst += amt
            elif "cgst" in ledger or "central" in ledger:
                cgst += amt
            elif "sgst" in ledger or "state" in ledger or "utgst" in ledger:
                sgst += amt
            elif "cess" in ledger:
                cess += amt
            elif "purchase" in ledger or "item" in ledger or "expense" in ledger:
                taxable += amt

        # Format date YYYYMMDD to YYYY-MM-DD
        if len(v_date) == 8 and v_date.isdigit():
            formatted_date = f"{v_date[:4]}-{v_date[4:6]}-{v_date[6:]}"
        else:
            formatted_date = v_date

        calc_total = taxable + igst + cgst + sgst + cess

        rows.append({
            "Voucher No": v_num,
            "Invoice Date": formatted_date,
            "Invoice Number": v_ref,
            "Supplier Name": party_name,
            "Supplier GSTIN": party_gstin,
            "Taxable Value (₹)": round(taxable, 2),
            "Integrated Tax (₹)": round(igst, 2),
            "Central Tax (₹)": round(cgst, 2),
            "State/UT Tax (₹)": round(sgst, 2),
            "Cess (₹)": round(cess, 2),
            "Total Invoice Value (₹)": round(calc_total, 2)
        })

    return pd.DataFrame(rows)


def simulate_tally_sync() -> pd.DataFrame:
    """Return benchmark Purchase Register data when Tally is offline for demo."""
    import sys
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    meipass = getattr(sys, "_MEIPASS", None)
    candidates = [
        os.path.join(meipass, "sample_data", "sample_purchase_register.xlsx") if meipass else "",
        os.path.join(project_root, "sample_data", "sample_purchase_register.xlsx"),
        os.path.join(os.getcwd(), "sample_data", "sample_purchase_register.xlsx"),
    ]
    for sample_file in candidates:
        if sample_file and os.path.exists(sample_file):
            return pd.read_excel(sample_file, sheet_name="Purchase Register")
    return pd.DataFrame()


def fetch_sales_vouchers_from_tally(
    host: str = "localhost",
    port: int = 9000,
    timeout: float = 10.0,
) -> pd.DataFrame:
    """
    Export Sales & Credit/Debit Note vouchers from Tally Prime XML Server (Port 9000)
    for GSTR-9 Outward Reconciliation.
    """
    status = check_tally_status(host, port, timeout=2.0)
    if not status["active"]:
        raise ConnectionError("Tally Prime XML server is not running on port 9000.")

    xml_envelope = """<ENVELOPE>
        <HEADER>
            <VERSION>1</VERSION>
            <TALLYREQUEST>Export</TALLYREQUEST>
            <TYPE>Collection</TYPE>
            <ID>SalesRegisterCollection</ID>
        </HEADER>
        <BODY>
            <DESC>
                <STATICVARIABLES>
                    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                </STATICVARIABLES>
                <TDL>
                    <TDLMESSAGE>
                        <COLLECTION NAME="SalesRegisterCollection">
                            <TYPE>Voucher</TYPE>
                            <FILTER>IsOutwardVoucher</FILTER>
                            <FETCH>Date, VoucherNumber, VoucherTypeName, Reference, PartyName, PartyLedgerName, PartyGSTIN, LedgerEntries.List.*</FETCH>
                        </COLLECTION>
                        <SYSTEM TYPE="Formulae" NAME="IsOutwardVoucher">
                            $$IsSales:$VoucherTypeName OR $$IsCreditNote:$VoucherTypeName OR $$IsDebitNote:$VoucherTypeName
                        </SYSTEM>
                    </TDLMESSAGE>
                </TDL>
            </DESC>
        </BODY>
    </ENVELOPE>"""

    req = urllib.request.Request(
        f"http://{host}:{port}",
        data=xml_envelope.encode("utf-8"),
        headers={"Content-Type": "text/xml;charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        xml_content = resp.read().decode("utf-8", errors="ignore")

    root = ET.fromstring(xml_content)
    rows = []
    for voucher in root.findall(".//VOUCHER"):
        v_date = voucher.findtext("DATE", "")
        v_num = voucher.findtext("VOUCHERNUMBER", "")
        v_type = voucher.findtext("VOUCHERTYPENAME", "Sales")
        v_ref = voucher.findtext("REFERENCE", "") or v_num
        party_name = voucher.findtext("PARTYNAME", "") or voucher.findtext("PARTYLEDGERNAME", "")
        party_gstin = voucher.findtext("PARTYGSTIN", "")
        taxable = igst = cgst = sgst = cess = 0.0
        for entry in voucher.findall(".//LEDGERENTRIES.LIST"):
            ledger = entry.findtext("LEDGERNAME", "").lower()
            try:
                amt = abs(float(entry.findtext("AMOUNT", "0")))
            except ValueError:
                amt = 0.0
            if "igst" in ledger or "integrated" in ledger:
                igst += amt
            elif "cgst" in ledger or "central" in ledger:
                cgst += amt
            elif "sgst" in ledger or "state" in ledger or "utgst" in ledger:
                sgst += amt
            elif "cess" in ledger:
                cess += amt
            elif "sales" in ledger or "income" in ledger or "service" in ledger or "exempt" in ledger:
                taxable += amt

        if len(v_date) == 8 and v_date.isdigit():
            formatted_date = f"{v_date[:4]}-{v_date[4:6]}-{v_date[6:]}"
        else:
            formatted_date = v_date

        sign = -1.0 if "credit" in v_type.lower() else 1.0
        rows.append({
            "Voucher No": v_num,
            "Voucher Type": v_type,
            "Date": formatted_date,
            "Invoice No": v_ref,
            "Party Name": party_name,
            "GSTIN": party_gstin,
            "Taxable": round(sign * taxable, 2),
            "IGST": round(sign * igst, 2),
            "CGST": round(sign * cgst, 2),
            "SGST": round(sign * sgst, 2),
            "Cess": round(sign * cess, 2),
        })
    return pd.DataFrame(rows)


def fetch_gst_expense_ledgers_from_tally(
    host: str = "localhost",
    port: int = 9000,
    timeout: float = 8.0,
) -> Dict[str, float]:
    """
    Query Books Interest on GST & GST Late Fee expense ledgers from Tally Prime.
    """
    status = check_tally_status(host, port, timeout=2.0)
    if not status["active"]:
        raise ConnectionError("Tally Prime XML server is not running on port 9000.")

    xml_envelope = """<ENVELOPE>
        <HEADER>
            <VERSION>1</VERSION>
            <TALLYREQUEST>Export</TALLYREQUEST>
            <TYPE>Collection</TYPE>
            <ID>LedgerExpenses</ID>
        </HEADER>
        <BODY>
            <DESC>
                <STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES>
                <TDL><TDLMESSAGE>
                    <COLLECTION NAME="LedgerExpenses">
                        <TYPE>Ledger</TYPE>
                        <FETCH>Name, ClosingBalance</FETCH>
                    </COLLECTION>
                </TDLMESSAGE></TDL>
            </DESC>
        </BODY>
    </ENVELOPE>"""
    req = urllib.request.Request(
        f"http://{host}:{port}",
        data=xml_envelope.encode("utf-8"),
        headers={"Content-Type": "text/xml;charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        xml_content = resp.read().decode("utf-8", errors="ignore")

    root = ET.fromstring(xml_content)
    books_interest = 0.0
    books_late_fee = 0.0
    for ldg in root.findall(".//LEDGER"):
        name = (ldg.attrib.get("NAME") or ldg.findtext("NAME", "")).lower()
        try:
            bal = abs(float(ldg.findtext("CLOSINGBALANCE", "0")))
        except ValueError:
            bal = 0.0
        if "gst" in name and "interest" in name:
            books_interest += bal
        elif "gst" in name and ("late" in name or "penalty" in name):
            books_late_fee += bal

    return {"books_interest": round(books_interest, 2), "books_late_fee": round(books_late_fee, 2)}


def fetch_overdue_payables_rule37_from_tally(
    host: str = "localhost",
    port: int = 9000,
    days_threshold: int = 180,
    timeout: float = 8.0,
) -> Dict[str, Any]:
    """
    Query Tally Prime Bills Payable (BILL collection) for creditor invoices remaining
    unpaid beyond 180 days from bill date to automate Sec 16(2) / Rule 37 reversal tracking.
    """
    from datetime import date, datetime

    status = check_tally_status(host, port, timeout=2.0)
    if not status["active"]:
        raise ConnectionError("Tally Prime XML server is not running on port 9000.")

    xml_envelope = """<ENVELOPE>
        <HEADER>
            <VERSION>1</VERSION>
            <TALLYREQUEST>Export</TALLYREQUEST>
            <TYPE>Collection</TYPE>
            <ID>BillsPayableRule37</ID>
        </HEADER>
        <BODY>
            <DESC>
                <STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES>
                <TDL><TDLMESSAGE>
                    <COLLECTION NAME="BillsPayableRule37">
                        <TYPE>Bill</TYPE>
                        <FETCH>Name, Parent, BillDate, ClosingBalance</FETCH>
                    </COLLECTION>
                </TDLMESSAGE></TDL>
            </DESC>
        </BODY>
    </ENVELOPE>"""

    req = urllib.request.Request(
        f"http://{host}:{port}",
        data=xml_envelope.encode("utf-8"),
        headers={"Content-Type": "text/xml;charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        xml_content = resp.read().decode("utf-8", errors="ignore")

    root = ET.fromstring(xml_content)
    today = date.today()
    overdue_bills = []
    est_tax_reversal = 0.0

    for bill in root.findall(".//BILL"):
        raw_dt = (bill.findtext("BILLDATE", "") or "").strip()
        try:
            bal = float(bill.findtext("CLOSINGBALANCE", "0") or 0.0)
        except ValueError:
            bal = 0.0
        # Credit payable closing balance in Tally is positive when payable (or negative depending on convention)
        if abs(bal) < 1.0:
            continue
        dt_obj = None
        if len(raw_dt) == 8 and raw_dt.isdigit():
            try:
                dt_obj = datetime.strptime(raw_dt, "%Y%m%d").date()
            except ValueError:
                dt_obj = None
        if dt_obj is None:
            continue
        age_days = (today - dt_obj).days
        if age_days > days_threshold:
            # Conservative 18/118th GST component estimate when bill is gross of 18% GST
            est_gst = round(abs(bal) * (18.0 / 118.0), 2)
            est_tax_reversal += est_gst
            overdue_bills.append({
                "bill_ref": (bill.attrib.get("NAME") or bill.findtext("NAME", "")).strip(),
                "party": (bill.findtext("PARENT", "") or "").strip(),
                "bill_date": dt_obj.isoformat(),
                "age_days": age_days,
                "unpaid_amount": round(abs(bal), 2),
                "estimated_gst_reversal": est_gst,
            })

    return {
        "overdue_bills_count": len(overdue_bills),
        "estimated_rule37_tax": round(est_tax_reversal, 2),
        "bills": overdue_bills,
    }


def sync_gstr9_full_fy_from_tally(
    fy: str = "2025-26",
    host: str = "localhost",
    port: int = 9000,
    simulate: bool = False,
) -> Dict[str, Any]:
    """
    One-click GSTR-9 Full-FY extraction from Tally Prime (or simulated enterprise Tally dataset):
      - Purchase Register (with Sec 17(5) / Rule 37 columns)
      - Sales & Credit/Debit Notes Register
      - Books GST Interest & Late Fee Expense Ledger Totals
      - >180-Day Overdue Creditor Bills Payable (Rule 37)
    Returns CSV payloads ready for automatic GSTR-9 form attachment.
    """
    if simulate:
        from app.engine.gstr9_sample import build_sample_gstr9_datasets
        ds = build_sample_gstr9_datasets(fy=fy)
        sales_df = ds["sales_df"]
        pur_df = ds["pur_df"]
        books_interest_total = float(ds.get("books_interest_total", 0.0))
        rule37_est = 9000.0
        overdue_cnt = 1
        company = f"Sample Enterprise Pvt Ltd (FY {fy})"
        source = "SIMULATED_TALLY_GSTR9_SYNC"
    else:
        status = check_tally_status(host, port, timeout=2.0)
        if not status.get("active"):
            raise ConnectionError("Tally Prime XML Server (Port 9000) is offline or not running.")
        company = status.get("company") or "Connected Tally Company"
        pur_df = fetch_purchase_vouchers_from_tally(host=host, port=port)
        sales_df = fetch_sales_vouchers_from_tally(host=host, port=port)
        try:
            exp = fetch_gst_expense_ledgers_from_tally(host=host, port=port)
            books_interest_total = round(float(exp.get("books_interest", 0.0)) + float(exp.get("books_late_fee", 0.0)), 2)
        except Exception:
            books_interest_total = 0.0
        try:
            r37 = fetch_overdue_payables_rule37_from_tally(host=host, port=port)
            rule37_est = float(r37.get("estimated_rule37_tax", 0.0))
            overdue_cnt = int(r37.get("overdue_bills_count", 0))
        except Exception:
            rule37_est = 0.0
            overdue_cnt = 0
        source = "LIVE_TALLY_GSTR9_SYNC"

    sales_csv = sales_df.to_csv(index=False) if sales_df is not None and not sales_df.empty else ""
    purchase_csv = pur_df.to_csv(index=False) if pur_df is not None and not pur_df.empty else ""

    return {
        "status": "success",
        "source": source,
        "fy": fy,
        "company": company,
        "sales_rows": int(len(sales_df)) if sales_df is not None else 0,
        "purchase_rows": int(len(pur_df)) if pur_df is not None else 0,
        "sales_csv": sales_csv,
        "purchase_csv": purchase_csv,
        "books_interest_late_fee": round(books_interest_total, 2),
        "rule37_tax": round(rule37_est, 2),
        "overdue_bills_count": overdue_cnt,
        "message": (
            f"Synced {int(len(sales_df)) if sales_df is not None else 0} Sales & "
            f"{int(len(pur_df)) if pur_df is not None else 0} Purchase vouchers from {company}."
        ),
    }
