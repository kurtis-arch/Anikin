"""Data models for the probate case workflow.

Defines all the core entities: cases, clients, decedents, assets,
beneficiaries, and documents that flow through the automation pipeline.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Optional


class CaseStage(str, Enum):
    """Every stage a probate case moves through, from signing to close."""

    CLIENT_SIGNED = "client_signed"
    INTAKE_IN_PROGRESS = "intake_in_progress"
    INTAKE_COMPLETE = "intake_complete"
    DOCUMENTS_REQUESTED = "documents_requested"
    DOCUMENTS_COLLECTED = "documents_collected"
    PETITION_DRAFTING = "petition_drafting"
    PETITION_GENERATED = "petition_generated"
    ATTORNEY_REVIEW = "attorney_review"
    ATTORNEY_APPROVED = "attorney_approved"
    FILING_IN_PROGRESS = "filing_in_progress"
    FILED_WITH_COURT = "filed_with_court"
    HEARING_SCHEDULED = "hearing_scheduled"
    LETTERS_ISSUED = "letters_issued"
    CASE_CLOSED = "case_closed"


class ProbateType(str, Enum):
    """Types of probate proceedings."""

    FORMAL = "formal"
    INFORMAL = "informal"
    SUMMARY = "summary"
    SMALL_ESTATE_AFFIDAVIT = "small_estate_affidavit"
    ANCILLARY = "ancillary"


class AssetType(str, Enum):
    REAL_PROPERTY = "real_property"
    BANK_ACCOUNT = "bank_account"
    INVESTMENT_ACCOUNT = "investment_account"
    RETIREMENT_ACCOUNT = "retirement_account"
    LIFE_INSURANCE = "life_insurance"
    VEHICLE = "vehicle"
    PERSONAL_PROPERTY = "personal_property"
    BUSINESS_INTEREST = "business_interest"
    DIGITAL_ASSET = "digital_asset"
    OTHER = "other"


class DocumentType(str, Enum):
    DEATH_CERTIFICATE = "death_certificate"
    WILL = "will"
    TRUST_DOCUMENT = "trust_document"
    DEED = "deed"
    BANK_STATEMENT = "bank_statement"
    INVESTMENT_STATEMENT = "investment_statement"
    VEHICLE_TITLE = "vehicle_title"
    INSURANCE_POLICY = "insurance_policy"
    TAX_RETURN = "tax_return"
    MARRIAGE_CERTIFICATE = "marriage_certificate"
    DIVORCE_DECREE = "divorce_decree"
    GOVERNMENT_ID = "government_id"
    PETITION = "petition"
    COURT_ORDER = "court_order"
    LETTERS_TESTAMENTARY = "letters_testamentary"
    LETTERS_OF_ADMINISTRATION = "letters_of_administration"
    INVENTORY_AND_APPRAISAL = "inventory_and_appraisal"
    NOTICE_TO_CREDITORS = "notice_to_creditors"
    PROOF_OF_SERVICE = "proof_of_service"
    OTHER = "other"


class DocumentStatus(str, Enum):
    REQUESTED = "requested"
    UPLOADED = "uploaded"
    VERIFIED = "verified"
    REJECTED = "rejected"


class RelationshipToDecedent(str, Enum):
    SPOUSE = "spouse"
    CHILD = "child"
    PARENT = "parent"
    SIBLING = "sibling"
    GRANDCHILD = "grandchild"
    OTHER_RELATIVE = "other_relative"
    NON_RELATIVE = "non_relative"


@dataclass
class ContactInfo:
    first_name: str
    last_name: str
    email: str
    phone: str
    address_line1: str = ""
    address_line2: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    county: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    @property
    def full_address(self) -> str:
        parts = [self.address_line1]
        if self.address_line2:
            parts.append(self.address_line2)
        parts.append(f"{self.city}, {self.state} {self.zip_code}")
        return "\n".join(parts)


@dataclass
class Decedent:
    first_name: str
    last_name: str
    date_of_birth: Optional[date] = None
    date_of_death: Optional[date] = None
    ssn_last_four: str = ""
    last_address_line1: str = ""
    last_address_line2: str = ""
    last_city: str = ""
    last_state: str = ""
    last_zip_code: str = ""
    last_county: str = ""
    had_will: Optional[bool] = None
    will_date: Optional[date] = None
    marital_status: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    @property
    def county_of_domicile(self) -> str:
        return self.last_county


@dataclass
class Beneficiary:
    contact: ContactInfo
    relationship: RelationshipToDecedent
    share_percentage: float = 0.0
    is_minor: bool = False
    is_incapacitated: bool = False
    guardian_name: str = ""


@dataclass
class Asset:
    asset_type: AssetType
    description: str
    estimated_value: float = 0.0
    account_number_last_four: str = ""
    institution_name: str = ""
    address: str = ""
    appraised_value: Optional[float] = None
    appraisal_date: Optional[date] = None


@dataclass
class Document:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    doc_type: DocumentType = DocumentType.OTHER
    status: DocumentStatus = DocumentStatus.REQUESTED
    file_path: str = ""
    file_url: str = ""
    uploaded_at: Optional[datetime] = None
    verified_at: Optional[datetime] = None
    notes: str = ""


@dataclass
class PetitionerInfo:
    """The person petitioning the court (usually the executor/administrator)."""

    contact: ContactInfo
    relationship: RelationshipToDecedent
    is_nominated_in_will: bool = False
    has_priority: bool = False
    bond_amount: float = 0.0
    bond_waived: bool = False


@dataclass
class CourtInfo:
    court_name: str = ""
    county: str = ""
    state: str = ""
    case_number: str = ""
    department: str = ""
    judge_name: str = ""
    hearing_date: Optional[datetime] = None
    hearing_location: str = ""
    filing_fee: float = 0.0
    efiling_court_id: str = ""


@dataclass
class ProbateCase:
    """The master record for a probate case flowing through the pipeline."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    stage: CaseStage = CaseStage.CLIENT_SIGNED
    probate_type: ProbateType = ProbateType.FORMAL
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    # People
    decedent: Optional[Decedent] = None
    petitioner: Optional[PetitionerInfo] = None
    beneficiaries: list[Beneficiary] = field(default_factory=list)

    # Assets & Documents
    assets: list[Asset] = field(default_factory=list)
    documents: list[Document] = field(default_factory=list)

    # Court
    court: CourtInfo = field(default_factory=CourtInfo)

    # Partner law firm handling this case
    law_firm_id: str = ""
    assigned_attorney_email: str = ""

    # Sales / intake tracking
    sales_closer_id: str = ""
    crm_deal_id: str = ""
    aircall_call_id: str = ""

    # Trustate Import API tracking
    trustate_import_id: str = ""

    # PandaDoc fee agreement tracking
    pandadoc_document_id: str = ""
    pandadoc_status: str = ""  # e.g. document.sent, document.completed
    fee_agreement_signed: bool = False
    fee_agreement_signed_at: Optional[datetime] = None

    # Cognito Forms intake tracking
    cognito_entry_id: str = ""
    intake_email_sent_at: Optional[datetime] = None

    # Automation metadata
    stage_history: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    estimated_estate_value: float = 0.0

    @property
    def total_estate_value(self) -> float:
        return sum(a.estimated_value for a in self.assets)

    def get_documents_by_type(self, doc_type: DocumentType) -> list[Document]:
        return [d for d in self.documents if d.doc_type == doc_type]

    def get_pending_documents(self) -> list[Document]:
        return [d for d in self.documents if d.status == DocumentStatus.REQUESTED]

    def all_documents_collected(self) -> bool:
        return all(
            d.status in (DocumentStatus.UPLOADED, DocumentStatus.VERIFIED)
            for d in self.documents
        )
