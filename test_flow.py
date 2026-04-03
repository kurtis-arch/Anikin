#!/usr/bin/env python3
"""Manual test script for the Aircall-GHL transcript integration.

Searches GHL for a contact by name, finds their Aircall calls,
pulls ALL transcripts, creates PDFs, posts notes, and sends Slack DMs.

Usage:
    python test_flow.py "Caroline Dunno"
    python test_flow.py "Caroline Dunno" --latest   # Only the most recent call
"""

import sys
import time
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

from app import (
    ghl_headers,
    GHL_BASE_URL,
    search_calls_by_phone,
    get_transcript,
    get_call_summary,
    format_transcript,
    extract_summary_text,
    create_transcript_pdf,
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
    contact_name = sys.argv[1] if len(sys.argv) > 1 else "Caroline Dunno"
    latest_only = "--latest" in sys.argv

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
    for c in calls[:10]:
        started = c.get("started_at", 0)
        date_str = datetime.fromtimestamp(started).strftime("%Y-%m-%d %H:%M") if started else "N/A"
        duration = c.get("duration", 0)
        direction = c.get("direction", "N/A")
        print(f"   - Call {c['id']}: {date_str} | {direction} | {duration}s")

    # If --latest, only process the most recent call
    calls_to_process = [calls[0]] if latest_only else calls
    print(f"\n   Processing {len(calls_to_process)} call(s)...")

    opportunity_name = "Test Opportunity"
    processed = 0

    for call in calls_to_process:
        call_id = call.get("id")
        started = call.get("started_at", 0)
        duration = call.get("duration", 0)
        call_date_str = datetime.fromtimestamp(started).strftime("%Y-%m-%d %H:%M") if started else "Unknown"

        if duration < 5:
            print(f"\n   Skipping call {call_id} (too short: {duration}s)")
            continue

        print(f"\n--- Call {call_id} ({call_date_str}, {duration}s) ---")

        # Get transcript
        print(f"   Fetching transcript...")
        transcript_data = get_transcript(call_id)
        if not transcript_data:
            print("   No transcript available, skipping")
            continue

        transcript_text = format_transcript(transcript_data)
        print(f"   Got transcript ({len(transcript_text)} chars)")

        # Get summary
        print(f"   Fetching AI summary...")
        summary_data = get_call_summary(call_id)
        summary_text = extract_summary_text(summary_data)
        if summary_text:
            print(f"   Got summary ({len(summary_text)} chars)")
        else:
            print("   No summary available")

        # Create PDF
        print(f"   Creating PDF...")
        pdf_title = f"Transcript – {full_name} – {call_date_str}"
        pdf_url = create_transcript_pdf(
            title=pdf_title,
            transcript_text=transcript_text,
            call_info=call,
            contact_name=full_name,
            opportunity_name=opportunity_name,
            summary_text=summary_text,
        )
        print(f"   PDF: {pdf_url}")

        # Post note to GHL
        print(f"   Posting note to GHL...")
        note_body = f"Aircall Transcript ({call_date_str}): {pdf_url}"
        ghl_create_note(contact_id, note_body)
        print("   Note posted!")

        # Slack notification
        print(f"   Sending Slack notification...")
        send_slack_notification(full_name, opportunity_name, pdf_url, phone=phone, call_date=call_date_str)
        print("   Sent!")

        processed += 1
        time.sleep(0.5)

    print(f"\n=== Test Complete: {processed} transcript(s) processed ===")


if __name__ == "__main__":
    main()
