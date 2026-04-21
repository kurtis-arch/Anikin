"""Trustate Probate Automation Pipeline.

This is the main orchestrator that wires together all the modules
and drives a case from signing to filing with zero manual intervention.

Usage:
    from trustate.pipeline import ProbatePipeline

    pipeline = ProbatePipeline()

    # 1. Sales closer signs a client — CRM fires a webhook
    case = pipeline.handle_new_client(crm_payload)

    # 2. Client submits intake form
    case = pipeline.handle_intake_submitted(case.id, form_data)

    # 3. Client uploads documents (called per document)
    case = pipeline.handle_document_uploaded(case.id, "death_certificate", "/path/to/file")

    # 4. Attorney approves petition
    case = pipeline.handle_attorney_approval(case.id)

    # The pipeline auto-advances through stages and sends notifications
    # at every step.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from trustate.documents.doc_manager import DocumentManager
from trustate.filing.efiling import (
    EFilingProvider,
    FilingOrchestrator,
    FilingResult,
    TylerEFileProvider,
)
from trustate.integrations.cognito_forms import (
    CognitoFieldMap,
    CognitoFormsProcessor,
    CognitoUpdate,
)
from trustate.integrations.mappers import MapperConfig, case_to_trustate_payload
from trustate.integrations.trustate_client import (
    TrustateAPIError,
    TrustateClient,
)
from trustate.intake.client_intake import IntakeProcessor
from trustate.notifications.notifier import (
    NotificationOrchestrator,
    SendGridSender,
)
from trustate.petitions.generator import GeneratedPetition, PetitionGenerator
from trustate.workflow.engine import WorkflowEngine
from trustate.workflow.models import (
    CaseStage,
    DocumentType,
    ProbateCase,
)
from trustate.workflow.store import CaseStore, JSONFileStore

logger = logging.getLogger(__name__)


def _summarize_update(update: "CognitoUpdate") -> str:
    """One-line summary of what fields a Cognito webhook carried, for logs."""
    parts = []
    if update.client:
        parts.append("client")
    if update.decedent:
        parts.append("decedent")
    if update.assets is not None:
        parts.append(f"assets({len(update.assets)})")
    if update.beneficiaries is not None:
        parts.append(f"beneficiaries({len(update.beneficiaries)})")
    if update.documents:
        parts.append(f"docs({len(update.documents)})")
    return ",".join(parts) or "empty"


class ProbatePipeline:
    """End-to-end automation pipeline for probate cases.

    Wires together: intake -> documents -> petitions -> filing
    with notifications at every stage transition.
    """

    def __init__(
        self,
        store: Optional[CaseStore] = None,
        efiling_provider: Optional[EFilingProvider] = None,
        notification_sender=None,
        trustate_client: Optional[TrustateClient] = None,
        mapper_config: Optional[MapperConfig] = None,
        cognito_field_map: Optional[CognitoFieldMap] = None,
    ):
        # Core components
        self.store = store or JSONFileStore()
        self.intake = IntakeProcessor()
        self.documents = DocumentManager()
        self.petitions = PetitionGenerator()

        # E-filing
        self.efiling_provider = efiling_provider or TylerEFileProvider(
            api_base_url="",
            api_key="",
            firm_id="",
        )
        self.filing = FilingOrchestrator(self.efiling_provider)

        # Notifications
        sender = notification_sender or SendGridSender()
        self.notifications = NotificationOrchestrator(sender)

        # Trustate Import API — optional. If not provided, Trustate sync
        # is skipped (useful for local dev without credentials).
        self.trustate = trustate_client
        self.mapper_config = mapper_config or MapperConfig()

        # Cognito Forms — used to parse intake/update webhooks
        self.cognito = CognitoFormsProcessor(field_map=cognito_field_map)

        # Workflow engine
        self.engine = WorkflowEngine()
        self._register_handlers()
        self.engine.on_transition(self.notifications.notify_stage_change)

    def _register_handlers(self):
        """Register automation handlers for each workflow stage."""
        self.engine.register_handler(
            CaseStage.INTAKE_IN_PROGRESS, self._on_intake_started
        )
        self.engine.register_handler(
            CaseStage.DOCUMENTS_REQUESTED, self._on_documents_requested
        )
        self.engine.register_handler(
            CaseStage.PETITION_DRAFTING, self._on_petition_drafting
        )
        self.engine.register_handler(
            CaseStage.FILING_IN_PROGRESS, self._on_filing_started
        )

    # ── Public API (called by webhooks/endpoints) ─────────────────────

    def handle_new_client(self, crm_payload: dict[str, Any]) -> ProbateCase:
        """Handle a new client signed by a sales closer.

        This is the entry point — triggered by a CRM webhook when a deal
        is closed-won. Creates the matter in Trustate, then advances the
        case through intake.
        """
        case = self.intake.create_case_from_crm_webhook(crm_payload)
        self.store.save(case)

        # Create the matter in Trustate right away — so partner law firms
        # can see the new case the moment the closer wins the deal.
        self._sync_to_trustate(case, create=True)
        self.store.save(case)

        # Auto-advance to intake
        case = self.engine.advance(case, CaseStage.INTAKE_IN_PROGRESS)
        self.store.save(case)

        logger.info(
            "New client pipeline started: case=%s, client=%s",
            case.id,
            case.petitioner.contact.full_name if case.petitioner else "unknown",
        )
        return case

    def handle_intake_submitted(
        self, case_id: str, form_data: dict[str, Any]
    ) -> ProbateCase:
        """Handle a completed intake form from the client."""
        case = self.store.get(case_id)
        if not case:
            raise ValueError(f"Case {case_id} not found")

        case = self.intake.process_intake_form(case, form_data)
        self.store.save(case)

        # Enrich the Trustate matter with intake data (decedent, beneficiaries, assets).
        self._sync_to_trustate(case, create=False)
        self.store.save(case)

        # Auto-advance: intake complete -> request documents
        case = self.engine.advance(case, CaseStage.DOCUMENTS_REQUESTED)
        self.store.save(case)

        return case

    def _sync_to_trustate(self, case: ProbateCase, *, create: bool) -> None:
        """Push the case to Trustate via the Import API.

        Silently no-ops if no Trustate client is configured (dev mode).
        Errors are logged but do not block the pipeline — the case still
        progresses locally and the sync can be retried later.
        """
        if not self.trustate:
            logger.debug(
                "Trustate client not configured — skipping sync for case %s",
                case.id,
            )
            return

        payload = case_to_trustate_payload(case, self.mapper_config)
        try:
            if create:
                result = self.trustate.create_matter(payload)
                logger.info(
                    "Case %s: created Trustate matter importId=%s",
                    case.id,
                    result.get("importId"),
                )
            else:
                result = self.trustate.upsert_matter(payload)
                logger.info(
                    "Case %s: synced to Trustate importId=%s",
                    case.id,
                    result.get("importId"),
                )
            import_id = result.get("importId")
            if import_id:
                case.trustate_import_id = import_id
        except TrustateAPIError as e:
            logger.error(
                "Case %s: Trustate sync failed (%d): %s",
                case.id,
                e.status_code,
                e.message,
            )
            case.notes.append(
                f"Trustate sync failed ({e.status_code}): {e.message}"
            )

    def handle_cognito_webhook(
        self, payload: dict[str, Any]
    ) -> ProbateCase:
        """Handle any Cognito Forms webhook — first submission OR later update.

        Fires on every entry save: initial submission, client coming back
        to add info, document re-uploads, corrections, etc. Each webhook:
          1. Is parsed into a CognitoUpdate (only non-empty fields).
          2. Is merged into the existing ProbateCase (no destructive overwrites).
          3. Triggers a PUT /import to Trustate with the updated data.
          4. Advances the case stage when milestones are reached
             (intake completion, all docs collected).
        """
        update = self.cognito.parse(payload)

        if not update.case_id:
            raise ValueError(
                "Cognito webhook missing CaseId field — add a hidden field "
                "named 'CaseId' pre-populated from the case_id URL parameter."
            )

        case = self.store.get(update.case_id)
        if not case:
            raise ValueError(
                f"Case {update.case_id} not found — Cognito entry #{update.entry_number} "
                f"references a case that doesn't exist in our system."
            )

        logger.info(
            "Cognito webhook: case=%s entry=#%s fields_updated=%s",
            case.id,
            update.entry_number,
            _summarize_update(update),
        )

        # Merge the update into the case (non-destructive)
        case = self.cognito.merge_into_case(case, update)
        self.store.save(case)

        # Push the updated matter to Trustate
        self._sync_to_trustate(case, create=False)
        self.store.save(case)

        # Auto-advance if we just crossed a milestone
        case = self._maybe_advance_after_cognito(case)
        self.store.save(case)

        return case

    def _maybe_advance_after_cognito(self, case: ProbateCase) -> ProbateCase:
        """Advance the case stage based on what the Cognito update just unlocked."""
        # Crossed from intake -> documents needed?
        if (
            case.stage == CaseStage.INTAKE_IN_PROGRESS
            and case.decedent
            and case.decedent.date_of_death
            and case.assets
            and case.beneficiaries
        ):
            case = self.engine.advance(case, CaseStage.INTAKE_COMPLETE)
            case = self.engine.advance(case, CaseStage.DOCUMENTS_REQUESTED)

        # Crossed from documents_requested -> documents_collected?
        if case.stage == CaseStage.DOCUMENTS_REQUESTED:
            pending = case.get_pending_documents()
            if pending and not any(
                d for d in case.documents if d.status.value == "requested"
            ):
                case = self.engine.advance(case, CaseStage.DOCUMENTS_COLLECTED)
                case = self.engine.advance(case, CaseStage.PETITION_DRAFTING)

        return case

    def handle_document_uploaded(
        self,
        case_id: str,
        doc_type_str: str,
        file_path: str = "",
        file_url: str = "",
    ) -> ProbateCase:
        """Handle a document upload from the client portal."""
        case = self.store.get(case_id)
        if not case:
            raise ValueError(f"Case {case_id} not found")

        doc_type = DocumentType(doc_type_str)
        case = self.documents.record_upload(case, doc_type, file_path, file_url)
        self.store.save(case)

        # If all docs collected, auto-advance to petition drafting
        if case.stage == CaseStage.DOCUMENTS_COLLECTED:
            case = self.engine.advance(case, CaseStage.PETITION_DRAFTING)
            self.store.save(case)

        return case

    def handle_attorney_approval(self, case_id: str) -> ProbateCase:
        """Handle attorney approving the petition — triggers filing."""
        case = self.store.get(case_id)
        if not case:
            raise ValueError(f"Case {case_id} not found")

        case = self.engine.advance(case, CaseStage.ATTORNEY_APPROVED)
        self.store.save(case)

        # Auto-advance to filing
        case = self.engine.advance(case, CaseStage.FILING_IN_PROGRESS)
        self.store.save(case)

        return case

    def handle_attorney_rejection(
        self, case_id: str, notes: str = ""
    ) -> ProbateCase:
        """Handle attorney sending petition back for revisions."""
        case = self.store.get(case_id)
        if not case:
            raise ValueError(f"Case {case_id} not found")

        if notes:
            case.notes.append(f"Attorney revision request: {notes}")

        case = self.engine.advance(case, CaseStage.PETITION_DRAFTING)
        self.store.save(case)
        return case

    def handle_hearing_scheduled(
        self, case_id: str, hearing_date: str, hearing_location: str = ""
    ) -> ProbateCase:
        """Handle court scheduling a hearing."""
        from datetime import datetime

        case = self.store.get(case_id)
        if not case:
            raise ValueError(f"Case {case_id} not found")

        case.court.hearing_date = datetime.fromisoformat(hearing_date)
        case.court.hearing_location = hearing_location
        case = self.engine.advance(case, CaseStage.HEARING_SCHEDULED)
        self.store.save(case)
        return case

    def handle_letters_issued(self, case_id: str) -> ProbateCase:
        """Handle court issuing Letters Testamentary/Administration."""
        case = self.store.get(case_id)
        if not case:
            raise ValueError(f"Case {case_id} not found")

        case = self.engine.advance(case, CaseStage.LETTERS_ISSUED)
        self.store.save(case)
        return case

    def get_case_status(self, case_id: str) -> Optional[dict]:
        """Get a summary of a case's current status."""
        case = self.store.get(case_id)
        if not case:
            return None

        return {
            "case_id": case.id,
            "stage": case.stage.value,
            "probate_type": case.probate_type.value,
            "decedent": case.decedent.full_name if case.decedent else "",
            "petitioner": (
                case.petitioner.contact.full_name if case.petitioner else ""
            ),
            "estate_value": case.total_estate_value,
            "pending_documents": [
                d.doc_type.value for d in case.get_pending_documents()
            ],
            "court_case_number": case.court.case_number,
            "law_firm_id": case.law_firm_id,
            "created_at": case.created_at.isoformat() if hasattr(case.created_at, "isoformat") else str(case.created_at),
            "updated_at": case.updated_at.isoformat() if hasattr(case.updated_at, "isoformat") else str(case.updated_at),
            "stage_history": case.stage_history,
        }

    # ── Internal stage handlers ───────────────────────────────────────

    def _on_intake_started(self, case: ProbateCase) -> ProbateCase:
        """When intake starts, send the intake form to the client."""
        intake_url = self.intake.get_intake_form_url(case)
        logger.info("Case %s: intake form sent — %s", case.id, intake_url)
        return case

    def _on_documents_requested(self, case: ProbateCase) -> ProbateCase:
        """When documents are requested, set up tracking and send requests."""
        case = self.documents.setup_document_requests(case)
        msg = self.documents.generate_document_request_message(case)
        if msg.get("to_email"):
            self.notifications.sender.send_email(
                msg["to_email"], msg["subject"], msg["body"]
            )
        return case

    def _on_petition_drafting(self, case: ProbateCase) -> ProbateCase:
        """Auto-generate petitions when all documents are collected."""
        generated = self.petitions.generate_all(case)
        self._last_generated_petitions = generated  # stash for filing

        # Auto-advance to attorney review
        case.stage = CaseStage.PETITION_GENERATED
        logger.info("Case %s: petitions generated, queued for attorney review", case.id)
        return case

    def _on_filing_started(self, case: ProbateCase) -> ProbateCase:
        """Submit the filing to the court."""
        petitions = getattr(self, "_last_generated_petitions", [])
        if not petitions:
            # Re-generate if needed
            petitions = self.petitions.generate_all(case)

        case, result = self.filing.prepare_and_file(case, petitions)
        logger.info(
            "Case %s: filing result — %s (id=%s)",
            case.id,
            result.status.value,
            result.filing_id,
        )
        return case
