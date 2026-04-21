"""Webhook & API endpoints for the Trustate automation pipeline.

Provides HTTP endpoints that CRM systems, client portals, attorney
dashboards, and court filing systems can call to drive the automation.

Built with FastAPI for easy deployment. Can also be triggered via
Zapier, Make.com, or direct API calls.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from trustate.config import build_trustate_client
from trustate.integrations.mappers import MapperConfig
from trustate.pipeline import ProbatePipeline

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Trustate Probate Automation API",
    description="Automated probate pipeline — from client signing to court filing",
    version="0.1.0",
)

from trustate.config import config as _config

pipeline = ProbatePipeline(
    trustate_client=build_trustate_client(),
    mapper_config=MapperConfig(source=_config.trustate_source_id),
)


# ── Request Models ────────────────────────────────────────────────────


class CRMWebhookPayload(BaseModel):
    """Payload from CRM when a sales closer signs a new client."""

    deal_id: str
    closer_id: str = ""
    contact_first_name: str
    contact_last_name: str
    contact_email: str
    contact_phone: str = ""
    contact_address: str = ""
    contact_city: str = ""
    contact_state: str = ""
    contact_zip: str = ""
    contact_county: str = ""
    decedent_first_name: str = ""
    decedent_last_name: str = ""
    relationship_to_decedent: str = "child"
    law_firm_id: str = ""
    aircall_call_id: str = ""


class IntakeFormPayload(BaseModel):
    """Completed intake form data from client portal."""

    case_id: str
    decedent: dict[str, Any] = {}
    assets: list[dict[str, Any]] = []
    beneficiaries: list[dict[str, Any]] = []


class DocumentUploadPayload(BaseModel):
    """Document upload notification from client portal."""

    case_id: str
    doc_type: str
    file_path: str = ""
    file_url: str = ""


class AttorneyDecisionPayload(BaseModel):
    """Attorney approval or rejection of a petition."""

    case_id: str
    approved: bool
    notes: str = ""


class HearingScheduledPayload(BaseModel):
    """Court hearing scheduled notification."""

    case_id: str
    hearing_date: str
    hearing_location: str = ""


# ── Endpoints ─────────────────────────────────────────────────────────


@app.post("/webhooks/crm/deal-closed")
async def crm_deal_closed(payload: CRMWebhookPayload):
    """Triggered when a sales closer signs a new client in the CRM.

    This kicks off the entire automation pipeline.
    """
    try:
        case = pipeline.handle_new_client(payload.model_dump())
        return {
            "status": "ok",
            "case_id": case.id,
            "stage": case.stage.value,
            "message": f"Case created for {payload.contact_first_name} {payload.contact_last_name}",
        }
    except Exception as e:
        logger.exception("Failed to process CRM webhook")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/webhooks/intake/submitted")
async def intake_submitted(payload: IntakeFormPayload):
    """Triggered when a client completes their intake form."""
    try:
        case = pipeline.handle_intake_submitted(
            payload.case_id, payload.model_dump()
        )
        return {
            "status": "ok",
            "case_id": case.id,
            "stage": case.stage.value,
            "pending_documents": [
                d.doc_type.value for d in case.get_pending_documents()
            ],
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("Failed to process intake form")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/webhooks/documents/uploaded")
async def document_uploaded(payload: DocumentUploadPayload):
    """Triggered when a client uploads a document."""
    try:
        case = pipeline.handle_document_uploaded(
            payload.case_id,
            payload.doc_type,
            payload.file_path,
            payload.file_url,
        )
        pending = case.get_pending_documents()
        return {
            "status": "ok",
            "case_id": case.id,
            "stage": case.stage.value,
            "remaining_documents": [d.doc_type.value for d in pending],
            "all_collected": len(pending) == 0,
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("Failed to process document upload")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/webhooks/attorney/decision")
async def attorney_decision(payload: AttorneyDecisionPayload):
    """Triggered when an attorney approves or rejects a petition."""
    try:
        if payload.approved:
            case = pipeline.handle_attorney_approval(payload.case_id)
        else:
            case = pipeline.handle_attorney_rejection(
                payload.case_id, payload.notes
            )
        return {
            "status": "ok",
            "case_id": case.id,
            "stage": case.stage.value,
            "approved": payload.approved,
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("Failed to process attorney decision")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/webhooks/court/hearing-scheduled")
async def hearing_scheduled(payload: HearingScheduledPayload):
    """Triggered when a court hearing is scheduled."""
    try:
        case = pipeline.handle_hearing_scheduled(
            payload.case_id, payload.hearing_date, payload.hearing_location
        )
        return {
            "status": "ok",
            "case_id": case.id,
            "stage": case.stage.value,
            "hearing_date": payload.hearing_date,
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("Failed to process hearing notification")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/webhooks/court/letters-issued")
async def letters_issued(request: Request):
    """Triggered when the court issues Letters Testamentary/Administration."""
    data = await request.json()
    case_id = data.get("case_id")
    if not case_id:
        raise HTTPException(status_code=400, detail="case_id required")
    try:
        case = pipeline.handle_letters_issued(case_id)
        return {"status": "ok", "case_id": case.id, "stage": case.stage.value}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/cases/{case_id}")
async def get_case(case_id: str):
    """Get the current status of a probate case."""
    status = pipeline.get_case_status(case_id)
    if not status:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found")
    return status


@app.get("/cases/{case_id}/documents/pending")
async def get_pending_documents(case_id: str):
    """Get the list of documents still needed for a case."""
    case = pipeline.store.get(case_id)
    if not case:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found")
    pending = case.get_pending_documents()
    return {
        "case_id": case_id,
        "pending": [
            {"type": d.doc_type.value, "status": d.status.value}
            for d in pending
        ],
    }


@app.get("/health")
async def health():
    return {"status": "ok", "service": "trustate-probate-automation"}
