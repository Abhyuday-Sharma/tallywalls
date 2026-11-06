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
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sample_file = os.path.join(
        project_root,
        "sample_data",
        "sample_purchase_register.xlsx"
    )
    if os.path.exists(sample_file):
        return pd.read_excel(sample_file, sheet_name="Purchase Register")
    return pd.DataFrame()
