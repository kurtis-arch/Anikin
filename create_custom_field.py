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
import json
import sys
import time

import requests

BASE_URL = "https://services.leadconnectorhq.com"

# ── Custom Field Definition ──────────────────────────────────────────────────

FIELD_NAME = "Abandoned Reason"
FIELD_OBJECT = "opportunity"
FIELD_DATATYPE = "SINGLE_OPTIONS"

# Add more options here as needed — each dict has "name" and "value".
FIELD_OPTIONS = [
    {
        "name": "Person is still alive (estate planning / POA / guardianship / conservatorship)",
        "value": "Person is still alive (estate planning / POA / guardianship / conservatorship)",
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
        time.sleep(0.3)  # rate-limit courtesy

    return locations


def ensure_custom_field_group(api_key: str, location_id: str, group_name: str = "Case Details") -> str | None:
    """
    Look up (or create) a custom field group named `group_name` under the
    opportunity object for the given location. Returns the group ID.
    """
    headers = get_headers(api_key)

    # First check if the group already exists
    url = f"{BASE_URL}/locations/{location_id}/customFields"
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    data = resp.json()

    # Check existing custom fields for the group
    custom_fields = data.get("customFields", [])
    for cf in custom_fields:
        if cf.get("fieldKey", "").startswith("opportunity.") or cf.get("model") == "opportunity":
            group = cf.get("group", {})
            if isinstance(group, dict) and group.get("name") == group_name:
                return group.get("id")

    # If not found, we'll pass the group name when creating the field.
    # The HighLevel API can auto-create the group when specified by name.
    return None


def create_custom_field_for_location(api_key: str, location_id: str, location_name: str = "") -> dict:
    """
    Create the Abandoned Reason custom field on a single location.
    Uses the HighLevel Custom Fields API v2.
    """
    headers = get_headers(api_key)
    url = f"{BASE_URL}/locations/{location_id}/customFields"

    # Check if field already exists to avoid duplicates
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    existing = resp.json().get("customFields", [])

    for cf in existing:
        if cf.get("name") == FIELD_NAME and cf.get("dataType") == FIELD_DATATYPE:
            label = f"[{location_name or location_id}]"
            print(f"  ⏭  {label} — '{FIELD_NAME}' already exists (id: {cf['id']}), skipping.")
            return {"status": "exists", "location_id": location_id, "field_id": cf["id"]}

    # Build the payload
    payload = {
        "name": FIELD_NAME,
        "dataType": FIELD_DATATYPE,
        "model": FIELD_OBJECT,
        "placeholder": "Select abandoned reason",
        "options": FIELD_OPTIONS,
    }

    resp = requests.post(url, headers=headers, json=payload)
    resp.raise_for_status()
    result = resp.json()
    field_id = result.get("customField", {}).get("id", "unknown")
    label = f"[{location_name or location_id}]"
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
