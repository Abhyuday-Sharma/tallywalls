import sys
import os
import io
import zipfile
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

from app.engine.portal_bridge import parse_gstr2b_excel, ingest_gstr2b_file

print("Testing Excel Parser with official GST Portal layouts...")

# 1. Test clean sample file
df_clean = ingest_gstr2b_file("sample_data/sample_gstr2b.xlsx")
assert len(df_clean) == 7
assert "GSTIN of supplier" in df_clean.columns
print("[PASSED] Standard B2B Excel sheet ingested successfully.")

# 2. Test official GST Portal multi-row metadata header layout
portal_metadata_rows = [
    ["GSTIN of the Taxpayer", "06AAGCH3494L1Z6", None, None, None, None, None, None, None, None, None],
    ["Legal Name", "HIGHMINDS ADTECH PRIVATE LIMITED", None, None, None, None, None, None, None, None, None],
    ["Trade Name", "HIGHMINDS ADTECH PR", None, None, None, None, None, None, None, None, None],
    ["Financial Year", "2026-27", "Tax Period", "April", None, None, None, None, None, None, None],
    ["B2B - Taxable inward supplies received from registered persons", None, None, None, None, None, None, None, None, None, None],
    ["GSTIN of supplier", "Trade/Legal name", "Invoice number", "Invoice Date", "Invoice Value (₹)", "Taxable Value (₹)", "Integrated Tax (₹)", "Central Tax (₹)", "State/UT Tax (₹)", "Cess (₹)", "ITC Availability"],
    ["07AAACU1234F1Z5", "UltraTech Cement Ltd", "UTC/2026/101", "2026-04-10", 192000.0, 150000.0, 42000.0, 0.0, 0.0, 0.0, "Yes"],
    ["Total", None, None, None, 192000.0, 150000.0, 42000.0, 0.0, 0.0, 0.0, None]
]

test_portal_file = "sample_data/test_portal_metadata.xlsx"
df_portal_raw = pd.DataFrame(portal_metadata_rows)
with pd.ExcelWriter(test_portal_file, engine="openpyxl") as writer:
    df_portal_raw.to_excel(writer, sheet_name="B2B", header=False, index=False)

df_portal = ingest_gstr2b_file(test_portal_file)
assert len(df_portal) == 1, f"Expected 1 data row, got {len(df_portal)}"
assert "GSTIN of supplier" in df_portal.columns
assert df_portal.iloc[0]["Invoice number"] == "UTC/2026/101"
print("[PASSED] Official GST Portal multi-row metadata layout auto-detected and parsed!")

# 3. Test Zip containing Excel file
test_zip_file = "sample_data/test_gstr2b_excel.zip"
with zipfile.ZipFile(test_zip_file, "w") as zf:
    zf.write(test_portal_file, arcname="GSTR2B_06AAGCH3494L1Z6_042026.xlsx")

df_zip = ingest_gstr2b_file(test_zip_file)
assert len(df_zip) == 1
assert df_zip.iloc[0]["GSTIN of supplier"] == "07AAACU1234F1Z5"
print("[PASSED] Zip file containing Excel statement (.xlsx) parsed seamlessly!")

# Clean up temporary test files
if os.path.exists(test_portal_file):
    os.remove(test_portal_file)
if os.path.exists(test_zip_file):
    os.remove(test_zip_file)

print("ALL EXCEL INGESTION TESTS PASSED 100%!")

