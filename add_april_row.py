import gspread
from google.oauth2.service_account import Credentials

# === CONFIGURATION ===
SPREADSHEET_NAME = "Your Spreadsheet Name"  # Update with your spreadsheet name
SHEET_INDEX = 0  # First sheet (tab index)
CREDENTIALS_FILE = "credentials.json"  # Path to your Google service account JSON key

# Original row positions (before any inserts)
OUTBOUND_CALLS_ROW = 38       # After March (row 37)
INBOUND_PURCHASED_ROW = 58    # After 26/03/2026 (row 57)
OUTBOUND_PURCHASED_ROW = 73   # After 26/03/2026 (row 72)
AVG_MONTHLY_COSTS_ROW = 93    # After February (row 92)

# === AUTH & CONNECT ===
scopes = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
client = gspread.authorize(creds)

spreadsheet = client.open(SPREADSHEET_NAME)
sheet = spreadsheet.get_worksheet(SHEET_INDEX)

# Each insert shifts all rows below by 1, so we track the offset
offset = 0

# === 1. OUTBOUND CALLS — row 38 ===
row = OUTBOUND_CALLS_ROW + offset
sheet.insert_row(
    ["April", "", "$1.50", f"=B{row}*C{row}"],
    index=row,
    value_input_option="USER_ENTERED",
)
print(f"1. Outbound Calls: April row inserted at row {row}")
offset += 1

# === 2. INBOUND CALLS PURCHASED — row 59 (after +1 offset) ===
row = INBOUND_PURCHASED_ROW + offset
sheet.insert_row(
    ["01/04/2026", "", "", f"=B{row}*C{row}"],
    index=row,
    value_input_option="USER_ENTERED",
)
print(f"2. Inbound Calls Purchased: April row inserted at row {row}")
offset += 1

# === 3. OUTBOUND MINUTES PURCHASED — row 75 (after +2 offset) ===
row = OUTBOUND_PURCHASED_ROW + offset
sheet.insert_row(
    ["01/04/2026", "", "1.5", f"=B{row}*C{row}"],
    index=row,
    value_input_option="USER_ENTERED",
)
print(f"3. Outbound Minutes Purchased: April row inserted at row {row}")
offset += 1

# === 4. AVERAGE MONTHLY COSTS — row 96 (after +3 offset) ===
row = AVG_MONTHLY_COSTS_ROW + offset
sheet.insert_row(
    ["April", "", "", f"=B{row}+C{row}"],
    index=row,
    value_input_option="USER_ENTERED",
)
print(f"4. Average Monthly Costs: April row inserted at row {row}")

print("\nDone! Fill in the blank cells and all totals will auto-calculate.")
