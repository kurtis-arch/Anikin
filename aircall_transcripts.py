#!/usr/bin/env python3
"""Aircall Transcript Extractor

Extracts call transcripts from the Aircall API using Basic Auth.
Credentials are read from a .env file (AIRCALL_API_ID, AIRCALL_API_TOKEN).

Usage:
    python aircall_transcripts.py                  # Extract all transcripts
    python aircall_transcripts.py --from 2026-01-01 --to 2026-03-30
    python aircall_transcripts.py --call-id 123456 # Single call transcript
    python aircall_transcripts.py --list-calls     # Just list calls
"""

import argparse
import base64
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api.aircall.io/v1"
RATE_LIMIT_DELAY = 0.5  # seconds between requests to avoid rate limiting


def get_auth_header():
    api_id = os.getenv("AIRCALL_API_ID")
    api_token = os.getenv("AIRCALL_API_TOKEN")
    if not api_id or not api_token:
        print("Error: AIRCALL_API_ID and AIRCALL_API_TOKEN must be set in .env")
        sys.exit(1)
    credentials = base64.b64encode(f"{api_id}:{api_token}".encode()).decode()
    return {"Authorization": f"Basic {credentials}"}


def api_get(endpoint, params=None):
    """Make a GET request to the Aircall API with retry logic."""
    url = endpoint if endpoint.startswith("https://") else f"{BASE_URL}{endpoint}"
    headers = get_auth_header()

    for attempt in range(4):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
                print(f"  Rate limited. Retrying in {retry_after}s...")
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            if attempt < 3:
                wait = 2 ** (attempt + 1)
                print(f"  Request failed: {e}. Retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise
    return None


def list_calls(from_ts=None, to_ts=None):
    """Fetch all calls with pagination."""
    params = {"per_page": 50, "order": "desc"}
    if from_ts:
        params["from"] = from_ts
    if to_ts:
        params["to"] = to_ts

    all_calls = []
    page = 1

    while True:
        params["page"] = page
        print(f"  Fetching calls page {page}...")
        data = api_get("/calls", params=params)
        if not data or "calls" not in data:
            break

        calls = data["calls"]
        all_calls.extend(calls)

        meta = data.get("meta", {})
        total = meta.get("total", 0)
        print(f"  Got {len(all_calls)}/{total} calls")

        if not meta.get("next_page_link"):
            break
        page += 1
        time.sleep(RATE_LIMIT_DELAY)

    return all_calls


def get_transcript(call_id):
    """Fetch transcript for a single call."""
    try:
        data = api_get(f"/calls/{call_id}/transcription")
        return data
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return None
        raise


def format_transcript(transcript_data, call_info=None):
    """Format transcript data into readable text."""
    lines = []

    if call_info:
        lines.append(f"Call ID: {call_info.get('id', 'N/A')}")
        lines.append(f"Direction: {call_info.get('direction', 'N/A')}")
        lines.append(f"Duration: {call_info.get('duration', 0)}s")
        started = call_info.get("started_at")
        if started:
            lines.append(f"Date: {datetime.fromtimestamp(started).isoformat()}")
        user = call_info.get("user")
        if user:
            lines.append(f"Agent: {user.get('name', 'N/A')}")
        contact = call_info.get("contact")
        if contact:
            name = f"{contact.get('first_name', '')} {contact.get('last_name', '')}".strip()
            lines.append(f"Contact: {name or 'N/A'}")
        lines.append("-" * 60)

    # Handle various transcript response formats
    if not transcript_data:
        lines.append("[No transcript available]")
        return "\n".join(lines)

    # The transcript may be nested under different keys
    transcript = (
        transcript_data.get("transcription")
        or transcript_data.get("transcript")
        or transcript_data
    )

    if isinstance(transcript, list):
        for segment in transcript:
            speaker = segment.get("speaker", segment.get("role", "Unknown"))
            text = segment.get("text", segment.get("content", ""))
            ts = segment.get("timestamp", segment.get("start", ""))
            if ts:
                lines.append(f"[{ts}] {speaker}: {text}")
            else:
                lines.append(f"{speaker}: {text}")
    elif isinstance(transcript, dict):
        # Could be a dict with segments/turns
        segments = (
            transcript.get("segments")
            or transcript.get("turns")
            or transcript.get("content")
        )
        if isinstance(segments, list):
            for segment in segments:
                speaker = segment.get("speaker", segment.get("role", "Unknown"))
                text = segment.get("text", segment.get("content", ""))
                lines.append(f"{speaker}: {text}")
        elif isinstance(transcript, dict) and "text" in transcript:
            lines.append(transcript["text"])
        else:
            lines.append(json.dumps(transcript, indent=2))
    elif isinstance(transcript, str):
        lines.append(transcript)
    else:
        lines.append(json.dumps(transcript_data, indent=2))

    return "\n".join(lines)


def save_transcript(call_id, content, output_dir):
    """Save a transcript to a file."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / f"call_{call_id}_transcript.txt"
    filepath.write_text(content, encoding="utf-8")
    return filepath


def parse_date(date_str):
    """Parse a date string to a UNIX timestamp."""
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return int(datetime.strptime(date_str, fmt).timestamp())
        except ValueError:
            continue
    print(f"Error: Could not parse date '{date_str}'. Use YYYY-MM-DD format.")
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Extract transcripts from Aircall API")
    parser.add_argument("--call-id", type=int, help="Fetch transcript for a specific call ID")
    parser.add_argument("--list-calls", action="store_true", help="List calls without fetching transcripts")
    parser.add_argument("--from", dest="from_date", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--to", dest="to_date", help="End date (YYYY-MM-DD)")
    parser.add_argument("--output", default="transcripts", help="Output directory (default: transcripts)")
    parser.add_argument("--json", action="store_true", help="Also save raw JSON responses")
    args = parser.parse_args()

    print("Aircall Transcript Extractor")
    print("=" * 40)

    # Verify credentials work
    print("Verifying API credentials...")
    try:
        company = api_get("/company")
        print(f"Connected to: {company.get('company', {}).get('name', 'Unknown')}")
    except Exception as e:
        print(f"Error connecting to Aircall API: {e}")
        sys.exit(1)

    # Single call mode
    if args.call_id:
        print(f"\nFetching transcript for call {args.call_id}...")
        transcript_data = get_transcript(args.call_id)
        content = format_transcript(transcript_data)
        filepath = save_transcript(args.call_id, content, args.output)
        print(f"Saved to {filepath}")
        if args.json and transcript_data:
            json_path = Path(args.output) / f"call_{args.call_id}_raw.json"
            json_path.write_text(json.dumps(transcript_data, indent=2), encoding="utf-8")
            print(f"Raw JSON saved to {json_path}")
        print(f"\n{content}")
        return

    # Fetch calls
    from_ts = parse_date(args.from_date) if args.from_date else None
    to_ts = parse_date(args.to_date) if args.to_date else None

    print("\nFetching calls...")
    calls = list_calls(from_ts=from_ts, to_ts=to_ts)
    print(f"\nFound {len(calls)} calls")

    if not calls:
        print("No calls found.")
        return

    # List mode
    if args.list_calls:
        print(f"\n{'ID':<12} {'Date':<22} {'Direction':<10} {'Duration':<10} {'Agent':<20} {'Status'}")
        print("-" * 90)
        for call in calls:
            call_id = call.get("id", "")
            started = call.get("started_at")
            date_str = datetime.fromtimestamp(started).strftime("%Y-%m-%d %H:%M:%S") if started else "N/A"
            direction = call.get("direction", "N/A")
            duration = f"{call.get('duration', 0)}s"
            user = call.get("user")
            agent = user.get("name", "N/A") if user else "N/A"
            status = call.get("status", "N/A")
            print(f"{call_id:<12} {date_str:<22} {direction:<10} {duration:<10} {agent:<20} {status}")
        return

    # Extract transcripts for all calls
    print(f"\nExtracting transcripts for {len(calls)} calls...")
    success = 0
    skipped = 0
    errors = 0

    for i, call in enumerate(calls, 1):
        call_id = call.get("id")
        duration = call.get("duration", 0)

        # Skip very short calls (likely no transcript)
        if duration < 5:
            skipped += 1
            continue

        print(f"[{i}/{len(calls)}] Call {call_id} ({duration}s)...", end=" ")
        try:
            transcript_data = get_transcript(call_id)
            if transcript_data:
                content = format_transcript(transcript_data, call_info=call)
                filepath = save_transcript(call_id, content, args.output)
                if args.json:
                    json_path = Path(args.output) / f"call_{call_id}_raw.json"
                    json_path.write_text(json.dumps(transcript_data, indent=2), encoding="utf-8")
                print(f"Saved to {filepath}")
                success += 1
            else:
                print("No transcript available")
                skipped += 1
        except Exception as e:
            print(f"Error: {e}")
            errors += 1

        time.sleep(RATE_LIMIT_DELAY)

    print(f"\nDone! Extracted: {success} | Skipped: {skipped} | Errors: {errors}")
    print(f"Transcripts saved to: {args.output}/")


if __name__ == "__main__":
    main()
