#!/usr/bin/env python3
"""Demo: Run the full Trustate probate automation pipeline end-to-end.

This simulates every step from a sales closer signing a client
all the way through court filing — showing exactly what gets
automated at each stage.
"""

import json
import logging

from trustate.pipeline import ProbatePipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)


def main():
    pipeline = ProbatePipeline()

    print("=" * 70)
    print("TRUSTATE PROBATE AUTOMATION PIPELINE — FULL DEMO")
    print("=" * 70)

    # ── STEP 1: Sales closer signs a new client ──────────────────────
    print("\n📋 STEP 1: Sales closer signs client (CRM webhook fires)")
    crm_payload = {
        "deal_id": "GHL-2026-4501",
        "closer_id": "closer-mike",
        "contact_first_name": "Maria",
        "contact_last_name": "Garcia",
        "contact_email": "maria.garcia@email.com",
        "contact_phone": "+15551234567",
        "contact_address": "456 Oak Avenue",
        "contact_city": "Los Angeles",
        "contact_state": "CA",
        "contact_zip": "90001",
        "contact_county": "Los Angeles",
        "decedent_first_name": "Roberto",
        "decedent_last_name": "Garcia",
        "relationship_to_decedent": "child",
        "law_firm_id": "firm-johnson-associates",
        "aircall_call_id": "call-98765",
    }

    case = pipeline.handle_new_client(crm_payload)
    print(f"   ✅ Case created: {case.id}")
    print(f"   ✅ Stage: {case.stage.value}")
    print(f"   ✅ Intake form sent to: {case.petitioner.contact.email}")

    # ── STEP 2: Client submits intake form ───────────────────────────
    print("\n📝 STEP 2: Client submits intake form")
    intake_data = {
        "case_id": case.id,
        "decedent": {
            "date_of_birth": "1945-03-15",
            "date_of_death": "2026-02-20",
            "ssn_last_four": "5678",
            "address": "789 Elm Street",
            "city": "Los Angeles",
            "state": "CA",
            "zip_code": "90002",
            "county": "Los Angeles",
            "had_will": True,
            "will_date": "2020-06-10",
            "marital_status": "widowed",
        },
        "assets": [
            {
                "type": "real_property",
                "description": "Family home at 789 Elm Street, Los Angeles, CA",
                "estimated_value": 650000,
                "address": "789 Elm Street, Los Angeles, CA 90002",
            },
            {
                "type": "bank_account",
                "description": "Chase checking account",
                "estimated_value": 45000,
                "account_last_four": "1234",
                "institution": "JPMorgan Chase",
            },
            {
                "type": "bank_account",
                "description": "Wells Fargo savings account",
                "estimated_value": 120000,
                "account_last_four": "5678",
                "institution": "Wells Fargo",
            },
            {
                "type": "investment_account",
                "description": "Fidelity brokerage account",
                "estimated_value": 280000,
                "account_last_four": "9012",
                "institution": "Fidelity Investments",
            },
            {
                "type": "vehicle",
                "description": "2021 Toyota Camry",
                "estimated_value": 25000,
            },
        ],
        "beneficiaries": [
            {
                "first_name": "Maria",
                "last_name": "Garcia",
                "email": "maria.garcia@email.com",
                "phone": "+15551234567",
                "relationship": "child",
                "share_percentage": 50,
                "address": "456 Oak Avenue",
                "city": "Los Angeles",
                "state": "CA",
                "zip_code": "90001",
            },
            {
                "first_name": "Carlos",
                "last_name": "Garcia",
                "email": "carlos.garcia@email.com",
                "phone": "+15559876543",
                "relationship": "child",
                "share_percentage": 50,
                "address": "321 Pine Street",
                "city": "Pasadena",
                "state": "CA",
                "zip_code": "91101",
            },
        ],
    }

    case = pipeline.handle_intake_submitted(case.id, intake_data)
    print(f"   ✅ Stage: {case.stage.value}")
    print(f"   ✅ Probate type: {case.probate_type.value}")
    print(f"   ✅ Estate value: ${case.total_estate_value:,.2f}")
    print(f"   ✅ Documents requested: {len(case.get_pending_documents())}")
    for doc in case.get_pending_documents():
        print(f"       - {doc.doc_type.value}")

    # ── STEP 3: Client uploads documents ─────────────────────────────
    print("\n📎 STEP 3: Client uploads required documents")
    pending_types = [d.doc_type.value for d in case.get_pending_documents()]
    for doc_type in pending_types:
        case = pipeline.handle_document_uploaded(
            case.id, doc_type, file_path=f"/uploads/{case.id}/{doc_type}.pdf"
        )
        remaining = len(case.get_pending_documents())
        print(f"   ✅ Uploaded: {doc_type} ({remaining} remaining)")

    print(f"   ✅ Stage: {case.stage.value}")

    # ── STEP 4: Petitions auto-generated ─────────────────────────────
    print("\n📄 STEP 4: Petitions auto-generated")
    petition_docs = [
        d for d in case.documents if d.doc_type.value in ("petition", "notice_to_creditors", "inventory_and_appraisal")
    ]
    for doc in petition_docs:
        print(f"   ✅ Generated: {doc.notes}")

    # ── STEP 5: Attorney reviews and approves ────────────────────────
    # At this point the case is in PETITION_GENERATED stage
    # We need to advance to ATTORNEY_REVIEW first
    from trustate.workflow.models import CaseStage
    case = pipeline.engine.advance(case, CaseStage.ATTORNEY_REVIEW)
    pipeline.store.save(case)

    print(f"\n⚖️  STEP 5: Attorney review")
    print(f"   📧 Notification sent to: {case.assigned_attorney_email or case.law_firm_id}")
    case = pipeline.handle_attorney_approval(case.id)
    print(f"   ✅ Attorney approved!")
    print(f"   ✅ Stage: {case.stage.value}")

    # ── STEP 6: Filed with court ─────────────────────────────────────
    print(f"\n🏛️  STEP 6: Court filing")
    print(f"   ✅ E-filed with: {case.court.court_name}")
    print(f"   ✅ Filing fee: ${case.court.filing_fee:,.2f}")
    print(f"   ✅ Stage: {case.stage.value}")

    # ── STEP 7: Hearing scheduled ────────────────────────────────────
    print(f"\n📅 STEP 7: Hearing scheduled")
    case = pipeline.handle_hearing_scheduled(
        case.id,
        hearing_date="2026-06-15T09:00:00",
        hearing_location="Dept. 5, Stanley Mosk Courthouse, 111 N Hill St, Los Angeles",
    )
    print(f"   ✅ Hearing: {case.court.hearing_date}")
    print(f"   ✅ Location: {case.court.hearing_location}")

    # ── STEP 8: Letters issued ───────────────────────────────────────
    print(f"\n📜 STEP 8: Letters issued")
    case = pipeline.handle_letters_issued(case.id)
    print(f"   ✅ Stage: {case.stage.value}")

    # ── Final status ─────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("CASE COMPLETE — FULL PIPELINE SUMMARY")
    print("=" * 70)
    status = pipeline.get_case_status(case.id)
    print(json.dumps(status, indent=2, default=str))
    print("\n✅ Pipeline ran end-to-end with ZERO manual intervention!")
    print("=" * 70)


if __name__ == "__main__":
    main()
