"""Petition & court form generation engine.

Auto-generates the probate petition and supporting documents
from the case data. Outputs filled PDF forms or structured data
ready for e-filing.

Supports:
- Petition for Probate (with will / without will)
- Small Estate Affidavit
- Inventory and Appraisal
- Notice to Creditors
- Notice of Hearing
- Letters Testamentary / Letters of Administration
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from trustate.workflow.models import (
    CaseStage,
    Document,
    DocumentStatus,
    DocumentType,
    ProbateCase,
    ProbateType,
)

logger = logging.getLogger(__name__)


@dataclass
class GeneratedPetition:
    """A generated court document ready for review/filing."""

    document_type: DocumentType
    title: str
    content: dict[str, Any]  # structured field data for PDF filling
    generated_at: datetime
    case_id: str
    template_id: str = ""
    file_path: str = ""


class PetitionGenerator:
    """Generates all required court filings from a ProbateCase."""

    def generate_all(self, case: ProbateCase) -> list[GeneratedPetition]:
        """Generate all court documents needed for this case."""
        logger.info("Generating petitions for case %s (type=%s)", case.id, case.probate_type.value)

        petitions = []

        if case.probate_type == ProbateType.SMALL_ESTATE_AFFIDAVIT:
            petitions.append(self._generate_small_estate_affidavit(case))
        else:
            petitions.append(self._generate_probate_petition(case))
            petitions.append(self._generate_notice_to_creditors(case))

        petitions.append(self._generate_inventory_and_appraisal(case))

        # Add generated documents to the case
        for petition in petitions:
            case.documents.append(
                Document(
                    doc_type=petition.document_type,
                    status=DocumentStatus.VERIFIED,
                    notes=f"Auto-generated: {petition.title}",
                )
            )

        case.stage = CaseStage.PETITION_GENERATED
        logger.info("Case %s: %d documents generated", case.id, len(petitions))
        return petitions

    def _generate_probate_petition(self, case: ProbateCase) -> GeneratedPetition:
        """Generate the main Petition for Probate."""
        decedent = case.decedent
        petitioner = case.petitioner

        has_will = decedent and decedent.had_will
        petition_title = (
            "Petition for Probate of Will and for Letters Testamentary"
            if has_will
            else "Petition for Letters of Administration"
        )

        content = {
            "court_name": case.court.court_name,
            "county": case.court.county or (decedent.county_of_domicile if decedent else ""),
            "state": case.court.state or (decedent.last_state if decedent else ""),
            "case_number": case.court.case_number,
            "petition_type": petition_title,
            "decedent": {
                "full_name": decedent.full_name if decedent else "",
                "date_of_birth": str(decedent.date_of_birth) if decedent and decedent.date_of_birth else "",
                "date_of_death": str(decedent.date_of_death) if decedent and decedent.date_of_death else "",
                "domicile_address": f"{decedent.last_address_line1}, {decedent.last_city}, {decedent.last_state} {decedent.last_zip_code}" if decedent else "",
                "county_of_domicile": decedent.county_of_domicile if decedent else "",
                "marital_status": decedent.marital_status if decedent else "",
                "had_will": has_will,
                "will_date": str(decedent.will_date) if decedent and decedent.will_date else "",
            },
            "petitioner": {
                "full_name": petitioner.contact.full_name if petitioner else "",
                "address": petitioner.contact.full_address if petitioner else "",
                "relationship": petitioner.relationship.value if petitioner else "",
                "phone": petitioner.contact.phone if petitioner else "",
                "email": petitioner.contact.email if petitioner else "",
                "is_nominated_in_will": petitioner.is_nominated_in_will if petitioner else False,
            },
            "beneficiaries": [
                {
                    "full_name": b.contact.full_name,
                    "relationship": b.relationship.value,
                    "address": b.contact.full_address,
                    "share_percentage": b.share_percentage,
                    "is_minor": b.is_minor,
                }
                for b in case.beneficiaries
            ],
            "estate_value": case.total_estate_value,
            "assets_summary": [
                {
                    "type": a.asset_type.value,
                    "description": a.description,
                    "value": a.estimated_value,
                }
                for a in case.assets
            ],
            "bond_amount": petitioner.bond_amount if petitioner else 0,
            "bond_waived": petitioner.bond_waived if petitioner else False,
            "probate_type": case.probate_type.value,
        }

        return GeneratedPetition(
            document_type=DocumentType.PETITION,
            title=petition_title,
            content=content,
            generated_at=datetime.utcnow(),
            case_id=case.id,
        )

    def _generate_small_estate_affidavit(self, case: ProbateCase) -> GeneratedPetition:
        """Generate a Small Estate Affidavit for estates under the threshold."""
        decedent = case.decedent
        petitioner = case.petitioner

        content = {
            "county": decedent.county_of_domicile if decedent else "",
            "state": decedent.last_state if decedent else "",
            "decedent_name": decedent.full_name if decedent else "",
            "date_of_death": str(decedent.date_of_death) if decedent and decedent.date_of_death else "",
            "affiant_name": petitioner.contact.full_name if petitioner else "",
            "affiant_address": petitioner.contact.full_address if petitioner else "",
            "affiant_relationship": petitioner.relationship.value if petitioner else "",
            "total_estate_value": case.total_estate_value,
            "assets": [
                {
                    "description": a.description,
                    "value": a.estimated_value,
                    "institution": a.institution_name,
                }
                for a in case.assets
            ],
            "days_since_death": (
                (datetime.utcnow().date() - decedent.date_of_death).days
                if decedent and decedent.date_of_death
                else 0
            ),
            "no_probate_pending": True,
        }

        return GeneratedPetition(
            document_type=DocumentType.PETITION,
            title="Small Estate Affidavit",
            content=content,
            generated_at=datetime.utcnow(),
            case_id=case.id,
        )

    def _generate_notice_to_creditors(self, case: ProbateCase) -> GeneratedPetition:
        """Generate the Notice to Creditors for publication."""
        decedent = case.decedent
        petitioner = case.petitioner

        content = {
            "court_name": case.court.court_name,
            "case_number": case.court.case_number,
            "county": case.court.county or (decedent.county_of_domicile if decedent else ""),
            "state": case.court.state or (decedent.last_state if decedent else ""),
            "decedent_name": decedent.full_name if decedent else "",
            "personal_representative_name": petitioner.contact.full_name if petitioner else "",
            "personal_representative_address": petitioner.contact.full_address if petitioner else "",
            "attorney_name": case.assigned_attorney_email,
            "claims_deadline_days": 120,  # varies by state
        }

        return GeneratedPetition(
            document_type=DocumentType.NOTICE_TO_CREDITORS,
            title="Notice to Creditors",
            content=content,
            generated_at=datetime.utcnow(),
            case_id=case.id,
        )

    def _generate_inventory_and_appraisal(self, case: ProbateCase) -> GeneratedPetition:
        """Generate the Inventory and Appraisal of estate assets."""
        content = {
            "case_number": case.court.case_number,
            "decedent_name": case.decedent.full_name if case.decedent else "",
            "personal_representative": (
                case.petitioner.contact.full_name if case.petitioner else ""
            ),
            "total_value": case.total_estate_value,
            "assets": [
                {
                    "item_number": i + 1,
                    "type": a.asset_type.value,
                    "description": a.description,
                    "institution": a.institution_name,
                    "estimated_value": a.estimated_value,
                    "appraised_value": a.appraised_value,
                    "appraisal_date": str(a.appraisal_date) if a.appraisal_date else "",
                }
                for i, a in enumerate(case.assets)
            ],
            "date_prepared": datetime.utcnow().strftime("%Y-%m-%d"),
        }

        return GeneratedPetition(
            document_type=DocumentType.INVENTORY_AND_APPRAISAL,
            title="Inventory and Appraisal",
            content=content,
            generated_at=datetime.utcnow(),
            case_id=case.id,
        )
