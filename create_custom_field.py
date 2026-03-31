"""
Create a custom field "Abandoned Reason" on all HighLevel subaccounts (locations).

Object: Opportunity
Group: Case Details
Field type: SINGLE_OPTIONS (dropdown)

Usage:
    # Using Agency API key (recommended - fetches all subaccounts automatically):
    python create_custom_field.py --agency-api-key YOUR_AGENCY_API_KEY

    # Using a specific location API key (creates on that single location only):
    python create_custom_field.py --location-api-key YOUR_LOCATION_API_KEY --location-id LOC_ID

    # Using Company API key with location IDs:
    python create_custom_field.py --company-api-key YOUR_COMPANY_API_KEY --location-ids loc1,loc2,loc3
"""

import argparse
import re
import sys
import time

import requests

BASE_URL = "https://services.leadconnectorhq.com"

# ── Custom Field Definition ──────────────────────────────────────────────────

FIELD_NAME = "Abandoned Reason"
FIELD_OBJECT = "opportunity"
FIELD_DATATYPE = "SINGLE_OPTIONS"
FOLDER_NAME = "Case Details"

# Each option: "name" is the display label, "value" is the internal key.
FIELD_OPTIONS = [
    {
        "name": "Person is still alive (estate planning / POA / guardianship / conservatorship)",
        "value": "person_is_still_alive",
    },
    {
        "name": "Decedent resided out of state (firm not licensed in that jurisdiction)",
        "value": "decedent_resided_out_of_state",
    },
    {
        "name": "Only assets are a vehicle and/or personal property (no real estate, no financial accounts)",
        "value": "only_assets_vehicle_or_personal_property",
    },
    {
        "name": "Caller wants one specific item only (car keys, gun, personal belonging)",
        "value": "caller_wants_one_specific_item",
    },
    {
        "name": "Family — no legal standing",
        "value": "family_no_legal_standing",
    },
    {
        "name": "Non-family — no legal standing",
        "value": "non_family_no_legal_standing",
    },
    {
        "name": "Services requested not offered by the firm (civil case, non-probate matter)",
        "value": "services_not_offered_by_firm",
    },
    {
        "name": "Language barrier — unable to qualify",
        "value": "language_barrier",
    },
    {
        "name": "Contested case — client expected win/settlement below $100K, doesn't justify $12K retainer",
        "value": "contested_case_below_100k",
    },
    {
        "name": "Court deadline too soon (within 14 days, can't onboard in time)",
        "value": "court_deadline_too_soon",
    },
    {
        "name": "Caller has limited/no info about assets but is an interested party",
        "value": "caller_limited_no_info_about_assets",
    },
]

# ── API Helpers ──────────────────────────────────────────────────────────────


def get_headers(api_key: str, version: str = "2021-07-28") -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Version": version,
    }


def fetch_locations(api_key: str) -> list[dict]:
    """Fetch all sub-account locations using the Agency/Company API key."""
    url = f"{BASE_URL}/locations/search"
    headers = get_headers(api_key)
    locations = []
    skip = 0
    limit = 100

    while True:
        params = {"skip": skip, "limit": limit}
        resp = requests.get(url, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("locations", [])
        if not batch:
            break
        locations.extend(batch)
        if len(batch) < limit:
            break
        skip += limit
        time.sleep(0.3)

    return locations


def get_or_create_folder(api_key: str, location_id: str, folder_name: str = FOLDER_NAME) -> str:
    """
    Find an existing custom field folder by name, or create one.
    Returns the folder ID.
    """
    headers = get_headers(api_key)

    # 1. List existing custom fields and look for a folder with the right name
    url = f"{BASE_URL}/locations/{location_id}/customFields"
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    data = resp.json()

    for cf in data.get("customFields", []):
        if cf.get("model") == "opportunity":
            group = cf.get("group", {})
            if isinstance(group, dict) and group.get("name") == folder_name:
                return group["id"]

    # 2. Not found — create it
    folder_url = f"{BASE_URL}/locations/{location_id}/customFields/folder"
    payload = {
        "name": folder_name,
        "model": FIELD_OBJECT,
    }
    resp = requests.post(folder_url, headers=headers, json=payload)
    resp.raise_for_status()
    result = resp.json()
    folder_id = result.get("folder", {}).get("id") or result.get("id")
    if not folder_id:
        raise RuntimeError(f"Failed to create folder '{folder_name}': {result}")
    return folder_id


def create_custom_field_for_location(api_key: str, location_id: str, location_name: str = "") -> dict:
    """
    Create the Abandoned Reason custom field on a single location.
    First ensures the "Case Details" folder exists, then creates the field in it.
    """
    headers = get_headers(api_key)
    label = f"[{location_name or location_id}]"
    url = f"{BASE_URL}/locations/{location_id}/customFields"

    # Check if field already exists to avoid duplicates
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    existing = resp.json().get("customFields", [])

    for cf in existing:
        if cf.get("name") == FIELD_NAME and cf.get("dataType") == FIELD_DATATYPE:
            print(f"  ⏭  {label} — '{FIELD_NAME}' already exists (id: {cf['id']}), skipping.")
            return {"status": "exists", "location_id": location_id, "field_id": cf["id"]}

    # Ensure "Case Details" folder exists and get its ID
    folder_id = get_or_create_folder(api_key, location_id)
    print(f"  📁 {label} — Using folder '{FOLDER_NAME}' (id: {folder_id})")

    # Build the payload with the folder ID
    payload = {
        "name": FIELD_NAME,
        "dataType": FIELD_DATATYPE,
        "model": FIELD_OBJECT,
        "group": folder_id,
        "placeholder": "Select abandoned reason",
        "options": FIELD_OPTIONS,
    }

    resp = requests.post(url, headers=headers, json=payload)
    resp.raise_for_status()
    result = resp.json()
    field_id = result.get("customField", {}).get("id", "unknown")
    print(f"  ✅ {label} — Created '{FIELD_NAME}' (id: {field_id})")
    return {"status": "created", "location_id": location_id, "field_id": field_id}


# ── Main ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Create the 'Abandoned Reason' custom field on HighLevel subaccounts."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--agency-api-key",
        help="Agency-level API key — will auto-discover all subaccounts.",
    )
    group.add_argument(
        "--company-api-key",
        help="Company API key — requires --location-ids.",
    )
    group.add_argument(
        "--location-api-key",
        help="Single location API key — requires --location-id.",
    )
    parser.add_argument(
        "--location-id",
        help="Single location ID (used with --location-api-key).",
    )
    parser.add_argument(
        "--location-ids",
        help="Comma-separated location IDs (used with --company-api-key).",
    )
    args = parser.parse_args()

    # Resolve which API key + locations to use
    if args.agency_api_key:
        api_key = args.agency_api_key
        print("Fetching all subaccount locations...")
        locations = fetch_locations(api_key)
        if not locations:
            print("No locations found. Check your Agency API key permissions.")
            sys.exit(1)
        print(f"Found {len(locations)} location(s).\n")
        for loc in locations:
            loc_id = loc["id"]
            loc_name = loc.get("name", loc_id)
            try:
                create_custom_field_for_location(api_key, loc_id, loc_name)
            except requests.HTTPError as e:
                print(f"  ❌ [{loc_name}] — Error: {e.response.status_code} {e.response.text}")
            time.sleep(0.3)

    elif args.company_api_key:
        api_key = args.company_api_key
        if not args.location_ids:
            print("Error: --location-ids is required with --company-api-key")
            sys.exit(1)
        loc_ids = [lid.strip() for lid in args.location_ids.split(",")]
        print(f"Creating field on {len(loc_ids)} location(s)...\n")
        for loc_id in loc_ids:
            try:
                create_custom_field_for_location(api_key, loc_id)
            except requests.HTTPError as e:
                print(f"  ❌ [{loc_id}] — Error: {e.response.status_code} {e.response.text}")
            time.sleep(0.3)

    elif args.location_api_key:
        api_key = args.location_api_key
        if not args.location_id:
            print("Error: --location-id is required with --location-api-key")
            sys.exit(1)
        try:
            create_custom_field_for_location(api_key, args.location_id)
        except requests.HTTPError as e:
            print(f"  ❌ Error: {e.response.status_code} {e.response.text}")
            sys.exit(1)

    print("\nDone.")


if __name__ == "__main__":
    main()
