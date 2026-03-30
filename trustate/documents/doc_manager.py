"""Document collection & tracking automation.

After intake is complete, this module:
1. Determines which documents are required for the case
2. Sends document request emails/SMS to the client
3. Tracks upload status
4. Validates uploaded documents
5. Signals when all required docs are collected
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from trustate.workflow.models import (
    CaseStage,
    Document,
    DocumentStatus,
    DocumentType,
    ProbateCase,
    ProbateType,
)

logger = logging.getLogger(__name__)

# Required documents by probate type
REQUIRED_DOCUMENTS: dict[ProbateType, list[DocumentType]] = {
    ProbateType.FORMAL: [
        DocumentType.DEATH_CERTIFICATE,
        DocumentType.GOVERNMENT_ID,
        DocumentType.WILL,
    ],
    ProbateType.INFORMAL: [
        DocumentType.DEATH_CERTIFICATE,
        DocumentType.GOVERNMENT_ID,
    ],
    ProbateType.SUMMARY: [
        DocumentType.DEATH_CERTIFICATE,
        DocumentType.GOVERNMENT_ID,
    ],
    ProbateType.SMALL_ESTATE_AFFIDAVIT: [
        DocumentType.DEATH_CERTIFICATE,
        DocumentType.GOVERNMENT_ID,
    ],
    ProbateType.ANCILLARY: [
        DocumentType.DEATH_CERTIFICATE,
        DocumentType.GOVERNMENT_ID,
        DocumentType.COURT_ORDER,
    ],
}

# Additional documents required based on asset types
ASSET_DOCUMENTS: dict[str, list[DocumentType]] = {
    "real_property": [DocumentType.DEED],
    "bank_account": [DocumentType.BANK_STATEMENT],
    "investment_account": [DocumentType.INVESTMENT_STATEMENT],
    "vehicle": [DocumentType.VEHICLE_TITLE],
    "life_insurance": [DocumentType.INSURANCE_POLICY],
}


class DocumentManager:
    """Manages the document collection lifecycle for a probate case."""

    def determine_required_documents(self, case: ProbateCase) -> list[Document]:
        """Figure out exactly which documents are needed for this case."""
        required_types: list[DocumentType] = []

        # Base documents for the probate type
        base_docs = REQUIRED_DOCUMENTS.get(case.probate_type, [])
        required_types.extend(base_docs)

        # If the decedent had a will, always need it
        if case.decedent and case.decedent.had_will:
            if DocumentType.WILL not in required_types:
                required_types.append(DocumentType.WILL)
        elif DocumentType.WILL in required_types and case.decedent and case.decedent.had_will is False:
            required_types.remove(DocumentType.WILL)

        # Asset-specific documents
        for asset in case.assets:
            asset_docs = ASSET_DOCUMENTS.get(asset.asset_type.value, [])
            for doc_type in asset_docs:
                if doc_type not in required_types:
                    required_types.append(doc_type)

        # Marriage certificate if married
        if case.decedent and case.decedent.marital_status in ("married", "widowed"):
            if DocumentType.MARRIAGE_CERTIFICATE not in required_types:
                required_types.append(DocumentType.MARRIAGE_CERTIFICATE)

        # Create Document records
        docs = []
        for doc_type in required_types:
            existing = case.get_documents_by_type(doc_type)
            if not existing:
                docs.append(
                    Document(doc_type=doc_type, status=DocumentStatus.REQUESTED)
                )

        return docs

    def setup_document_requests(self, case: ProbateCase) -> ProbateCase:
        """Create all required document records and mark case as DOCUMENTS_REQUESTED."""
        new_docs = self.determine_required_documents(case)
        case.documents.extend(new_docs)
        case.stage = CaseStage.DOCUMENTS_REQUESTED

        logger.info(
            "Case %s: %d documents requested — %s",
            case.id,
            len(new_docs),
            ", ".join(d.doc_type.value for d in new_docs),
        )
        return case

    def record_upload(
        self,
        case: ProbateCase,
        doc_type: DocumentType,
        file_path: str = "",
        file_url: str = "",
    ) -> ProbateCase:
        """Record that a document has been uploaded by the client."""
        for doc in case.documents:
            if doc.doc_type == doc_type and doc.status == DocumentStatus.REQUESTED:
                doc.status = DocumentStatus.UPLOADED
                doc.file_path = file_path
                doc.file_url = file_url
                doc.uploaded_at = datetime.utcnow()
                logger.info("Case %s: %s uploaded", case.id, doc_type.value)
                break
        else:
            # Document wasn't in the list — add it
            case.documents.append(
                Document(
                    doc_type=doc_type,
                    status=DocumentStatus.UPLOADED,
                    file_path=file_path,
                    file_url=file_url,
                    uploaded_at=datetime.utcnow(),
                )
            )

        # Check if all documents are collected
        if case.all_documents_collected():
            case.stage = CaseStage.DOCUMENTS_COLLECTED
            logger.info("Case %s: all documents collected!", case.id)

        return case

    def get_missing_documents(self, case: ProbateCase) -> list[Document]:
        """Return documents that still need to be uploaded."""
        return case.get_pending_documents()

    def generate_document_request_message(self, case: ProbateCase) -> dict:
        """Generate the email/SMS content requesting documents from the client."""
        pending = self.get_missing_documents(case)
        if not pending:
            return {"subject": "", "body": ""}

        petitioner_name = ""
        if case.petitioner:
            petitioner_name = case.petitioner.contact.first_name

        doc_names = {
            DocumentType.DEATH_CERTIFICATE: "Certified Death Certificate",
            DocumentType.WILL: "Original Will (or certified copy)",
            DocumentType.GOVERNMENT_ID: "Government-issued photo ID (driver's license or passport)",
            DocumentType.DEED: "Property deed(s)",
            DocumentType.BANK_STATEMENT: "Recent bank statement(s)",
            DocumentType.INVESTMENT_STATEMENT: "Investment account statement(s)",
            DocumentType.VEHICLE_TITLE: "Vehicle title(s)",
            DocumentType.INSURANCE_POLICY: "Life insurance policy document(s)",
            DocumentType.MARRIAGE_CERTIFICATE: "Marriage certificate",
            DocumentType.TAX_RETURN: "Most recent tax return",
        }

        doc_list = "\n".join(
            f"  - {doc_names.get(d.doc_type, d.doc_type.value)}"
            for d in pending
        )

        upload_url = f"https://portal.trustate.com/upload?case_id={case.id}"

        body = (
            f"Hi {petitioner_name},\n\n"
            f"Thank you for choosing Trustate to help with the probate process. "
            f"To move forward, we need the following documents:\n\n"
            f"{doc_list}\n\n"
            f"You can securely upload them here:\n{upload_url}\n\n"
            f"If you have any questions about what's needed, simply reply to this "
            f"email or call us.\n\n"
            f"Best regards,\nThe Trustate Team"
        )

        return {
            "subject": "Documents Needed for Your Probate Case",
            "body": body,
            "to_email": case.petitioner.contact.email if case.petitioner else "",
            "upload_url": upload_url,
        }
