"""Court e-filing integration.

Handles submitting petitions and documents to county courts via
e-filing service providers. Most states use one of:
- Tyler Technologies (Odyssey eFileCA, eFileTexas, etc.)
- File & Serve Xpress
- JEFS (Hawaii)
- State-specific portals

This module abstracts the filing interface so you can plug in
any provider.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from trustate.petitions.generator import GeneratedPetition
from trustate.workflow.models import CaseStage, CourtInfo, ProbateCase

logger = logging.getLogger(__name__)


class FilingStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNDER_REVIEW = "under_review"


@dataclass
class FilingResult:
    status: FilingStatus
    filing_id: str = ""
    case_number: str = ""
    confirmation_url: str = ""
    rejection_reason: str = ""
    submitted_at: Optional[datetime] = None
    fees_charged: float = 0.0


class EFilingProvider(ABC):
    """Abstract interface for court e-filing providers."""

    @abstractmethod
    def submit_filing(
        self,
        court_id: str,
        case: ProbateCase,
        petitions: list[GeneratedPetition],
        supporting_docs: list[str],
    ) -> FilingResult: ...

    @abstractmethod
    def check_status(self, filing_id: str) -> FilingResult: ...

    @abstractmethod
    def get_court_info(self, county: str, state: str) -> CourtInfo: ...

    @abstractmethod
    def get_filing_fees(self, county: str, state: str, filing_type: str) -> float: ...


class TylerEFileProvider(EFilingProvider):
    """Tyler Technologies e-filing integration (eFileCA, eFileTexas, etc.)

    In production, this would make real API calls to the Tyler OFS
    (Online Filing Service) API. This implementation shows the structure
    with placeholder calls you'd replace with actual API integration.
    """

    def __init__(self, api_base_url: str, api_key: str, firm_id: str):
        self.api_base_url = api_base_url
        self.api_key = api_key
        self.firm_id = firm_id

    def submit_filing(
        self,
        court_id: str,
        case: ProbateCase,
        petitions: list[GeneratedPetition],
        supporting_docs: list[str],
    ) -> FilingResult:
        """Submit a filing envelope to the court via Tyler OFS API."""
        logger.info(
            "Submitting e-filing for case %s to court %s",
            case.id,
            court_id,
        )

        # Build the filing envelope
        envelope = self._build_envelope(court_id, case, petitions, supporting_docs)

        # In production: POST to Tyler OFS API
        # response = requests.post(
        #     f"{self.api_base_url}/filing/submit",
        #     headers={"Authorization": f"Bearer {self.api_key}"},
        #     json=envelope,
        # )

        # Placeholder response
        result = FilingResult(
            status=FilingStatus.SUBMITTED,
            filing_id=f"TYL-{case.id[:8]}",
            submitted_at=datetime.utcnow(),
        )

        logger.info("Filing submitted: %s", result.filing_id)
        return result

    def check_status(self, filing_id: str) -> FilingResult:
        """Check the status of a previously submitted filing."""
        logger.info("Checking status for filing %s", filing_id)

        # In production: GET from Tyler OFS API
        # response = requests.get(
        #     f"{self.api_base_url}/filing/{filing_id}/status",
        #     headers={"Authorization": f"Bearer {self.api_key}"},
        # )

        return FilingResult(
            status=FilingStatus.UNDER_REVIEW,
            filing_id=filing_id,
        )

    def get_court_info(self, county: str, state: str) -> CourtInfo:
        """Look up the correct court for a given county/state."""
        # In production this would query the Tyler court directory
        return CourtInfo(
            court_name=f"Superior Court of {county} County",
            county=county,
            state=state,
            efiling_court_id=f"{state}-{county}".upper(),
        )

    def get_filing_fees(self, county: str, state: str, filing_type: str) -> float:
        """Look up filing fees for a given court and filing type."""
        # Common fee ranges — in production, query the Tyler fee schedule API
        base_fees = {
            "CA": 435.0,
            "TX": 320.0,
            "FL": 400.0,
            "NY": 210.0,
            "AZ": 331.0,
            "NV": 267.0,
        }
        return base_fees.get(state.upper(), 350.0)

    def _build_envelope(
        self,
        court_id: str,
        case: ProbateCase,
        petitions: list[GeneratedPetition],
        supporting_docs: list[str],
    ) -> dict[str, Any]:
        """Build the filing envelope payload for the Tyler API."""
        return {
            "court_id": court_id,
            "case_category": "Probate",
            "case_type": case.probate_type.value,
            "filer_id": self.firm_id,
            "parties": [
                {
                    "role": "Decedent",
                    "name": case.decedent.full_name if case.decedent else "",
                },
                {
                    "role": "Petitioner",
                    "name": (
                        case.petitioner.contact.full_name
                        if case.petitioner
                        else ""
                    ),
                },
            ],
            "documents": [
                {
                    "type": p.document_type.value,
                    "title": p.title,
                    "data": p.content,
                }
                for p in petitions
            ],
            "supporting_documents": supporting_docs,
        }


class FileServeXpressProvider(EFilingProvider):
    """File & Serve Xpress e-filing integration.

    Structure for an alternative provider. Implement when needed.
    """

    def __init__(self, api_base_url: str, credentials: dict):
        self.api_base_url = api_base_url
        self.credentials = credentials

    def submit_filing(self, court_id, case, petitions, supporting_docs):
        raise NotImplementedError("File & Serve Xpress integration not yet implemented")

    def check_status(self, filing_id):
        raise NotImplementedError

    def get_court_info(self, county, state):
        raise NotImplementedError

    def get_filing_fees(self, county, state, filing_type):
        raise NotImplementedError


class FilingOrchestrator:
    """Orchestrates the full filing process for a case."""

    def __init__(self, provider: EFilingProvider):
        self.provider = provider

    def prepare_and_file(
        self,
        case: ProbateCase,
        petitions: list[GeneratedPetition],
    ) -> tuple[ProbateCase, FilingResult]:
        """Prepare the filing, determine fees, submit, and update the case."""
        county = case.court.county
        state = case.court.state

        if not county and case.decedent:
            county = case.decedent.county_of_domicile
            state = case.decedent.last_state

        # Look up court if not already set
        if not case.court.efiling_court_id:
            court_info = self.provider.get_court_info(county, state)
            case.court = court_info

        # Get filing fees
        case.court.filing_fee = self.provider.get_filing_fees(
            county, state, case.probate_type.value
        )

        # Collect supporting document paths
        supporting_docs = [
            d.file_path or d.file_url
            for d in case.documents
            if d.file_path or d.file_url
        ]

        # Submit
        case.stage = CaseStage.FILING_IN_PROGRESS
        result = self.provider.submit_filing(
            case.court.efiling_court_id,
            case,
            petitions,
            supporting_docs,
        )

        if result.status in (FilingStatus.SUBMITTED, FilingStatus.ACCEPTED):
            case.stage = CaseStage.FILED_WITH_COURT
            if result.case_number:
                case.court.case_number = result.case_number
            logger.info("Case %s filed successfully: %s", case.id, result.filing_id)
        else:
            logger.warning(
                "Case %s filing issue: %s — %s",
                case.id,
                result.status.value,
                result.rejection_reason,
            )

        return case, result
