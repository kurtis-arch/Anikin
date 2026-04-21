"""Client intake automation.

Triggered when a sales closer signs a new client. Handles:
1. Receiving the webhook/event from CRM (e.g., GoHighLevel, HubSpot, Salesforce)
2. Creating the ProbateCase record
3. Sending the intake form to the client
4. Processing completed intake forms
5. Auto-advancing the case when intake is complete
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Optional

from trustate.workflow.models import (
    Asset,
    AssetType,
    Beneficiary,
    CaseStage,
    ContactInfo,
    Decedent,
    Document,
    DocumentType,
    PetitionerInfo,
    ProbateCase,
    ProbateType,
    RelationshipToDecedent,
)

logger = logging.getLogger(__name__)


class IntakeProcessor:
    """Processes client intake data and builds the ProbateCase."""

    def create_case_from_crm_webhook(self, payload: dict[str, Any]) -> ProbateCase:
        """Create a new ProbateCase from a CRM deal-closed webhook.

        Expected payload fields (adapt to your CRM):
            - deal_id: CRM deal identifier
            - closer_id: sales closer who signed the client
            - contact_first_name, contact_last_name
            - contact_email, contact_phone
            - contact_address, contact_city, contact_state, contact_zip
            - decedent_first_name, decedent_last_name
            - law_firm_id: partner firm assigned
            - aircall_call_id: (optional) linked call recording
        """
        logger.info("Creating case from CRM webhook, deal_id=%s", payload.get("deal_id"))

        petitioner_contact = ContactInfo(
            first_name=payload.get("contact_first_name", ""),
            last_name=payload.get("contact_last_name", ""),
            email=payload.get("contact_email", ""),
            phone=payload.get("contact_phone", ""),
            address_line1=payload.get("contact_address", ""),
            city=payload.get("contact_city", ""),
            state=payload.get("contact_state", ""),
            zip_code=payload.get("contact_zip", ""),
            county=payload.get("contact_county", ""),
        )

        decedent = Decedent(
            first_name=payload.get("decedent_first_name", ""),
            last_name=payload.get("decedent_last_name", ""),
        )

        relationship_str = payload.get("relationship_to_decedent", "child")
        try:
            relationship = RelationshipToDecedent(relationship_str)
        except ValueError:
            relationship = RelationshipToDecedent.OTHER_RELATIVE

        petitioner = PetitionerInfo(
            contact=petitioner_contact,
            relationship=relationship,
        )

        case = ProbateCase(
            stage=CaseStage.CLIENT_SIGNED,
            decedent=decedent,
            petitioner=petitioner,
            crm_deal_id=payload.get("deal_id", ""),
            sales_closer_id=payload.get("closer_id", ""),
            law_firm_id=payload.get("law_firm_id", ""),
            aircall_call_id=payload.get("aircall_call_id", ""),
        )

        logger.info("Created case %s for %s", case.id, petitioner_contact.full_name)
        return case

    def process_intake_form(
        self, case: ProbateCase, form_data: dict[str, Any]
    ) -> ProbateCase:
        """Process a completed intake form submitted by the client.

        This populates all the detailed information needed to draft the petition:
        decedent details, assets, beneficiaries, etc.
        """
        logger.info("Processing intake form for case %s", case.id)

        # Update decedent details
        if case.decedent and form_data.get("decedent"):
            d = form_data["decedent"]
            case.decedent.date_of_birth = _parse_date(d.get("date_of_birth"))
            case.decedent.date_of_death = _parse_date(d.get("date_of_death"))
            case.decedent.ssn_last_four = d.get("ssn_last_four", "")
            case.decedent.last_address_line1 = d.get("address", "")
            case.decedent.last_city = d.get("city", "")
            case.decedent.last_state = d.get("state", "")
            case.decedent.last_zip_code = d.get("zip_code", "")
            case.decedent.last_county = d.get("county", "")
            case.decedent.had_will = d.get("had_will")
            case.decedent.will_date = _parse_date(d.get("will_date"))
            case.decedent.marital_status = d.get("marital_status", "")

        # Process assets
        for asset_data in form_data.get("assets", []):
            try:
                asset_type = AssetType(asset_data.get("type", "other"))
            except ValueError:
                asset_type = AssetType.OTHER
            case.assets.append(
                Asset(
                    asset_type=asset_type,
                    description=asset_data.get("description", ""),
                    estimated_value=float(asset_data.get("estimated_value", 0)),
                    account_number_last_four=asset_data.get("account_last_four", ""),
                    institution_name=asset_data.get("institution", ""),
                    address=asset_data.get("address", ""),
                )
            )

        # Process beneficiaries
        for ben_data in form_data.get("beneficiaries", []):
            try:
                rel = RelationshipToDecedent(ben_data.get("relationship", "other_relative"))
            except ValueError:
                rel = RelationshipToDecedent.OTHER_RELATIVE
            case.beneficiaries.append(
                Beneficiary(
                    contact=ContactInfo(
                        first_name=ben_data.get("first_name", ""),
                        last_name=ben_data.get("last_name", ""),
                        email=ben_data.get("email", ""),
                        phone=ben_data.get("phone", ""),
                        address_line1=ben_data.get("address", ""),
                        city=ben_data.get("city", ""),
                        state=ben_data.get("state", ""),
                        zip_code=ben_data.get("zip_code", ""),
                    ),
                    relationship=rel,
                    share_percentage=float(ben_data.get("share_percentage", 0)),
                    is_minor=ben_data.get("is_minor", False),
                )
            )

        # Determine probate type based on estate value and state rules
        case.estimated_estate_value = case.total_estate_value
        case.probate_type = self._determine_probate_type(case)

        case.stage = CaseStage.INTAKE_COMPLETE
        logger.info(
            "Intake complete for case %s — estate value $%.2f, type=%s",
            case.id,
            case.estimated_estate_value,
            case.probate_type.value,
        )
        return case

    def _determine_probate_type(self, case: ProbateCase) -> ProbateType:
        """Auto-determine the most efficient probate type based on the estate."""
        value = case.total_estate_value
        state = ""
        if case.decedent:
            state = case.decedent.last_state.upper()

        # Small estate thresholds vary by state — these are common defaults
        small_estate_limits = {
            "CA": 184500,
            "TX": 75000,
            "FL": 75000,
            "NY": 50000,
            "AZ": 100000,
            "NV": 100000,
        }
        small_limit = small_estate_limits.get(state, 50000)

        if value <= small_limit:
            return ProbateType.SMALL_ESTATE_AFFIDAVIT

        has_will = case.decedent and case.decedent.had_will
        if has_will:
            return ProbateType.FORMAL

        return ProbateType.FORMAL

    def get_intake_form_url(
        self, case: ProbateCase, base_url: str = ""
    ) -> str:
        """Generate the intake form URL to send to the client.

        Uses the Cognito Forms URL configured via INTAKE_FORM_BASE_URL env
        var (e.g. https://www.cognitoforms.com/YourCompany/ProbateIntake).
        The case_id is passed as a query param so Cognito's hidden CaseId
        field auto-populates and every webhook back to us carries it.
        """
        if not base_url:
            import os

            base_url = os.getenv(
                "INTAKE_FORM_BASE_URL",
                "https://www.cognitoforms.com/YourCompany/ProbateIntake",
            )
        sep = "&" if "?" in base_url else "?"
        return f"{base_url}{sep}case_id={case.id}&crm={case.crm_deal_id}"


def _parse_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
    return None
