"""PandaDoc integration — send the fee agreement for e-signature.

The moment a deal is marked Won in GHL, we create a PandaDoc document
from a pre-built fee-agreement template, pre-fill it with the client's
info, and email it to them for signing. When the client signs, PandaDoc
fires a webhook back to us so we can mark the case's fee agreement as
signed and advance the pipeline.

API reference: https://developers.pandadoc.com/reference
    POST /public/v1/documents                 create from template
    POST /public/v1/documents/{id}/send       send for signing
    GET  /public/v1/documents/{id}            status check
    POST /public/v1/documents/{id}/download   download signed PDF

Auth: API-Key <your-key> in Authorization header.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)


class PandaDocAPIError(Exception):
    def __init__(self, status_code: int, message: str, payload: Optional[dict] = None):
        super().__init__(f"PandaDoc API {status_code}: {message}")
        self.status_code = status_code
        self.message = message
        self.payload = payload or {}


@dataclass
class Recipient:
    """A recipient on a PandaDoc document."""

    email: str
    first_name: str = ""
    last_name: str = ""
    role: str = "Client"  # must match the role name in your PandaDoc template
    signing_order: int = 1


@dataclass
class FeeAgreementRequest:
    """Everything needed to generate a fee agreement from a template."""

    template_id: str
    recipient: Recipient
    # Field values to pre-fill. Keys match the "Name" of fields in the template.
    # Example: {"Client.FullName": "Jane Doe", "Client.Address": "123 Main St"}
    fields: dict[str, Any] = field(default_factory=dict)
    # Tokens are simpler merge fields like [Client.Name] in template text.
    tokens: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)
    document_name: str = ""
    # When True, send the document immediately after creation
    send_immediately: bool = True
    # Subject/message of the email PandaDoc sends to the signer
    email_subject: str = "Please sign your fee agreement"
    email_message: str = (
        "Thank you for choosing us to handle the probate process. "
        "Please review and sign the attached fee agreement to get started."
    )


@dataclass
class FeeAgreementResult:
    document_id: str
    status: str
    recipient_email: str
    sent: bool
    signing_url: str = ""


class PandaDocClient:
    """HTTP client for the PandaDoc public API."""

    BASE_URL = "https://api.pandadoc.com/public/v1"

    def __init__(self, api_key: str, timeout: int = 30, max_retries: int = 3):
        if not api_key:
            raise ValueError("PandaDoc api_key is required")
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries

    # ── Public methods ──────────────────────────────────────────────

    def send_fee_agreement(self, req: FeeAgreementRequest) -> FeeAgreementResult:
        """End-to-end: create doc from template, optionally send it."""
        doc = self.create_from_template(req)
        doc_id = doc["id"]

        # PandaDoc needs a moment to transition from document.uploaded to document.draft
        # before it can be sent. Poll briefly for readiness.
        self._wait_until_status(doc_id, target="document.draft", timeout_s=30)

        sent = False
        final_status = doc.get("status", "document.draft")
        if req.send_immediately:
            send_resp = self.send_document(
                doc_id, req.email_subject, req.email_message
            )
            sent = True
            final_status = send_resp.get("status") or "document.sent"

        return FeeAgreementResult(
            document_id=doc_id,
            status=final_status,
            recipient_email=req.recipient.email,
            sent=sent,
        )

    def create_from_template(self, req: FeeAgreementRequest) -> dict[str, Any]:
        """POST /public/v1/documents — create a document from a template."""
        body = {
            "name": req.document_name or f"Fee Agreement - {req.recipient.email}",
            "template_uuid": req.template_id,
            "recipients": [
                {
                    "email": req.recipient.email,
                    "first_name": req.recipient.first_name,
                    "last_name": req.recipient.last_name,
                    "role": req.recipient.role,
                    "signing_order": req.recipient.signing_order,
                }
            ],
            "tokens": [
                {"name": k, "value": v} for k, v in req.tokens.items()
            ],
            "fields": {
                k: {"value": v} for k, v in req.fields.items()
            },
            "metadata": req.metadata,
        }
        return self._request("POST", "/documents", body)

    def send_document(
        self, document_id: str, subject: str, message: str
    ) -> dict[str, Any]:
        """POST /public/v1/documents/{id}/send — email the document to recipients."""
        return self._request(
            "POST",
            f"/documents/{document_id}/send",
            {"subject": subject, "message": message, "silent": False},
        )

    def get_status(self, document_id: str) -> dict[str, Any]:
        """GET /public/v1/documents/{id} — check status."""
        return self._request("GET", f"/documents/{document_id}", None)

    def download_signed_pdf(self, document_id: str) -> bytes:
        """GET /public/v1/documents/{id}/download — returns PDF bytes."""
        url = f"{self.BASE_URL}/documents/{document_id}/download"
        resp = requests.get(url, headers=self._headers(), timeout=self.timeout)
        if resp.status_code >= 400:
            raise PandaDocAPIError(resp.status_code, resp.text)
        return resp.content

    # ── Internal ────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"API-Key {self.api_key}",
            "Content-Type": "application/json",
        }

    def _request(
        self, method: str, path: str, body: Optional[dict]
    ) -> dict[str, Any]:
        url = f"{self.BASE_URL}{path}"

        last_err: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.request(
                    method,
                    url,
                    headers=self._headers(),
                    json=body,
                    timeout=self.timeout,
                )
            except requests.RequestException as e:
                last_err = e
                if attempt < self.max_retries:
                    time.sleep(2 ** (attempt - 1))
                    continue
                raise PandaDocAPIError(0, f"Network error: {e}") from e

            try:
                payload = resp.json() if resp.content else {}
            except ValueError:
                payload = {"error": resp.text}

            # Retry 5xx and 429
            if (
                resp.status_code in (429,) or 500 <= resp.status_code < 600
            ) and attempt < self.max_retries:
                backoff = 2 ** (attempt - 1)
                logger.warning(
                    "PandaDoc %s %s returned %d (attempt %d/%d) — retrying in %ds",
                    method,
                    path,
                    resp.status_code,
                    attempt,
                    self.max_retries,
                    backoff,
                )
                time.sleep(backoff)
                continue

            if resp.status_code >= 400:
                raise PandaDocAPIError(
                    resp.status_code,
                    payload.get("detail") or payload.get("error") or resp.reason,
                    payload,
                )

            return payload

        raise PandaDocAPIError(0, f"Exhausted retries: {last_err}")

    def _wait_until_status(
        self, doc_id: str, target: str, timeout_s: int = 30, interval_s: float = 2.0
    ) -> None:
        """Poll until the document reaches the target status (or timeout)."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            doc = self.get_status(doc_id)
            if doc.get("status") == target:
                return
            time.sleep(interval_s)
        logger.warning(
            "PandaDoc doc %s did not reach status %s within %ds",
            doc_id,
            target,
            timeout_s,
        )


# ── Webhook event parsing ──────────────────────────────────────────


@dataclass
class PandaDocEvent:
    """Normalized PandaDoc webhook event."""

    event: str  # e.g. "document_state_changed"
    document_id: str
    status: str  # e.g. "document.completed", "document.viewed"
    recipient_email: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def parse_webhook_event(payload: Any) -> PandaDocEvent:
    """Normalize a PandaDoc webhook into a PandaDocEvent.

    PandaDoc sends webhooks as an array of events. Call this once per
    event item. See: https://developers.pandadoc.com/reference/webhooks
    """
    if isinstance(payload, list):
        raise ValueError(
            "parse_webhook_event expects a single event dict — iterate the list first"
        )
    data = payload.get("data") or {}
    recipient_email = ""
    for recipient in data.get("recipients") or []:
        if recipient.get("has_completed"):
            recipient_email = recipient.get("email", "")
            break
    if not recipient_email and (data.get("recipients") or []):
        recipient_email = data["recipients"][0].get("email", "")
    return PandaDocEvent(
        event=payload.get("event", ""),
        document_id=data.get("id", ""),
        status=data.get("status", ""),
        recipient_email=recipient_email,
        metadata=data.get("metadata", {}) or {},
    )
