#!/usr/bin/env python3
"""Quick diagnostic to test Notion API connectivity."""
import sys
import requests

TOKEN = sys.argv[1]
PAGE_ID = sys.argv[2]
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}

print("=== NOTION API DIAGNOSTIC ===", flush=True)
print(f"Token: {TOKEN[:8]}...{TOKEN[-4:]}", flush=True)
print(f"Page ID: {PAGE_ID}", flush=True)

# Test 1: Auth
print("\n[1] Testing authentication...", flush=True)
r = requests.get("https://api.notion.com/v1/users/me", headers=HEADERS, timeout=10)
print(f"  Status: {r.status_code}", flush=True)
print(f"  Body: {r.text[:500]}", flush=True)
if r.status_code != 200:
    sys.exit(f"Auth failed with status {r.status_code}")

# Test 2: Page access
print("\n[2] Testing page access...", flush=True)
r = requests.get(f"https://api.notion.com/v1/pages/{PAGE_ID}", headers=HEADERS, timeout=10)
print(f"  Status: {r.status_code}", flush=True)
print(f"  Body: {r.text[:500]}", flush=True)
if r.status_code != 200:
    sys.exit(f"Page access failed with status {r.status_code}")

# Test 3: Create a simple DB
print("\n[3] Testing database creation...", flush=True)
payload = {
    "parent": {"type": "page_id", "page_id": PAGE_ID},
    "title": [{"type": "text", "text": {"content": "Test DB"}}],
    "properties": {"Name": {"title": {}}},
}
r = requests.post("https://api.notion.com/v1/databases", headers=HEADERS, json=payload, timeout=10)
print(f"  Status: {r.status_code}", flush=True)
print(f"  Body: {r.text[:500]}", flush=True)

if r.status_code == 200:
    db_id = r.json()["id"]
    print(f"\n  SUCCESS! Test DB created: {db_id}", flush=True)
    # Clean up - archive it
    requests.patch(f"https://api.notion.com/v1/databases/{db_id}", headers=HEADERS,
                   json={"archived": True}, timeout=10)
    print("  Cleaned up test DB.", flush=True)

print("\n=== DIAGNOSTIC COMPLETE ===", flush=True)
