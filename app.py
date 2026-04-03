#!/usr/bin/env python3
"""Aircall-GHL Transcript Integration

Webhook server that listens for GoHighLevel "opportunity won" events,
pulls ALL call transcripts from Aircall for that contact, and continues
monitoring for new calls for 2 weeks after the won date.

Flow:
  1. GHL fires OpportunityStatusUpdate webhook (status=won)
  2. We fetch the contact's phone number from GHL
  3. We pull ALL existing Aircall transcripts for that contact
  4. We start monitoring for new calls for 2 weeks
  5. Each transcript gets its own Google Doc + GHL note (with call date)
  6. The closer is notified on Slack
  7. After 2 weeks, we stop monitoring that contact
"""

import base64
import io
import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

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

MONITOR_DURATION_DAYS = int(os.getenv("MONITOR_DURATION_DAYS", "14"))
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "43200"))  # twice a day (every 12 hours)

MONITOR_FILE = Path(os.getenv("MONITOR_FILE", "monitored_contacts.json"))


# ---------------------------------------------------------------------------
# Contact monitoring store
# ---------------------------------------------------------------------------
def load_monitored_contacts():
    """Load the list of contacts being monitored from disk."""
    if MONITOR_FILE.exists():
        return json.loads(MONITOR_FILE.read_text(encoding="utf-8"))
    return {}


def save_monitored_contacts(data):
    """Save the monitored contacts list to disk."""
    MONITOR_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def add_monitored_contact(contact_id, phone, contact_name, opportunity_name, processed_call_ids=None):
    """Add a contact to the monitoring list with a 2-week expiry."""
    contacts = load_monitored_contacts()
    expiry = (datetime.now() + timedelta(days=MONITOR_DURATION_DAYS)).isoformat()

    contacts[contact_id] = {
        "phone": phone,
        "contact_name": contact_name,
        "opportunity_name": opportunity_name,
        "won_at": datetime.now().isoformat(),
        "expires_at": expiry,
        "processed_call_ids": processed_call_ids or [],
    }
    save_monitored_contacts(contacts)
    log.info("Monitoring contact %s (%s) until %s", contact_name, phone, expiry)


def remove_expired_contacts():
    """Remove contacts whose 2-week monitoring window has expired."""
    contacts = load_monitored_contacts()
    now = datetime.now()
    expired = []

    for cid, info in contacts.items():
        expiry = datetime.fromisoformat(info["expires_at"])
        if now >= expiry:
            expired.append(cid)
            log.info("Monitoring expired for %s (%s)", info["contact_name"], info["phone"])

    for cid in expired:
        del contacts[cid]

    if expired:
        save_monitored_contacts(contacts)

    return expired


# ---------------------------------------------------------------------------
# PDF + Google Drive helpers
# ---------------------------------------------------------------------------
def get_drive_service():
    """Build Google Drive service client from service account credentials."""
    creds = service_account.Credentials.from_service_account_file(
        GOOGLE_SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    return build("drive", "v3", credentials=creds)


def generate_pdf(title, transcript_text, call_info=None, contact_name=None, opportunity_name=None, summary_text=None):
    """Generate a PDF file with the summary and transcript. Returns the temp file path."""
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp.close()

    doc = SimpleDocTemplate(
        tmp.name,
        pagesize=letter,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("DocTitle", parent=styles["Heading1"], fontSize=16, spaceAfter=12)
    heading_style = ParagraphStyle("SectionHead", parent=styles["Heading2"], fontSize=13, spaceAfter=8, spaceBefore=16)
    meta_style = ParagraphStyle("Meta", parent=styles["Normal"], fontSize=10, textColor="#555555", spaceAfter=2)
    body_style = ParagraphStyle("Body", parent=styles["Normal"], fontSize=10, leading=14, spaceAfter=6)
    speaker_style = ParagraphStyle("Speaker", parent=styles["Normal"], fontSize=10, textColor="#666666", spaceBefore=10, spaceAfter=2)

    elements = []

    # Title
    elements.append(Paragraph(title.replace("&", "&amp;"), title_style))
    elements.append(Spacer(1, 8))

    # Call metadata
    if opportunity_name:
        elements.append(Paragraph(f"Opportunity: {opportunity_name}", meta_style))
    if contact_name:
        elements.append(Paragraph(f"Contact: {contact_name}", meta_style))
    if call_info:
        started = call_info.get("started_at")
        if started:
            elements.append(Paragraph(f"Call Date: {datetime.fromtimestamp(started).strftime('%Y-%m-%d %H:%M')}", meta_style))
        elements.append(Paragraph(f"Call ID: {call_info.get('id', 'N/A')}", meta_style))
        elements.append(Paragraph(f"Direction: {call_info.get('direction', 'N/A')}", meta_style))
        duration = call_info.get("duration", 0)
        mins, secs = divmod(duration, 60)
        elements.append(Paragraph(f"Duration: {mins}m {secs}s", meta_style))
        user = call_info.get("user")
        if user:
            elements.append(Paragraph(f"Agent: {user.get('name', 'N/A')}", meta_style))

    elements.append(Spacer(1, 12))

    # Summary section
    if summary_text:
        elements.append(Paragraph("SUMMARY", heading_style))
        for line in summary_text.split("\n"):
            if line.strip():
                safe = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                elements.append(Paragraph(safe, body_style))
        elements.append(Spacer(1, 12))

    # Transcript section
    elements.append(Paragraph("TRANSCRIPT", heading_style))
    for line in transcript_text.split("\n"):
        if not line.strip():
            continue
        safe = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        # Speaker lines (e.g. "Agent · 00:17")
        if " · " in line and any(line.startswith(s) for s in ("Agent", "Contact", "Unknown")):
            elements.append(Paragraph(f"<b>{safe}</b>", speaker_style))
        else:
            elements.append(Paragraph(safe, body_style))

    doc.build(elements)
    return tmp.name


def upload_pdf_to_drive(pdf_path, title):
    """Upload a PDF to Google Drive shared folder. Returns the shareable link."""
    drive_service = get_drive_service()

    file_metadata = {
        "name": f"{title}.pdf",
        "mimeType": "application/pdf",
    }
    if GOOGLE_DRIVE_FOLDER_ID:
        file_metadata["parents"] = [GOOGLE_DRIVE_FOLDER_ID]

    media = MediaFileUpload(pdf_path, mimetype="application/pdf")

    file = drive_service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id",
        supportsAllDrives=True,
    ).execute()
    file_id = file["id"]

    # Share: anyone with the link can view
    drive_service.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"},
        supportsAllDrives=True,
    ).execute()

    pdf_url = f"https://drive.google.com/file/d/{file_id}/view"
    log.info("Uploaded PDF: %s", pdf_url)

    # Clean up temp file
    os.unlink(pdf_path)

    return pdf_url


def create_transcript_pdf(title, transcript_text, call_info=None, contact_name=None, opportunity_name=None, summary_text=None):
    """Generate a PDF and upload it to Google Drive. Returns the shareable link."""
    pdf_path = generate_pdf(
        title=title,
        transcript_text=transcript_text,
        call_info=call_info,
        contact_name=contact_name,
        opportunity_name=opportunity_name,
        summary_text=summary_text,
    )
    return upload_pdf_to_drive(pdf_path, title)


# ---------------------------------------------------------------------------
# Slack helpers
# ---------------------------------------------------------------------------
def send_slack_notification(contact_name, opportunity_name, doc_url, phone=None, call_date=None, user_id=None):
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
    if call_date:
        message += f"\n*Call Date:* {call_date}"
    message += f"\n*Transcript:* <{doc_url}|View Google Doc>"

    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json",
    }

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


def _extract_segments(data):
    """Extract the list of transcript segments from various Aircall response formats."""
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in ("transcription", "transcript", "segments", "turns", "content"):
            val = data.get(key)
            if isinstance(val, list):
                return val
            if isinstance(val, dict):
                for subkey in ("segments", "turns", "content", "utterances"):
                    subval = val.get(subkey)
                    if isinstance(subval, list):
                        return subval

    return None


def _format_timestamp(seconds):
    """Convert seconds to MM:SS format."""
    if not seconds:
        return "00:00"
    mins = int(seconds) // 60
    secs = int(seconds) % 60
    return f"{mins:02d}:{secs:02d}"


def format_transcript(transcript_data, call_info=None):
    """Format Aircall transcript data into readable dialogue.

    Output format (similar to Fireflies):
        Agent · 00:11
        Hi. Is this Caroline?

        Contact · 00:15
        Yeah. This is her.
    """
    if not transcript_data:
        return "[No transcript available]"

    segments = _extract_segments(transcript_data)

    if not segments:
        if isinstance(transcript_data, str):
            return transcript_data
        return json.dumps(transcript_data, indent=2)

    lines = []

    for seg in segments:
        if not isinstance(seg, dict):
            continue

        text = seg.get("text", "").strip()
        if not text:
            continue

        participant = seg.get("participant_type", "")
        if participant in ("internal", "agent"):
            speaker = "Agent"
        elif participant == "external":
            speaker = "Contact"
        else:
            speaker = seg.get("speaker", seg.get("role", "Unknown"))

        start_time = seg.get("start_time", seg.get("start", 0))
        timestamp = _format_timestamp(start_time)

        lines.append(f"{speaker} · {timestamp}")
        lines.append(text)
        lines.append("")

    return "\n".join(lines).strip() if lines else "[No transcript content]"


def extract_summary_text(summary_data):
    """Extract the summary text from Aircall's summary API response."""
    if not summary_data:
        return None
    summary_text = (
        summary_data.get("summary")
        or summary_data.get("text")
        or summary_data.get("content")
    )
    if isinstance(summary_text, dict):
        summary_text = summary_text.get("text", str(summary_text))
    return summary_text


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
# Process a single call → Google Doc + GHL note + Slack
# ---------------------------------------------------------------------------
def process_single_call(call, contact_id, contact_name, opportunity_name, phone):
    """Process one Aircall call: get transcript, create doc, post note, notify."""
    call_id = call.get("id")
    started = call.get("started_at", 0)
    duration = call.get("duration", 0)

    # Skip very short calls
    if duration < 5:
        log.info("Skipping call %s (too short: %ds)", call_id, duration)
        return None

    # Format call date for the note
    call_date_str = datetime.fromtimestamp(started).strftime("%Y-%m-%d %H:%M") if started else "Unknown"

    log.info("Processing call %s (%s, %ds)", call_id, call_date_str, duration)

    # Get transcript
    transcript_data = get_transcript(call_id)
    if not transcript_data:
        log.info("No transcript for call %s, skipping", call_id)
        return None

    transcript_text = format_transcript(transcript_data)

    # Get summary
    summary_data = get_call_summary(call_id)
    summary_text = extract_summary_text(summary_data)

    # Create PDF and upload to Drive
    pdf_title = f"Transcript – {contact_name} – {call_date_str}"
    doc_url = create_transcript_pdf(
        title=pdf_title,
        transcript_text=transcript_text,
        call_info=call,
        contact_name=contact_name,
        opportunity_name=opportunity_name,
        summary_text=summary_text,
    )

    # Post note to GHL with call date
    note_body = f"Aircall Transcript ({call_date_str}): {doc_url}"
    ghl_create_note(contact_id, note_body)

    # Slack notification
    send_slack_notification(
        contact_name, opportunity_name, doc_url,
        phone=phone, call_date=call_date_str,
    )

    log.info("Processed call %s → %s", call_id, doc_url)
    return call_id


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
    """Full pipeline: pull ALL transcripts for the contact, start 2-week monitoring."""

    # Step 1: Get contact phone number from GHL
    log.info("Fetching GHL contact %s", contact_id)
    contact = ghl_get_contact(contact_id)
    phone = contact.get("phone")
    if not phone:
        log.warning("No phone number for contact %s", contact_id)
        return {"status": "skipped", "reason": "no_phone_number"}

    contact_name = f"{contact.get('firstName', '')} {contact.get('lastName', '')}".strip()
    log.info("Contact: %s, Phone: %s", contact_name, phone)

    # Step 2: Search Aircall for ALL calls to this number
    log.info("Searching Aircall for all calls to %s", phone)
    calls = search_calls_by_phone(phone)
    if not calls:
        log.warning("No Aircall calls found for %s", phone)
        return {"status": "no_calls_found"}

    log.info("Found %d Aircall calls for %s", len(calls), phone)

    # Step 3: Process ALL calls with transcripts
    processed_ids = []
    for call in calls:
        call_id = process_single_call(call, contact_id, contact_name, opportunity_name, phone)
        if call_id:
            processed_ids.append(call_id)
        time.sleep(0.5)  # Rate limit protection

    # Step 4: Add contact to monitoring list for 2 weeks
    add_monitored_contact(
        contact_id=contact_id,
        phone=phone,
        contact_name=contact_name,
        opportunity_name=opportunity_name,
        processed_call_ids=processed_ids,
    )

    return {
        "status": "success",
        "contact": contact_name,
        "transcripts_processed": len(processed_ids),
        "monitoring_until": (datetime.now() + timedelta(days=MONITOR_DURATION_DAYS)).isoformat(),
    }


# ---------------------------------------------------------------------------
# Background polling for new calls
# ---------------------------------------------------------------------------
def poll_monitored_contacts():
    """Check all monitored contacts for new Aircall calls."""
    remove_expired_contacts()
    contacts = load_monitored_contacts()

    if not contacts:
        return

    log.info("Polling %d monitored contacts for new calls", len(contacts))

    for contact_id, info in contacts.items():
        phone = info["phone"]
        contact_name = info["contact_name"]
        opportunity_name = info["opportunity_name"]
        processed_ids = info.get("processed_call_ids", [])

        calls = search_calls_by_phone(phone)
        new_calls = [c for c in calls if c.get("id") not in processed_ids]

        if new_calls:
            log.info("Found %d new calls for %s", len(new_calls), contact_name)
            for call in new_calls:
                call_id = process_single_call(call, contact_id, contact_name, opportunity_name, phone)
                if call_id:
                    processed_ids.append(call_id)
                time.sleep(0.5)

            # Update the processed list
            info["processed_call_ids"] = processed_ids
            save_monitored_contacts(contacts)

        time.sleep(1)  # Rate limit between contacts


def background_poller():
    """Run the polling loop in a background thread."""
    log.info("Background poller started (interval: %ds)", POLL_INTERVAL_SECONDS)
    while True:
        try:
            poll_monitored_contacts()
        except Exception:
            log.exception("Error in background poller")
        time.sleep(POLL_INTERVAL_SECONDS)


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
    if not SLACK_BOT_TOKEN:
        missing.append("SLACK_BOT_TOKEN")

    contacts = load_monitored_contacts()

    return jsonify({
        "status": "ok" if not missing else "misconfigured",
        "missing_env_vars": missing,
        "monitored_contacts": len(contacts),
    })


@app.route("/monitored", methods=["GET"])
def list_monitored():
    """View currently monitored contacts."""
    contacts = load_monitored_contacts()
    return jsonify(contacts)


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

    # Start background poller
    poller_thread = threading.Thread(target=background_poller, daemon=True)
    poller_thread.start()

    port = int(os.getenv("PORT", 5000))
    log.info("Starting webhook server on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_DEBUG", "0") == "1")
