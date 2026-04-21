"""GoHighLevel (GHL) webhook integration.

GHL fires a webhook when an opportunity moves to a terminal stage. We
listen for "won" status changes on probate opportunities and fan out to:
  1. Trustate (create matter + contact)
  2. Cognito Forms (email the client the intake link)
  3. PandaDoc (send the fee agreement for signature)

GHL webhook payloads vary depending on how you configure the workflow
trigger. This module normalizes the common variants into one shape that
feeds the rest of the pipeline.

Two ways to send a webhook from GHL:

1. **Workflow > Webhook action** (recommended) — lets you build any JSON
   shape you want with merge fields. Map each field as you like.
2. **Native "Opportunity Status Changed" webhook** — GHL's default shape,
   includes nested contact + opportunity objects.

The parser below handles both.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class GHLOpportunityWonEvent:
    """Normalized view of a GHL opportunity-won webhook."""

    opportunity_id: str
    contact_id: str = ""
    location_id: str = ""
    pipeline_id: str = ""
    stage_id: str = ""
    monetary_value: float = 0.0
    opportunity_name: str = ""

    # Contact (the client) — flattened for ease of use
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    address_line1: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    county: str = ""

    # Probate-specific fields pulled from GHL custom fields
    decedent_first_name: str = ""
    decedent_last_name: str = ""
    relationship_to_decedent: str = ""

    # Pipeline assignment
    assigned_user_id: str = ""
    law_firm_id: str = ""

    # Raw payload for debugging / downstream custom handling
    raw: dict[str, Any] = field(default_factory=dict)


# GHL custom-field slug mapping — adjust to match YOUR GHL form's field names.
# Left side = the slug/id that appears in GHL's customFields array.
# Right side = which attribute on GHLOpportunityWonEvent it populates.
CUSTOM_FIELD_MAP: dict[str, str] = {
    "decedent_first_name": "decedent_first_name",
    "decedent_last_name": "decedent_last_name",
    "relationship_to_decedent": "relationship_to_decedent",
    "law_firm_id": "law_firm_id",
    "county": "county",
}


def parse_ghl_webhook(payload: dict[str, Any]) -> GHLOpportunityWonEvent:
    """Turn a GHL webhook payload into a normalized event.

    Handles both the native opportunity webhook shape and a flat
    workflow-action shape. Any field missing from the payload stays
    at its default (empty string / 0.0).
    """
    # Opportunity block — native webhooks wrap it; workflow actions may flatten.
    opp = payload.get("opportunity") or payload
    contact = payload.get("contact") or payload

    event = GHLOpportunityWonEvent(
        opportunity_id=str(opp.get("id") or payload.get("opportunityId") or ""),
        contact_id=str(contact.get("id") or payload.get("contactId") or ""),
        location_id=str(payload.get("locationId") or payload.get("location_id") or ""),
        pipeline_id=str(opp.get("pipelineId") or payload.get("pipeline_id") or ""),
        stage_id=str(opp.get("pipelineStageId") or payload.get("stage_id") or ""),
        monetary_value=_to_float(opp.get("monetaryValue") or payload.get("monetary_value")),
        opportunity_name=str(opp.get("name") or payload.get("opportunity_name") or ""),
        first_name=_s(contact.get("firstName") or contact.get("first_name")),
        last_name=_s(contact.get("lastName") or contact.get("last_name")),
        email=_s(contact.get("email")),
        phone=_s(contact.get("phone") or contact.get("phoneNumber")),
        address_line1=_s(contact.get("address1") or contact.get("address")),
        city=_s(contact.get("city")),
        state=_s(contact.get("state")),
        zip_code=_s(contact.get("postalCode") or contact.get("zip")),
        assigned_user_id=_s(
            opp.get("assignedTo") or payload.get("assigned_user_id")
        ),
        raw=payload,
    )

    # Custom fields may be either a dict or a list of {id, value} objects
    custom = contact.get("customFields") or contact.get("custom_fields") or {}
    _apply_custom_fields(event, custom)

    # Also allow top-level flat fields to win (workflow action shape)
    for src_key, dest_attr in CUSTOM_FIELD_MAP.items():
        if payload.get(src_key) and not getattr(event, dest_attr, None):
            setattr(event, dest_attr, str(payload[src_key]))

    return event


def is_won_status(payload: dict[str, Any]) -> bool:
    """Check whether a GHL webhook represents an opportunity being marked won.

    GHL sends "status" as one of: open, won, lost, abandoned. Some workflow
    webhooks don't carry a status and simply fire on the won trigger — in
    that case we assume the caller already filtered upstream.
    """
    status = (
        payload.get("status")
        or (payload.get("opportunity") or {}).get("status")
        or ""
    ).lower()
    if not status:
        # Workflow webhook with no explicit status — trust the trigger
        return True
    return status == "won"


def event_to_crm_payload(event: GHLOpportunityWonEvent) -> dict[str, Any]:
    """Map the normalized GHL event to the CRM webhook payload expected by
    ProbatePipeline.handle_new_client (the same shape as CRMWebhookPayload).
    """
    return {
        "deal_id": event.opportunity_id,
        "closer_id": event.assigned_user_id,
        "contact_first_name": event.first_name,
        "contact_last_name": event.last_name,
        "contact_email": event.email,
        "contact_phone": event.phone,
        "contact_address": event.address_line1,
        "contact_city": event.city,
        "contact_state": event.state,
        "contact_zip": event.zip_code,
        "contact_county": event.county,
        "decedent_first_name": event.decedent_first_name,
        "decedent_last_name": event.decedent_last_name,
        "relationship_to_decedent": event.relationship_to_decedent or "child",
        "law_firm_id": event.law_firm_id,
    }


# ── Helpers ─────────────────────────────────────────────────────────


def _apply_custom_fields(event: GHLOpportunityWonEvent, custom: Any) -> None:
    """Apply GHL customFields (dict or list form) to the event."""
    if isinstance(custom, dict):
        for key, value in custom.items():
            attr = CUSTOM_FIELD_MAP.get(key)
            if attr and value:
                setattr(event, attr, str(value))
        return
    if isinstance(custom, list):
        for item in custom:
            if not isinstance(item, dict):
                continue
            key = item.get("name") or item.get("key") or item.get("id") or ""
            value = item.get("value") or item.get("field_value") or ""
            attr = CUSTOM_FIELD_MAP.get(str(key))
            if attr and value:
                setattr(event, attr, str(value))


def _s(value: Any) -> str:
    return str(value) if value not in (None, "") else ""


def _to_float(value: Any) -> float:
    try:
        return float(value) if value not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0
