#!/usr/bin/env python3
"""Manual test script for the Aircall-GHL transcript integration.

Searches GHL for a contact by name, finds their Aircall calls,
pulls the transcript, creates a Google Doc, posts it as a GHL note,
and sends a Slack DM.

Usage:
    python test_flow.py "Zachary Smith"
"""

import sys
import time

from dotenv import load_dotenv

load_dotenv()

from app import (
    ghl_headers,
    GHL_BASE_URL,
    search_calls_by_phone,
    find_nearest_call,
    get_transcript,
    format_transcript,
    create_transcript_doc,
    ghl_create_note,
    send_slack_notification,
    log,
)
import requests


def search_ghl_contact(name):
    """Search GHL for a contact by name."""
    resp = requests.get(
        f"{GHL_BASE_URL}/contacts/",
        headers=ghl_headers(),
        params={"query": name, "limit": 5},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    contacts = data.get("contacts", [])
    return contacts


def main():
    contact_name = sys.argv[1] if len(sys.argv) > 1 else "Zachary Smith"

    print(f"=== Testing Transcript Flow for: {contact_name} ===\n")

    # Step 1: Search GHL for the contact
    print(f"1. Searching GHL for '{contact_name}'...")
    contacts = search_ghl_contact(contact_name)
    if not contacts:
        print("   No contacts found in GHL!")
        sys.exit(1)

    contact = contacts[0]
    contact_id = contact.get("id")
    phone = contact.get("phone")
    first = contact.get("firstName", "")
    last = contact.get("lastName", "")
    full_name = f"{first} {last}".strip()
    print(f"   Found: {full_name} (ID: {contact_id})")
    print(f"   Phone: {phone}")

    if not phone:
        print("   No phone number on this contact!")
        sys.exit(1)

    # Step 2: Search Aircall for calls to this number
    print(f"\n2. Searching Aircall for calls to {phone}...")
    calls = search_calls_by_phone(phone)
    if not calls:
        print("   No Aircall calls found for this number!")
        sys.exit(1)

    print(f"   Found {len(calls)} calls")
    for c in calls[:5]:
        from datetime import datetime
        started = c.get("started_at", 0)
        date_str = datetime.fromtimestamp(started).strftime("%Y-%m-%d %H:%M") if started else "N/A"
        duration = c.get("duration", 0)
        direction = c.get("direction", "N/A")
        print(f"   - Call {c['id']}: {date_str} | {direction} | {duration}s")

    # Step 3: Pick the most recent call
    nearest_call = calls[0]  # Already sorted desc
    call_id = nearest_call.get("id")
    print(f"\n3. Using most recent call: {call_id}")

    # Step 4: Get transcript
    print(f"\n4. Fetching transcript for call {call_id}...")
    transcript_data = get_transcript(call_id)
    if not transcript_data:
        print("   No transcript available for this call!")
        print("   (AI Assist may not be enabled on this Aircall account)")
        sys.exit(1)

    transcript_text = format_transcript(transcript_data)
    print(f"   Got transcript ({len(transcript_text)} chars)")
    print(f"   Preview: {transcript_text[:200]}...")

    # Step 5: Create Google Doc
    print(f"\n5. Creating Google Doc...")
    opportunity_name = "Test Opportunity"
    doc_title = f"Transcript – {full_name} – {opportunity_name}"
    doc_url = create_transcript_doc(
        title=doc_title,
        transcript_text=transcript_text,
        call_info=nearest_call,
        contact_name=full_name,
        opportunity_name=opportunity_name,
    )
    print(f"   Doc created: {doc_url}")

    # Step 6: Post note to GHL
    print(f"\n6. Posting note to GHL contact...")
    note_body = (
        f"Opportunity Won: {opportunity_name}\n"
        f"Contact: {full_name} ({phone})\n\n"
        f"Aircall Transcript: {doc_url}"
    )
    ghl_create_note(contact_id, note_body)
    print("   Note posted!")

    # Step 7: Send Slack notification
    print(f"\n7. Sending Slack notification...")
    send_slack_notification(full_name, opportunity_name, doc_url, phone=phone)
    print("   Slack notification sent!")

    print(f"\n=== Test Complete ===")
    print(f"Google Doc: {doc_url}")


if __name__ == "__main__":
    main()
