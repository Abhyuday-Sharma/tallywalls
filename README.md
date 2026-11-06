# TallyWalls - GST Intelligence & Reconciliation Hub
**(GSTR-2B Statutory Portal vs. ERP Purchase Ledger Audit Engine)**

An enterprise-grade, automated reconciliation engine and interactive web application designed for Indian businesses and Chartered Accountants to reconcile government portal purchase statements (**GSTR-2B**) against internal accounting ledgers (**Tally Prime, Busy, SAP, Zoho Books**).

---

## Quick Start (1-Click Launch)

1. Double-click **`run_app.bat`** on your Windows desktop.
2. The server will start locally and automatically open the application in your browser at:
   ```
   http://127.0.0.1:8000
   ```
3. Click the **"Load Enterprise Demo"** button at the top right to instantly test all matching and discrepancy scenarios with realistic Indian business data.

---

## Key Capabilities & Problems Solved

1. **Direct GST Portal Bridge (`services.gst.gov.in/services/login`)**:
   * Automated Chromium browser bridge to navigate, select periods (including April - October multi-month batch download), and download statutory GSTR-2B Excel statements.
   * Credentials stored locally with AES-256 Fernet encryption.
2. **Recent Downloads Scanner**:
   * Scans local Downloads folder to auto-detect monthly or quarterly GSTR-2B Excel and Zip statements and consolidate them in one click.
3. **Live Tally Prime Connector (Port 9000)**:
   * Direct XML/HTTP connector to extract Purchase Vouchers directly from active Tally Prime company without manual Excel export.
4. **Invoice Number Normalization**:
   * Strips leading zeroes (`0049` on portal matches `49` in books).
   * Strips formatting noise like slashes, dashes, and prefixes (`INV/2023-24/108` matches `108`).
5. **Typo / Transposition Detection**:
   * When GSTIN and amounts match identically but invoice numbers differ (e.g. `49` vs `94`), the engine tags it as **Typo Match (Verify)** for quick accountant confirmation.
6. **ITC at Risk (Vendor Default)**:
   * Flags invoices present in internal books where the vendor defaulted on filing GSTR-1. Produces an automatic **Vendor Follow-up Sheet** to demand filing before releasing payments.
7. **Unbooked Invoices (Omitted Purchases)**:
   * Flags valid purchases appearing in GSTR-2B that internal accounts missed entering in ERP, preventing loss of legitimate tax credits.
8. **Statutory Tolerance Setting**:
   * Section 16(2) rounding tolerance threshold (default: +/- Rs. 1.00).

---

## Multi-Tier Matching Engine Rules

| Match Status | Category | Criteria | Business Impact |
| :--- | :--- | :--- | :--- |
| **Exact Match** | Clean Emerald Mint | GSTIN + Inv No + Taxable + Tax match 100%. | Zero risk; ready for full ITC claim in GSTR-3B. |
| **Normalized Match** | Soft Blue | Formatting resolved (stripped 0s, slashes, spaces). | Auto-accepted with audit remark. |
| **Typo Match** | Warm Amber / Yellow | Same GSTIN & Amount; invoice number digit inversion. | 1-click accountant verification. |
| **Value Mismatch** | Warm Peach / Orange | Same GSTIN & Inv No; taxable or tax differs by > Rs. 1.00. | Discrepancy amount displayed for debit/credit note check. |
| **Missing in 2B** | Soft Red | In Books, absent from 2B (Vendor non-filing). | **ITC at Risk.** Held from claim to avoid penalties. |
| **Missing in Books** | Soft Sky Cyan | In 2B, absent from Books (Omitted expense). | Pending entry in Tally to claim credit. |

---

## Project Structure

```
finance/
├── run_app.bat                      # 1-Click Windows desktop launcher
├── README.md                        # Documentation & user guide
├── requirements.txt                 # Python dependencies
├── gst_financial_terms_guide.html   # Standalone interactive handbook of statutory terms
├── test_reconciliation.py           # Core matching algorithm test suite
├── test_portal_bridge.py            # Portal automation & encryption tests
├── test_batch_workflow.py           # Multi-month (Apr-Oct) batch workflow tests
├── test_excel_ingest.py             # Official GST Portal Excel layout tests
│
├── app/
│   ├── main.py                      # FastAPI web server & API routes
│   ├── engine/
│   │   ├── column_mapper.py         # Smart column auto-detection for Tally & 2B
│   │   ├── normalizer.py            # Leading zero stripper, symbol cleaner & typo matcher
│   │   ├── reconciler.py            # 5-stage reconciliation pipeline & KPI calculator
│   │   ├── portal_bridge.py         # Portal automation, Playwright/Chromium & AES-256
│   │   ├── tally_connector.py       # Live Tally Prime XML server integration
│   │   └── excel_exporter.py        # OpenPyXL multi-sheet formatted workbook generator
│   └── static/
│       ├── index.html               # Audit vault dashboard interface
│       ├── css/style.css            # Responsive fintech styling & row highlight system
│       ├── js/app.js                # Dynamic filtering, live search, upload & export
│       └── img/                     # Brand logo, favicon, and mobile web icons
│
└── sample_data/
    ├── sample_gstr2b.xlsx           # Realistic government portal test file (B2B)
    └── sample_purchase_register.xlsx# Realistic Tally purchase register test file
```

---

## Exported Excel Workbook Structure

When clicking **"Export Audit Package (.xlsx)"**, the system generates an audit-ready multi-sheet workbook containing:
1. **Executive Summary**: High-level KPIs, total ITC, eligible credit, defaulting amounts, and reconciliation rate.
2. **Reconciled Ledger**: Complete side-by-side ledger with color-coded status badges and audit remarks.
3. **Vendor Follow-up (ITC at Risk)**: Ready-to-email sheet of defaulting vendors who have not filed GSTR-1.
4. **Unbooked Invoices**: List of portal bills missing from internal books for ERP voucher entry.

---

## Security & Privacy

* **100% Local Processing**: All reconciliation runs locally on your machine.
* **Encrypted Credentials**: Stored locally with AES-256 Fernet encryption and never sent over the network.
* No financial data or confidential invoice figures are ever transmitted to third-party cloud servers.
