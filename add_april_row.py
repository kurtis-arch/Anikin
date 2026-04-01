import gspread
from google.oauth2.service_account import Credentials

# === CONFIGURATION ===
SPREADSHEET_NAME = "Your Spreadsheet Name"  # Update with your spreadsheet name
SHEET_INDEX = 0  # First sheet (tab index)
CREDENTIALS_FILE = "credentials.json"  # Path to your Google service account JSON key

# April data to insert
APRIL_ROW = 38  # Insert after March (row 37), before the blank row
APRIL_DATA = {
    "month": "April",
    "minutes": 0,       # Update with actual April minutes
    "price": 1.50,
    "total_formula": "=B38*C38",  # Auto-calculates Minutes * Price
}

# === AUTH & CONNECT ===
scopes = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
client = gspread.authorize(creds)

spreadsheet = client.open(SPREADSHEET_NAME)
sheet = spreadsheet.get_worksheet(SHEET_INDEX)

# === INSERT APRIL ROW ===
# Insert a new row at position 38 (pushes Total row down to 40)
sheet.insert_row(
    [APRIL_DATA["month"], APRIL_DATA["minutes"], APRIL_DATA["price"], APRIL_DATA["total_formula"]],
    index=APRIL_ROW,
    value_input_option="USER_ENTERED",  # So the formula gets evaluated
)

# === UPDATE TOTAL ROW ===
# Total row shifted from 39 -> 40 after insert
# Update the Minutes total (B40) and Cost total (D40) to include the new row
sheet.update("B40", "=SUM(B27:B38)", value_input_option="USER_ENTERED")
sheet.update("D40", "=SUM(D27:D38)", value_input_option="USER_ENTERED")

print("April row added successfully!")
print(f"  Month: {APRIL_DATA['month']}")
print(f"  Minutes: {APRIL_DATA['minutes']}")
print(f"  Price: ${APRIL_DATA['price']:.2f}")
print("  Total: auto-calculated via formula")
print("Total row (row 40) formulas updated to include April.")
