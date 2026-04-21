"""Mappers between our internal ProbateCase model and Trustate's Import payload.

The Trustate PUT/POST /import endpoint expects a specific JSON structure
documented in the Trustate API spec. This module is the single source of
truth for that translation — update enum maps and field mappings here
if the Trustate schema changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional

from trustate.workflow.models import (
    Asset,
    AssetType,
    Beneficiary,
    ContactInfo,
    Decedent,
    PetitionerInfo,
    ProbateCase,
    RelationshipToDecedent,
)

# ── Enum maps: our internal values -> Trustate enum values ─────────

ASSET_TYPE_MAP: dict[AssetType, str] = {
    AssetType.REAL_PROPERTY: "REAL_PROPERTY",
    AssetType.BANK_ACCOUNT: "BANK_ACCOUNT",
    AssetType.INVESTMENT_ACCOUNT: "INVESTMENT_ACCOUNT",
    AssetType.RETIREMENT_ACCOUNT: "RETIREMENT_ACCOUNT",
    AssetType.LIFE_INSURANCE: "LIFE_INSURANCE",
    AssetType.VEHICLE: "MOTOR_VEHICLES",
    AssetType.PERSONAL_PROPERTY: "TANGIBLE_PERSONAL_PROPERTY",
    AssetType.BUSINESS_INTEREST: "BUSINESSES",
    AssetType.DIGITAL_ASSET: "OTHER",
    AssetType.OTHER: "OTHER",
}

RELATIONSHIP_LABEL_MAP: dict[RelationshipToDecedent, str] = {
    RelationshipToDecedent.SPOUSE: "spouse",
    RelationshipToDecedent.CHILD: "child",
    RelationshipToDecedent.PARENT: "parent",
    RelationshipToDecedent.SIBLING: "sibling",
    RelationshipToDecedent.GRANDCHILD: "grandchild",
    RelationshipToDecedent.OTHER_RELATIVE: "other relative",
    RelationshipToDecedent.NON_RELATIVE: "non-relative",
}


@dataclass
class MapperConfig:
    """Configuration for case -> Trustate payload mapping."""

    # The `source` value used in IntegrationContext to identify this pipeline
    source: str = "trustate-automation-pipeline"
    # Default matterType when we can't infer one
    default_matter_type: str = "ESTATE_ADMINISTRATION"


# ── Main entry point ───────────────────────────────────────────────


def case_to_trustate_payload(
    case: ProbateCase,
    config: Optional[MapperConfig] = None,
    *,
    include_empty_sections: bool = False,
) -> dict[str, Any]:
    """Convert a ProbateCase into a Trustate Import API payload.

    Args:
        case: The internal ProbateCase to export.
        config: Mapper configuration (source name, defaults).
        include_empty_sections: If False (default), omit empty Contacts/Assets/
            Liabilities arrays from the payload — useful for PUT (partial update).
            Set True when you want to explicitly clear a section on update.
    """
    cfg = config or MapperConfig()

    payload: dict[str, Any] = {
        "IntegrationContext": {
            "externalId": case.crm_deal_id or case.id,
            "source": cfg.source,
        },
        "Matter": _build_matter(case, cfg),
    }

    contacts = _build_contacts(case)
    if contacts or include_empty_sections:
        payload["Contacts"] = contacts

    assets = _build_assets(case)
    if assets or include_empty_sections:
        payload["Assets"] = assets

    data_fields = _build_data_fields(case)
    if data_fields:
        payload["DataFields"] = data_fields

    return payload


# ── Section builders ───────────────────────────────────────────────


def _build_matter(case: ProbateCase, cfg: MapperConfig) -> dict[str, Any]:
    """Build the Matter section of the payload.

    displayFirstName/displayLastName are required by the API — we fall back
    to the decedent's name if the petitioner is missing (matter is titled
    after the decedent in probate).
    """
    petitioner = case.petitioner
    decedent = case.decedent

    display_first = ""
    display_last = ""
    if petitioner and petitioner.contact:
        display_first = petitioner.contact.first_name
        display_last = petitioner.contact.last_name
    if (not display_first or not display_last) and decedent:
        display_first = display_first or decedent.first_name
        display_last = display_last or decedent.last_name

    matter: dict[str, Any] = {
        "displayFirstName": display_first,
        "displayLastName": display_last,
        "matterType": cfg.default_matter_type,
    }

    if decedent:
        decedent_obj = _build_decedent(decedent)
        if decedent_obj:
            matter["decedent"] = decedent_obj

    if petitioner:
        client_obj = _build_client(petitioner)
        if client_obj:
            matter["client"] = client_obj

    return matter


def _build_decedent(d: Decedent) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    if d.first_name:
        obj["firstName"] = d.first_name
    if d.last_name:
        obj["lastName"] = d.last_name
    if d.date_of_birth:
        obj["dateOfBirth"] = _to_iso(d.date_of_birth)
    if d.date_of_death:
        obj["dateOfDeath"] = _to_iso(d.date_of_death)
    ssn = _format_ssn(d.ssn_last_four)
    if ssn:
        obj["ssn"] = ssn
    addr = _build_address(
        street=_join_street(d.last_address_line1, d.last_address_line2),
        city=d.last_city,
        state=d.last_state,
        zip_code=d.last_zip_code,
    )
    if addr:
        obj["address"] = addr
    return obj


def _build_client(p: PetitionerInfo) -> dict[str, Any]:
    c = p.contact
    obj: dict[str, Any] = {}
    if c.first_name:
        obj["firstName"] = c.first_name
    if c.last_name:
        obj["lastName"] = c.last_name
    if c.email:
        obj["emailAddress"] = c.email
    if c.phone:
        obj["phoneNumber"] = c.phone
    addr = _build_address(
        street=_join_street(c.address_line1, c.address_line2),
        city=c.city,
        state=c.state,
        zip_code=c.zip_code,
    )
    if addr:
        obj["address"] = addr
    return obj


def _build_contacts(case: ProbateCase) -> list[dict[str, Any]]:
    """Map beneficiaries (and the petitioner, if not already the client) to Contacts."""
    contacts: list[dict[str, Any]] = []

    for b in case.beneficiaries:
        c = b.contact
        if not c.first_name or not c.last_name:
            continue  # API requires both
        entry: dict[str, Any] = {
            "firstName": c.first_name,
            "lastName": c.last_name,
            "relationship": RELATIONSHIP_LABEL_MAP.get(
                b.relationship, b.relationship.value
            ),
        }
        if c.email:
            entry["email"] = c.email
        if c.phone:
            entry["phoneNumber"] = c.phone
        addr = _build_address(
            street=_join_street(c.address_line1, c.address_line2),
            city=c.city,
            state=c.state,
            zip_code=c.zip_code,
        )
        if addr:
            entry["address"] = addr
        contacts.append(entry)

    return contacts


def _build_assets(case: ProbateCase) -> list[dict[str, Any]]:
    assets: list[dict[str, Any]] = []
    for a in case.assets:
        if not a.description:
            continue  # API requires assetDescription (min length 1)
        entry: dict[str, Any] = {
            "assetType": ASSET_TYPE_MAP.get(a.asset_type, "OTHER"),
            "assetDescription": a.description,
        }
        if a.estimated_value:
            entry["currentValue"] = float(a.estimated_value)
            entry["currentValueDate"] = _to_iso(datetime.utcnow())
        if a.appraised_value is not None:
            entry["initialValue"] = float(a.appraised_value)
            if a.appraisal_date:
                entry["initialValueDate"] = _to_iso(a.appraisal_date)
        assets.append(entry)
    return assets


def _build_data_fields(case: ProbateCase) -> dict[str, Any]:
    """Pack pipeline metadata into Trustate's flexible DataFields map."""
    fields: dict[str, Any] = {
        "pipelineCaseId": case.id,
        "pipelineStage": case.stage.value,
        "probateType": case.probate_type.value,
    }
    if case.law_firm_id:
        fields["lawFirmId"] = case.law_firm_id
    if case.sales_closer_id:
        fields["salesCloserId"] = case.sales_closer_id
    if case.crm_deal_id:
        fields["crmDealId"] = case.crm_deal_id
    if case.aircall_call_id:
        fields["aircallCallId"] = case.aircall_call_id
    if case.estimated_estate_value:
        fields["estimatedEstateValue"] = float(case.estimated_estate_value)
    return fields


# ── Shared helpers ─────────────────────────────────────────────────


def _build_address(
    street: str, city: str, state: str, zip_code: str
) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    if street:
        obj["street"] = street
    if city:
        obj["city"] = city
    if state:
        obj["state"] = state.upper()[:2]  # API requires 2-letter uppercase
    if zip_code:
        obj["zip"] = zip_code
    return obj


def _join_street(line1: str, line2: str) -> str:
    parts = [p for p in (line1, line2) if p]
    return ", ".join(parts)


def _to_iso(value) -> str:
    """Convert a date/datetime/string into Trustate's ISO datetime format."""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%dT00:00:00.000Z")
    if isinstance(value, str):
        return value
    return ""


def _format_ssn(last_four: str) -> str:
    """Trustate expects XXX-XX-XXXX format. We only store last 4 internally.

    Return empty string rather than a fake SSN — let Trustate treat it as missing.
    """
    return ""
