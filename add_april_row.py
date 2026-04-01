import gspread
from google.oauth2.service_account import Credentials

# === CONFIGURATION ===
SPREADSHEET_NAME = "Your Spreadsheet Name"  # Update with your spreadsheet name
SHEET_INDEX = 0  # First sheet (tab index)
CREDENTIALS_FILE = "credentials.json"  # Path to your Google service account JSON key

APRIL_ROW = 38  # Insert after March (row 37)

# === AUTH & CONNECT ===
scopes = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
client = gspread.authorize(creds)

spreadsheet = client.open(SPREADSHEET_NAME)
sheet = spreadsheet.get_worksheet(SHEET_INDEX)

# === INSERT APRIL ROW WITH FORMULA ===
sheet.insert_row(
    ["April", "", "$1.50", "=B38*C38"],
    index=APRIL_ROW,
    value_input_option="USER_ENTERED",
)

print(f"April row inserted at row {APRIL_ROW} with Total formula (=B38*C38).")
print("Just fill in the Minutes column (B38) and the total will calculate automatically.")
