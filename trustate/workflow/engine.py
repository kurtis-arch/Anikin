"""Workflow engine — the state machine that drives cases through every stage.

Each stage transition triggers the appropriate automation actions:
sending notifications, requesting documents, generating petitions, filing, etc.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Optional

from trustate.workflow.models import CaseStage, ProbateCase

logger = logging.getLogger(__name__)

# Valid transitions: current_stage -> set of allowed next stages
VALID_TRANSITIONS: dict[CaseStage, set[CaseStage]] = {
    CaseStage.CLIENT_SIGNED: {CaseStage.INTAKE_IN_PROGRESS},
    CaseStage.INTAKE_IN_PROGRESS: {CaseStage.INTAKE_COMPLETE},
    CaseStage.INTAKE_COMPLETE: {CaseStage.DOCUMENTS_REQUESTED},
    CaseStage.DOCUMENTS_REQUESTED: {CaseStage.DOCUMENTS_COLLECTED},
    CaseStage.DOCUMENTS_COLLECTED: {CaseStage.PETITION_DRAFTING},
    CaseStage.PETITION_DRAFTING: {CaseStage.PETITION_GENERATED},
    CaseStage.PETITION_GENERATED: {CaseStage.ATTORNEY_REVIEW},
    CaseStage.ATTORNEY_REVIEW: {
        CaseStage.ATTORNEY_APPROVED,
        CaseStage.PETITION_DRAFTING,  # kicked back for revisions
    },
    CaseStage.ATTORNEY_APPROVED: {CaseStage.FILING_IN_PROGRESS},
    CaseStage.FILING_IN_PROGRESS: {
        CaseStage.FILED_WITH_COURT,
        CaseStage.ATTORNEY_REVIEW,  # filing rejected, needs revision
    },
    CaseStage.FILED_WITH_COURT: {CaseStage.HEARING_SCHEDULED},
    CaseStage.HEARING_SCHEDULED: {CaseStage.LETTERS_ISSUED},
    CaseStage.LETTERS_ISSUED: {CaseStage.CASE_CLOSED},
}

# Type alias for stage handler functions
StageHandler = Callable[[ProbateCase], ProbateCase]


class WorkflowEngine:
    """Drives a ProbateCase through its lifecycle stages.

    Register handlers for each stage transition. When a case advances,
    the engine validates the transition, runs the handler, records history,
    and auto-advances to the next stage if the handler signals readiness.
    """

    def __init__(self):
        self._handlers: dict[CaseStage, StageHandler] = {}
        self._on_transition_callbacks: list[
            Callable[[ProbateCase, CaseStage, CaseStage], None]
        ] = []

    def register_handler(self, stage: CaseStage, handler: StageHandler):
        """Register a function to run when a case enters a stage."""
        self._handlers[stage] = handler

    def on_transition(
        self, callback: Callable[[ProbateCase, CaseStage, CaseStage], None]
    ):
        """Register a callback that fires on every stage transition."""
        self._on_transition_callbacks.append(callback)

    def advance(
        self, case: ProbateCase, to_stage: CaseStage
    ) -> ProbateCase:
        """Move a case to the next stage, running the registered handler."""
        from_stage = case.stage

        if to_stage not in VALID_TRANSITIONS.get(from_stage, set()):
            raise InvalidTransitionError(
                f"Cannot transition from {from_stage.value} to {to_stage.value}"
            )

        logger.info(
            "Case %s: transitioning %s -> %s",
            case.id,
            from_stage.value,
            to_stage.value,
        )

        # Record history
        case.stage_history.append(
            {
                "from": from_stage.value,
                "to": to_stage.value,
                "timestamp": datetime.utcnow().isoformat(),
            }
        )
        case.stage = to_stage
        case.updated_at = datetime.utcnow()

        # Fire transition callbacks (for notifications, logging, etc.)
        for cb in self._on_transition_callbacks:
            try:
                cb(case, from_stage, to_stage)
            except Exception:
                logger.exception("Transition callback failed for case %s", case.id)

        # Run the stage handler
        handler = self._handlers.get(to_stage)
        if handler:
            try:
                case = handler(case)
            except Exception:
                logger.exception(
                    "Handler failed for stage %s on case %s",
                    to_stage.value,
                    case.id,
                )
                raise

        return case

    def advance_through(
        self, case: ProbateCase, to_stage: CaseStage
    ) -> ProbateCase:
        """Advance a case through all intermediate stages up to to_stage.

        Useful for auto-progressing when all data is ready — e.g., if intake
        data is complete, skip straight from CLIENT_SIGNED through to
        DOCUMENTS_REQUESTED in one call.
        """
        stage_order = list(CaseStage)
        current_idx = stage_order.index(case.stage)
        target_idx = stage_order.index(to_stage)

        if target_idx <= current_idx:
            raise InvalidTransitionError(
                f"Target stage {to_stage.value} is not ahead of current "
                f"stage {case.stage.value}"
            )

        for next_stage in stage_order[current_idx + 1 : target_idx + 1]:
            if next_stage in VALID_TRANSITIONS.get(case.stage, set()):
                case = self.advance(case, next_stage)
            else:
                break

        return case

    def get_next_stages(self, case: ProbateCase) -> set[CaseStage]:
        """Return the valid next stages for a case."""
        return VALID_TRANSITIONS.get(case.stage, set())


class InvalidTransitionError(Exception):
    pass
