import gspread
from google.oauth2.service_account import Credentials

# === CONFIGURATION ===
SPREADSHEET_NAME = "Your Spreadsheet Name"  # Update with your spreadsheet name
SHEET_INDEX = 0  # First sheet (tab index)
CREDENTIALS_FILE = "credentials.json"  # Path to your Google service account JSON key

OUTBOUND_APRIL_ROW = 38  # Insert after March (row 37) in Outbound Calls
INBOUND_APRIL_ROW = 52   # Insert after last entry (row 51) in Inbound Calls Purchased
                          # Note: becomes row 53 after the outbound row insert shifts everything down

# === AUTH & CONNECT ===
scopes = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
client = gspread.authorize(creds)

spreadsheet = client.open(SPREADSHEET_NAME)
sheet = spreadsheet.get_worksheet(SHEET_INDEX)

# === 1. INSERT APRIL ROW IN OUTBOUND CALLS ===
sheet.insert_row(
    ["April", "", "$1.50", "=B38*C38"],
    index=OUTBOUND_APRIL_ROW,
    value_input_option="USER_ENTERED",
)
print(f"Outbound Calls: April row inserted at row {OUTBOUND_APRIL_ROW} with formula =B38*C38")

# === 2. INSERT APRIL ROW IN INBOUND CALLS PURCHASED ===
# After the outbound insert, everything below shifts down by 1
inbound_row = INBOUND_APRIL_ROW + 1  # 52 -> 53
sheet.insert_row(
    ["01/04/2026", "", "", "=B53*C53"],
    index=inbound_row,
    value_input_option="USER_ENTERED",
)
print(f"Inbound Calls: April row inserted at row {inbound_row} with formula =B53*C53")
print("\nFill in the blank cells (minutes, inbound calls, price) and totals will auto-calculate.")
