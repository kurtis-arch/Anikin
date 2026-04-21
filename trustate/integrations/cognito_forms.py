"""Cognito Forms integration.

Cognito Forms fires a webhook on EVERY entry save — first submission,
later edits, and each time the client uploads more documents via their
unique entry link. This module parses those webhooks and turns each one
into an incremental update to our ProbateCase (and, in turn, to the
Trustate matter via PUT /import).

Setup on the Cognito Forms side:
    1. Form > Submission Settings > Webhook
       Point it at: POST https://<your-api>/webhooks/cognito/intake
    2. Enable "Send updates on entry changes" (not just new submissions)
    3. Add a hidden field named "CaseId" and pre-populate it from the
       URL parameter case_id (Field > Hidden > Default Value: =QueryString("case_id"))
       This is how we link the Cognito entry back to our ProbateCase.

Cognito Forms webhook payload shape (relevant subset):
    {
      "Form": {"Id": "...", "Name": "Probate Intake"},
      "Id": 42,                   # entry id within the form
      "Number": "42",             # human-readable entry number
      "DateSubmitted": "2026-04-21T12:00:00Z",
      "DateUpdated": "2026-04-22T09:00:00Z",
      "CaseId": "uuid",           # our hidden field
      "Client": {"First": "Jane", "Last": "Doe", "Email": "..."},
      "Decedent": {...},
      "Assets": [{...}, {...}],           # repeating section
      "Beneficiaries": [{...}],
      "DeathCertificate": [                # file upload field
          {"File": "cert.pdf", "Url": "https://www.cognitoforms.com/...",
           "ContentType": "application/pdf", "Size": 12345}
      ],
      ...
    }

The exact field names depend on how your Cognito form is structured.
Map them via CognitoFieldMap below.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field as dc_field
from datetime import date, datetime
from typing import Any, Optional

from trustate.workflow.models import (
    Asset,
    AssetType,
    Beneficiary,
    ContactInfo,
    Decedent,
    Document,
    DocumentStatus,
    DocumentType,
    PetitionerInfo,
    ProbateCase,
    RelationshipToDecedent,
)

logger = logging.getLogger(__name__)


# ── Field mapping configuration ─────────────────────────────────────

@dataclass
class CognitoFieldMap:
    """Maps Cognito Forms field names to our internal model.

    Change these to match the exact field labels in your Cognito form.
    Every field is optional — if a field isn't present in a given webhook
    payload, it's simply skipped (no overwrite).
    """

    # Top-level linking field — the hidden CaseId pre-filled from ?case_id=
    case_id_field: str = "CaseId"

    # Client (petitioner) section — e.g. a Name field "Client"
    client_section: str = "Client"

    # Decedent section — name, DOB, DOD, address, marital status, will info
    decedent_section: str = "Decedent"

    # Repeating sections — these come in as arrays
    assets_section: str = "Assets"
    beneficiaries_section: str = "Beneficiaries"
    liabilities_section: str = "Liabilities"

    # File upload fields — map Cognito field name -> our DocumentType
    file_upload_fields: dict[str, DocumentType] = dc_field(
        default_factory=lambda: {
            "DeathCertificate": DocumentType.DEATH_CERTIFICATE,
            "Will": DocumentType.WILL,
            "GovernmentId": DocumentType.GOVERNMENT_ID,
            "Deed": DocumentType.DEED,
            "BankStatement": DocumentType.BANK_STATEMENT,
            "InvestmentStatement": DocumentType.INVESTMENT_STATEMENT,
            "VehicleTitle": DocumentType.VEHICLE_TITLE,
            "InsurancePolicy": DocumentType.INSURANCE_POLICY,
            "MarriageCertificate": DocumentType.MARRIAGE_CERTIFICATE,
            "TaxReturn": DocumentType.TAX_RETURN,
            "TrustDocument": DocumentType.TRUST_DOCUMENT,
        }
    )


# Map free-text asset type strings (as entered in Cognito) to our enum.
_ASSET_TYPE_ALIASES: dict[str, AssetType] = {
    "real property": AssetType.REAL_PROPERTY,
    "real estate": AssetType.REAL_PROPERTY,
    "home": AssetType.REAL_PROPERTY,
    "house": AssetType.REAL_PROPERTY,
    "bank account": AssetType.BANK_ACCOUNT,
    "checking": AssetType.BANK_ACCOUNT,
    "savings": AssetType.BANK_ACCOUNT,
    "investment": AssetType.INVESTMENT_ACCOUNT,
    "investment account": AssetType.INVESTMENT_ACCOUNT,
    "brokerage": AssetType.INVESTMENT_ACCOUNT,
    "retirement": AssetType.RETIREMENT_ACCOUNT,
    "retirement account": AssetType.RETIREMENT_ACCOUNT,
    "ira": AssetType.RETIREMENT_ACCOUNT,
    "401k": AssetType.RETIREMENT_ACCOUNT,
    "life insurance": AssetType.LIFE_INSURANCE,
    "vehicle": AssetType.VEHICLE,
    "car": AssetType.VEHICLE,
    "auto": AssetType.VEHICLE,
    "personal property": AssetType.PERSONAL_PROPERTY,
    "business": AssetType.BUSINESS_INTEREST,
    "business interest": AssetType.BUSINESS_INTEREST,
    "digital": AssetType.DIGITAL_ASSET,
}

_RELATIONSHIP_ALIASES: dict[str, RelationshipToDecedent] = {
    "spouse": RelationshipToDecedent.SPOUSE,
    "husband": RelationshipToDecedent.SPOUSE,
    "wife": RelationshipToDecedent.SPOUSE,
    "child": RelationshipToDecedent.CHILD,
    "son": RelationshipToDecedent.CHILD,
    "daughter": RelationshipToDecedent.CHILD,
    "parent": RelationshipToDecedent.PARENT,
    "father": RelationshipToDecedent.PARENT,
    "mother": RelationshipToDecedent.PARENT,
    "sibling": RelationshipToDecedent.SIBLING,
    "brother": RelationshipToDecedent.SIBLING,
    "sister": RelationshipToDecedent.SIBLING,
    "grandchild": RelationshipToDecedent.GRANDCHILD,
}


# ── Parsed update record ────────────────────────────────────────────


@dataclass
class CognitoUpdate:
    """Normalized representation of one Cognito Forms webhook.

    Only fields that were actually present in the webhook payload are
    populated — everything else stays None so the merger knows not to
    overwrite existing case data with blanks.
    """

    case_id: str = ""
    entry_id: str = ""
    entry_number: str = ""
    form_name: str = ""

    # Partial updates — only set if present in the webhook
    client: Optional[dict[str, Any]] = None
    decedent: Optional[dict[str, Any]] = None
    assets: Optional[list[dict[str, Any]]] = None
    beneficiaries: Optional[list[dict[str, Any]]] = None
    documents: list[Document] = dc_field(default_factory=list)


# ── Parser ──────────────────────────────────────────────────────────


class CognitoFormsProcessor:
    """Parse Cognito Forms webhooks and merge them into a ProbateCase."""

    def __init__(self, field_map: Optional[CognitoFieldMap] = None):
        self.field_map = field_map or CognitoFieldMap()

    def parse(self, payload: dict[str, Any]) -> CognitoUpdate:
        """Turn a raw Cognito webhook payload into a CognitoUpdate."""
        fm = self.field_map

        update = CognitoUpdate(
            case_id=str(payload.get(fm.case_id_field) or ""),
            entry_id=str(payload.get("Id") or ""),
            entry_number=str(payload.get("Number") or ""),
            form_name=(payload.get("Form") or {}).get("Name") or "",
        )

        if fm.client_section in payload:
            update.client = self._parse_person(payload[fm.client_section])

        if fm.decedent_section in payload:
            update.decedent = self._parse_decedent(payload[fm.decedent_section])

        if fm.assets_section in payload:
            update.assets = [
                self._parse_asset(a) for a in payload[fm.assets_section] or []
            ]

        if fm.beneficiaries_section in payload:
            update.beneficiaries = [
                self._parse_beneficiary(b)
                for b in payload[fm.beneficiaries_section] or []
            ]

        # Collect uploaded files
        for field_name, doc_type in fm.file_upload_fields.items():
            files = payload.get(field_name)
            if not files:
                continue
            # Cognito always returns file fields as arrays, even for single-file
            if isinstance(files, dict):
                files = [files]
            for f in files:
                update.documents.append(
                    Document(
                        doc_type=doc_type,
                        status=DocumentStatus.UPLOADED,
                        file_url=f.get("Url", ""),
                        file_path=f.get("File", ""),
                        uploaded_at=datetime.utcnow(),
                        notes=f"From Cognito entry #{update.entry_number}",
                    )
                )

        return update

    def merge_into_case(
        self, case: ProbateCase, update: CognitoUpdate
    ) -> ProbateCase:
        """Apply a Cognito update to a ProbateCase.

        Non-destructive: only overwrites a field if the update actually has a
        non-empty value for it. Lists (assets, beneficiaries, documents) are
        merged by a simple "replace if the form sent a new list" strategy
        for assets/beneficiaries, and "append if new" for documents.
        """
        if update.client:
            case.petitioner = self._merge_petitioner(case.petitioner, update.client)

        if update.decedent:
            case.decedent = self._merge_decedent(case.decedent, update.decedent)

        if update.assets is not None:
            case.assets = [self._dict_to_asset(a) for a in update.assets]
            case.estimated_estate_value = case.total_estate_value

        if update.beneficiaries is not None:
            case.beneficiaries = [
                self._dict_to_beneficiary(b) for b in update.beneficiaries
            ]

        # Append new document uploads (dedupe by file_url)
        existing_urls = {d.file_url for d in case.documents if d.file_url}
        for doc in update.documents:
            if doc.file_url and doc.file_url in existing_urls:
                continue
            # If we already have a REQUESTED doc of this type, upgrade it
            for existing in case.documents:
                if (
                    existing.doc_type == doc.doc_type
                    and existing.status == DocumentStatus.REQUESTED
                ):
                    existing.status = DocumentStatus.UPLOADED
                    existing.file_url = doc.file_url
                    existing.file_path = doc.file_path
                    existing.uploaded_at = doc.uploaded_at
                    break
            else:
                case.documents.append(doc)

        case.updated_at = datetime.utcnow()
        return case

    # ── Field-level parsers ─────────────────────────────────────────

    def _parse_person(self, section: Any) -> dict[str, Any]:
        """Parse a Cognito Name + contact section (Client, Trustee, etc.)."""
        if not isinstance(section, dict):
            return {}
        # Cognito Name fields typically have First, Middle, Last subfields
        name = section.get("Name") if isinstance(section.get("Name"), dict) else section
        addr = section.get("Address") or {}
        return {
            "first_name": name.get("First") or section.get("First") or "",
            "last_name": name.get("Last") or section.get("Last") or "",
            "email": section.get("Email") or section.get("EmailAddress") or "",
            "phone": section.get("Phone") or section.get("PhoneNumber") or "",
            "address_line1": addr.get("Line1") or addr.get("Street") or "",
            "address_line2": addr.get("Line2") or "",
            "city": addr.get("City") or "",
            "state": addr.get("State") or "",
            "zip_code": addr.get("PostalCode") or addr.get("Zip") or "",
            "county": section.get("County") or "",
        }

    def _parse_decedent(self, section: Any) -> dict[str, Any]:
        if not isinstance(section, dict):
            return {}
        person = self._parse_person(section)
        return {
            **person,
            "date_of_birth": _parse_date(
                section.get("DateOfBirth") or section.get("DOB")
            ),
            "date_of_death": _parse_date(
                section.get("DateOfDeath") or section.get("DOD")
            ),
            "ssn_last_four": section.get("SSNLastFour") or "",
            "had_will": section.get("HadWill"),
            "will_date": _parse_date(section.get("WillDate")),
            "marital_status": (section.get("MaritalStatus") or "").lower(),
        }

    def _parse_asset(self, row: Any) -> dict[str, Any]:
        if not isinstance(row, dict):
            return {}
        return {
            "type": row.get("Type") or row.get("AssetType") or "",
            "description": row.get("Description") or "",
            "estimated_value": _parse_money(
                row.get("EstimatedValue") or row.get("Value")
            ),
            "institution": row.get("Institution") or "",
            "account_last_four": row.get("AccountLastFour") or "",
            "address": row.get("Address") or "",
        }

    def _parse_beneficiary(self, row: Any) -> dict[str, Any]:
        if not isinstance(row, dict):
            return {}
        person = self._parse_person(row)
        return {
            **person,
            "relationship": (row.get("Relationship") or "").lower(),
            "share_percentage": _parse_money(row.get("SharePercentage")),
            "is_minor": bool(row.get("IsMinor")),
        }

    # ── Merge helpers ───────────────────────────────────────────────

    @staticmethod
    def _merge_petitioner(
        existing: Optional[PetitionerInfo], update: dict[str, Any]
    ) -> PetitionerInfo:
        contact = existing.contact if existing else ContactInfo(
            first_name="", last_name="", email="", phone=""
        )
        for k, v in update.items():
            if v and hasattr(contact, k):
                setattr(contact, k, v)
        if existing:
            existing.contact = contact
            return existing
        return PetitionerInfo(
            contact=contact, relationship=RelationshipToDecedent.CHILD
        )

    @staticmethod
    def _merge_decedent(
        existing: Optional[Decedent], update: dict[str, Any]
    ) -> Decedent:
        d = existing or Decedent(first_name="", last_name="")
        # Map contact-style keys from _parse_person back to Decedent fields
        remap = {
            "address_line1": "last_address_line1",
            "address_line2": "last_address_line2",
            "city": "last_city",
            "state": "last_state",
            "zip_code": "last_zip_code",
            "county": "last_county",
        }
        for k, v in update.items():
            target = remap.get(k, k)
            if v in (None, "") or not hasattr(d, target):
                continue
            setattr(d, target, v)
        return d

    @staticmethod
    def _dict_to_asset(data: dict[str, Any]) -> Asset:
        asset_type = _ASSET_TYPE_ALIASES.get(
            (data.get("type") or "").strip().lower(), AssetType.OTHER
        )
        return Asset(
            asset_type=asset_type,
            description=data.get("description", ""),
            estimated_value=float(data.get("estimated_value") or 0),
            institution_name=data.get("institution", ""),
            account_number_last_four=data.get("account_last_four", ""),
            address=data.get("address", ""),
        )

    @staticmethod
    def _dict_to_beneficiary(data: dict[str, Any]) -> Beneficiary:
        rel = _RELATIONSHIP_ALIASES.get(
            (data.get("relationship") or "").strip().lower(),
            RelationshipToDecedent.OTHER_RELATIVE,
        )
        return Beneficiary(
            contact=ContactInfo(
                first_name=data.get("first_name", ""),
                last_name=data.get("last_name", ""),
                email=data.get("email", ""),
                phone=data.get("phone", ""),
                address_line1=data.get("address_line1", ""),
                address_line2=data.get("address_line2", ""),
                city=data.get("city", ""),
                state=data.get("state", ""),
                zip_code=data.get("zip_code", ""),
            ),
            relationship=rel,
            share_percentage=float(data.get("share_percentage") or 0),
            is_minor=bool(data.get("is_minor")),
        )


# ── Utilities ───────────────────────────────────────────────────────


def _parse_date(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        # Cognito uses ISO 8601 for dates/datetimes
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
            try:
                return datetime.strptime(value.split(".")[0].rstrip("Z"), fmt).date()
            except ValueError:
                continue
    return None


def _parse_money(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    # Cognito Money fields serialize as strings like "$1,234.56"
    cleaned = str(value).replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return 0.0
