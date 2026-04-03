#!/usr/bin/env python3
"""Aircall-GHL Transcript Integration

Webhook server that listens for GoHighLevel "opportunity won" events,
pulls the call transcript from Aircall (matched by contact phone number
and closest timestamp), saves it to a Google Doc (shared via link),
posts the link as a note on the GHL contact, and notifies the closer on Slack.

Flow:
  1. GHL fires OpportunityStatusUpdate webhook (status=won)
  2. We fetch the contact's phone number from GHL
  3. We search Aircall for calls to that number
  4. We pick the call nearest to the won timestamp
  5. We pull the transcript
  6. We create a Google Doc with the transcript (anyone with link can view)
  7. We post the doc link as a note on the GHL contact
  8. We notify the closer on Slack with the doc link
"""

import base64
import json
import logging
import os
import sys
import time
from datetime import datetime

import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from google.oauth2 import service_account
from googleapiclient.discovery import build

load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
AIRCALL_API_ID = os.getenv("AIRCALL_API_ID")
AIRCALL_API_TOKEN = os.getenv("AIRCALL_API_TOKEN")
AIRCALL_BASE_URL = "https://api.aircall.io/v1"

GHL_API_TOKEN = os.getenv("GHL_API_TOKEN")
GHL_BASE_URL = "https://rest.gohighlevel.com/v1"

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials.json")
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_USER_ID = os.getenv("SLACK_USER_ID")


# ---------------------------------------------------------------------------
# Google Docs helpers
# ---------------------------------------------------------------------------
def get_google_services():
    """Build Google Docs and Drive service clients from service account credentials."""
    creds = service_account.Credentials.from_service_account_file(
        GOOGLE_SERVICE_ACCOUNT_FILE,
        scopes=[
            "https://www.googleapis.com/auth/documents",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    docs_service = build("docs", "v1", credentials=creds)
    drive_service = build("drive", "v3", credentials=creds)
    return docs_service, drive_service


def create_transcript_doc(title, transcript_text, call_info=None, contact_name=None, opportunity_name=None, summary_text=None):
    """Create a Google Doc with the summary and transcript, shared with anyone who has the link."""
    docs_service, drive_service = get_google_services()

    # Build the document body
    body_lines = []
    if opportunity_name:
        body_lines.append(f"Opportunity: {opportunity_name}")
    if contact_name:
        body_lines.append(f"Contact: {contact_name}")
    if call_info:
        started = call_info.get("started_at")
        if started:
            body_lines.append(f"Call Date: {datetime.fromtimestamp(started).isoformat()}")
        body_lines.append(f"Call ID: {call_info.get('id', 'N/A')}")
        body_lines.append(f"Direction: {call_info.get('direction', 'N/A')}")
        body_lines.append(f"Duration: {call_info.get('duration', 0)}s")
        user = call_info.get("user")
        if user:
            body_lines.append(f"Agent: {user.get('name', 'N/A')}")
    body_lines.append("")
    body_lines.append("─" * 40)

    if summary_text:
        body_lines.append("")
        body_lines.append("SUMMARY")
        body_lines.append("─" * 40)
        body_lines.append(summary_text)
        body_lines.append("")
        body_lines.append("─" * 40)

    body_lines.append("")
    body_lines.append("TRANSCRIPT")
    body_lines.append("─" * 40)
    body_lines.append(transcript_text)

    full_text = "\n".join(body_lines)

    # Create the doc in the shared folder via Drive API
    file_metadata = {
        "name": title,
        "mimeType": "application/vnd.google-apps.document",
    }
    if GOOGLE_DRIVE_FOLDER_ID:
        file_metadata["parents"] = [GOOGLE_DRIVE_FOLDER_ID]

    file = drive_service.files().create(
        body=file_metadata,
        fields="id",
        supportsAllDrives=True,
    ).execute()
    doc_id = file["id"]

    # Insert the text content
    docs_service.documents().batchUpdate(
        documentId=doc_id,
        body={
            "requests": [
                {
                    "insertText": {
                        "location": {"index": 1},
                        "text": full_text,
                    }
                }
            ]
        },
    ).execute()

    # Share: anyone with the link can view
    drive_service.permissions().create(
        fileId=doc_id,
        body={"type": "anyone", "role": "reader"},
        supportsAllDrives=True,
    ).execute()

    doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
    log.info("Created Google Doc: %s", doc_url)
    return doc_url


# ---------------------------------------------------------------------------
# Slack helpers
# ---------------------------------------------------------------------------
def send_slack_notification(contact_name, opportunity_name, doc_url, phone=None, user_id=None):
    """Send a Slack DM notifying the closer that a transcript is ready."""
    if not SLACK_BOT_TOKEN:
        log.warning("SLACK_BOT_TOKEN not set, skipping notification")
        return

    target_user = user_id or SLACK_USER_ID
    if not target_user:
        log.warning("No Slack user ID configured, skipping notification")
        return

    message = (
        f"*Opportunity Won:* {opportunity_name}\n"
        f"*Contact:* {contact_name}"
    )
    if phone:
        message += f" ({phone})"
    message += f"\n*Transcript:* <{doc_url}|View Google Doc>"

    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json",
    }

    # Open a DM channel with the user
    dm_resp = requests.post(
        "https://slack.com/api/conversations.open",
        headers=headers,
        json={"users": target_user},
        timeout=15,
    )
    dm_data = dm_resp.json()
    if not dm_data.get("ok"):
        log.warning("Failed to open Slack DM: %s", dm_data.get("error"))
        return

    channel_id = dm_data["channel"]["id"]

    # Send the message
    msg_resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers=headers,
        json={"channel": channel_id, "text": message},
        timeout=15,
    )
    msg_data = msg_resp.json()
    if msg_data.get("ok"):
        log.info("Slack notification sent to %s", target_user)
    else:
        log.warning("Slack notification failed: %s", msg_data.get("error"))


# ---------------------------------------------------------------------------
# Aircall helpers
# ---------------------------------------------------------------------------
def aircall_headers():
    creds = base64.b64encode(f"{AIRCALL_API_ID}:{AIRCALL_API_TOKEN}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}


def aircall_get(endpoint, params=None):
    """GET request to Aircall API with retry logic."""
    url = f"{AIRCALL_BASE_URL}{endpoint}"
    for attempt in range(4):
        try:
            resp = requests.get(url, headers=aircall_headers(), params=params, timeout=30)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
                log.warning("Aircall rate limited, retrying in %ds", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            if attempt < 3:
                wait = 2 ** (attempt + 1)
                log.warning("Aircall request failed: %s. Retrying in %ds", e, wait)
                time.sleep(wait)
            else:
                raise
    return None


def search_calls_by_phone(phone_number):
    """Search Aircall calls by phone number, returns list sorted by most recent."""
    data = aircall_get("/calls/search", params={
        "phone_number": phone_number,
        "order": "desc",
        "per_page": 50,
    })
    if not data:
        return []
    return data.get("calls", [])


def find_nearest_call(calls, target_timestamp):
    """Find the call whose started_at is closest to (but before) the target timestamp.

    If no call is before the target, pick the closest one overall.
    """
    if not calls:
        return None

    # Prefer calls that happened before the won timestamp
    calls_before = [c for c in calls if c.get("started_at", 0) <= target_timestamp]
    if calls_before:
        return max(calls_before, key=lambda c: c.get("started_at", 0))

    # Fallback: closest call overall
    return min(calls, key=lambda c: abs(c.get("started_at", 0) - target_timestamp))


def get_transcript(call_id):
    """Fetch transcript for a specific Aircall call."""
    try:
        return aircall_get(f"/calls/{call_id}/transcription")
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return None
        raise


def get_call_summary(call_id):
    """Fetch AI-generated call summary from Aircall."""
    try:
        return aircall_get(f"/calls/{call_id}/summary")
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return None
        raise


def format_transcript(transcript_data, call_info=None):
    """Format transcript data into readable text for a GHL note."""
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
        lines.append("-" * 40)

    if not transcript_data:
        lines.append("[No transcript available]")
        return "\n".join(lines)

    transcript = (
        transcript_data.get("transcription")
        or transcript_data.get("transcript")
        or transcript_data
    )

    if isinstance(transcript, list):
        for seg in transcript:
            speaker = seg.get("speaker", seg.get("role", "Unknown"))
            text = seg.get("text", seg.get("content", ""))
            ts = seg.get("timestamp", seg.get("start", ""))
            if ts:
                lines.append(f"[{ts}] {speaker}: {text}")
            else:
                lines.append(f"{speaker}: {text}")
    elif isinstance(transcript, dict):
        segments = (
            transcript.get("segments")
            or transcript.get("turns")
            or transcript.get("content")
        )
        if isinstance(segments, list):
            for seg in segments:
                speaker = seg.get("speaker", seg.get("role", "Unknown"))
                text = seg.get("text", seg.get("content", ""))
                lines.append(f"{speaker}: {text}")
        elif "text" in transcript:
            lines.append(transcript["text"])
        else:
            lines.append(json.dumps(transcript, indent=2))
    elif isinstance(transcript, str):
        lines.append(transcript)
    else:
        lines.append(json.dumps(transcript_data, indent=2))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# GHL helpers
# ---------------------------------------------------------------------------
def ghl_headers():
    return {
        "Authorization": f"Bearer {GHL_API_TOKEN}",
        "Content-Type": "application/json",
    }


def ghl_get_contact(contact_id):
    """Fetch contact details from GHL to get phone number."""
    resp = requests.get(
        f"{GHL_BASE_URL}/contacts/{contact_id}",
        headers=ghl_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("contact", data)


def ghl_create_note(contact_id, body):
    """Create a note on a GHL contact."""
    resp = requests.post(
        f"{GHL_BASE_URL}/contacts/{contact_id}/notes/",
        headers=ghl_headers(),
        json={"body": body},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------
@app.route("/webhooks/ghl", methods=["POST"])
def ghl_webhook():
    """Handle GoHighLevel opportunity status update webhooks."""
    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"error": "No JSON payload"}), 400

    log.info("Received GHL webhook: type=%s", payload.get("type"))

    # Only process opportunity won events
    event_type = payload.get("type", "")
    status = payload.get("status", "")

    if event_type != "OpportunityStatusUpdate" or status != "won":
        log.info("Ignoring event: type=%s status=%s", event_type, status)
        return jsonify({"status": "ignored"}), 200

    contact_id = payload.get("contactId")
    opportunity_id = payload.get("id")
    opportunity_name = payload.get("name", "Unknown")

    if not contact_id:
        log.error("No contactId in webhook payload")
        return jsonify({"error": "Missing contactId"}), 400

    log.info(
        "Processing won opportunity: id=%s name=%s contact=%s",
        opportunity_id, opportunity_name, contact_id,
    )

    try:
        result = process_won_opportunity(contact_id, opportunity_name, payload)
        return jsonify(result), 200
    except Exception:
        log.exception("Error processing won opportunity %s", opportunity_id)
        return jsonify({"error": "Internal processing error"}), 500


def process_won_opportunity(contact_id, opportunity_name, payload):
    """Full pipeline: GHL contact → phone → Aircall search → transcript → note."""

    # Step 1: Get contact phone number from GHL
    log.info("Fetching GHL contact %s", contact_id)
    contact = ghl_get_contact(contact_id)
    phone = contact.get("phone")
    if not phone:
        log.warning("No phone number for contact %s", contact_id)
        return {"status": "skipped", "reason": "no_phone_number"}

    contact_name = f"{contact.get('firstName', '')} {contact.get('lastName', '')}".strip()
    log.info("Contact: %s, Phone: %s", contact_name, phone)

    # Step 2: Search Aircall for calls to this number
    log.info("Searching Aircall for calls to %s", phone)
    calls = search_calls_by_phone(phone)
    if not calls:
        log.warning("No Aircall calls found for %s", phone)
        note_body = (
            f"Opportunity Won: {opportunity_name}\n"
            f"Contact: {contact_name} ({phone})\n\n"
            "No Aircall calls found for this contact."
        )
        ghl_create_note(contact_id, note_body)
        return {"status": "no_calls_found"}

    log.info("Found %d Aircall calls for %s", len(calls), phone)

    # Step 3: Find the call nearest to the won timestamp
    # Use dateAdded from the webhook, or fall back to current time
    won_timestamp = payload.get("dateAdded")
    if won_timestamp:
        # GHL timestamps may be milliseconds or ISO string
        if isinstance(won_timestamp, (int, float)):
            if won_timestamp > 1e12:
                won_timestamp = won_timestamp / 1000
        elif isinstance(won_timestamp, str):
            try:
                won_timestamp = datetime.fromisoformat(
                    won_timestamp.replace("Z", "+00:00")
                ).timestamp()
            except ValueError:
                won_timestamp = time.time()
    else:
        won_timestamp = time.time()

    nearest_call = find_nearest_call(calls, won_timestamp)
    call_id = nearest_call.get("id")
    log.info("Nearest call: id=%s started_at=%s", call_id, nearest_call.get("started_at"))

    # Step 4: Get the transcript and summary
    log.info("Fetching transcript for call %s", call_id)
    transcript_data = get_transcript(call_id)
    transcript_text = format_transcript(transcript_data)

    log.info("Fetching AI summary for call %s", call_id)
    summary_data = get_call_summary(call_id)
    summary_text = None
    if summary_data:
        summary_text = (
            summary_data.get("summary")
            or summary_data.get("text")
            or summary_data.get("content")
        )
        if isinstance(summary_text, dict):
            summary_text = summary_text.get("text", str(summary_text))
        log.info("Got AI summary (%d chars)", len(summary_text) if summary_text else 0)

    # Step 5: Create Google Doc with summary and transcript
    doc_title = f"Transcript – {contact_name} – {opportunity_name}"
    log.info("Creating Google Doc for call %s", call_id)
    doc_url = create_transcript_doc(
        title=doc_title,
        transcript_text=transcript_text,
        call_info=nearest_call,
        contact_name=contact_name,
        opportunity_name=opportunity_name,
        summary_text=summary_text,
    )

    # Step 6: Post doc link as a note on the GHL contact
    note_body = (
        f"Opportunity Won: {opportunity_name}\n"
        f"Contact: {contact_name} ({phone})\n\n"
        f"Aircall Transcript: {doc_url}"
    )

    log.info("Posting transcript note to GHL contact %s", contact_id)
    ghl_create_note(contact_id, note_body)

    # Step 7: Notify the closer on Slack
    send_slack_notification(contact_name, opportunity_name, doc_url, phone=phone)

    return {
        "status": "success",
        "call_id": call_id,
        "contact": contact_name,
        "doc_url": doc_url,
        "transcript_length": len(transcript_text),
    }


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.route("/health", methods=["GET"])
def health():
    missing = []
    if not AIRCALL_API_ID:
        missing.append("AIRCALL_API_ID")
    if not AIRCALL_API_TOKEN:
        missing.append("AIRCALL_API_TOKEN")
    if not GHL_API_TOKEN:
        missing.append("GHL_API_TOKEN")
    if not os.path.exists(GOOGLE_SERVICE_ACCOUNT_FILE):
        missing.append("GOOGLE_SERVICE_ACCOUNT_FILE")
    if not SLACK_WEBHOOK_URL:
        missing.append("SLACK_WEBHOOK_URL")
    return jsonify({
        "status": "ok" if not missing else "misconfigured",
        "missing_env_vars": missing,
    })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    missing = []
    if not AIRCALL_API_ID or not AIRCALL_API_TOKEN:
        missing.append("AIRCALL_API_ID / AIRCALL_API_TOKEN")
    if not GHL_API_TOKEN:
        missing.append("GHL_API_TOKEN")
    if missing:
        log.warning("Missing credentials: %s — check your .env file", ", ".join(missing))

    port = int(os.getenv("PORT", 5000))
    log.info("Starting webhook server on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_DEBUG", "0") == "1")
