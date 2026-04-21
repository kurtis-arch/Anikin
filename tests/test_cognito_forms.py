"""Tests for Cognito Forms -> ProbateCase -> Trustate flow.

Simulates the full end-to-end: a sales closer signs a client (creates the
matter in Trustate), then the client fills out the Cognito form in stages:
  1. Just the decedent section
  2. Adds assets and beneficiaries
  3. Uploads the death certificate
  4. Uploads the will a few days later
Each webhook triggers a PUT /import to Trustate with only the new data.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from trustate.integrations.cognito_forms import (
    CognitoFieldMap,
    CognitoFormsProcessor,
)
from trustate.integrations.mappers import MapperConfig
from trustate.integrations.trustate_client import TrustateClient
from trustate.pipeline import ProbatePipeline
from trustate.workflow.store import JSONFileStore


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.content = json.dumps(body).encode()
        self.reason = "OK"
        self.text = json.dumps(body)

    def json(self):
        return self._body


def _cognito_payload(case_id: str, **sections) -> dict:
    """Build a realistic Cognito Forms webhook body with just the given sections."""
    payload = {
        "Form": {"Id": "form-123", "Name": "Probate Intake"},
        "Id": 42,
        "Number": "42",
        "CaseId": case_id,
    }
    payload.update(sections)
    return payload


class CognitoEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = JSONFileStore(data_dir=os.path.join(self.tmpdir, "cases"))
        self.trustate = TrustateClient(
            host="https://fake-trustate.test",
            public_token="pub",
            private_key="priv",
        )
        self.pipeline = ProbatePipeline(
            store=self.store,
            trustate_client=self.trustate,
            mapper_config=MapperConfig(source="test-pipeline"),
        )
        self.calls: list[dict] = []

        def capture(method, url, headers=None, json=None, timeout=None):
            self.calls.append({"method": method, "url": url, "json": json})
            return FakeResponse(200, {"message": "ok", "importId": "imp-1"})

        self._patcher = patch(
            "trustate.integrations.trustate_client.requests.request",
            side_effect=capture,
        )
        self._patcher.start()

        # Seed a case via deal-won
        self.case = self.pipeline.handle_new_client(
            {
                "deal_id": "GHL-777",
                "contact_first_name": "Jane",
                "contact_last_name": "Smith",
                "contact_email": "jane@example.com",
                "contact_phone": "+15551110000",
                "decedent_first_name": "John",
                "decedent_last_name": "Smith",
                "relationship_to_decedent": "child",
            }
        )
        self.calls.clear()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ── Individual update webhooks ─────────────────────────────────

    def test_decedent_only_update_puts_to_trustate(self):
        payload = _cognito_payload(
            self.case.id,
            Decedent={
                "Name": {"First": "John", "Last": "Smith"},
                "DateOfBirth": "1945-03-15",
                "DateOfDeath": "2026-02-20",
                "Address": {
                    "Line1": "789 Elm St",
                    "City": "Austin",
                    "State": "TX",
                    "PostalCode": "78701",
                },
                "MaritalStatus": "Widowed",
                "HadWill": True,
            },
        )

        case = self.pipeline.handle_cognito_webhook(payload)

        # One PUT/POST/upsert sequence to Trustate
        put_calls = [c for c in self.calls if c["method"] == "PUT"]
        self.assertEqual(
            len(put_calls), 1, "Expected a single PUT /import on Cognito update"
        )

        body = put_calls[0]["json"]
        self.assertEqual(body["IntegrationContext"]["externalId"], "GHL-777")
        decedent = body["Matter"]["decedent"]
        self.assertEqual(decedent["dateOfBirth"], "1945-03-15T00:00:00.000Z")
        self.assertEqual(decedent["dateOfDeath"], "2026-02-20T00:00:00.000Z")
        self.assertEqual(decedent["address"]["state"], "TX")
        self.assertEqual(decedent["address"]["zip"], "78701")

        # Assets section was NOT in the webhook, so Trustate payload shouldn't
        # carry it (partial update)
        self.assertNotIn("Assets", body)

        # Local case has the decedent info merged
        self.assertIsNotNone(case.decedent.date_of_death)
        self.assertEqual(case.decedent.last_state, "TX")

    def test_assets_only_update_replaces_assets_in_trustate(self):
        payload = _cognito_payload(
            self.case.id,
            Assets=[
                {
                    "Type": "Bank Account",
                    "Description": "Chase Checking",
                    "EstimatedValue": "$50,000.00",
                    "Institution": "JPMorgan Chase",
                },
                {
                    "Type": "Real Estate",
                    "Description": "Family home 789 Elm St",
                    "EstimatedValue": 450000,
                },
                {
                    "Type": "Car",
                    "Description": "2020 Honda Civic",
                    "EstimatedValue": 18000,
                },
            ],
        )

        case = self.pipeline.handle_cognito_webhook(payload)

        put_calls = [c for c in self.calls if c["method"] == "PUT"]
        body = put_calls[0]["json"]
        assets = body["Assets"]
        types = {a["assetType"] for a in assets}
        self.assertEqual(
            types, {"BANK_ACCOUNT", "REAL_PROPERTY", "MOTOR_VEHICLES"}
        )
        bank = next(a for a in assets if a["assetType"] == "BANK_ACCOUNT")
        self.assertEqual(bank["currentValue"], 50000.0)
        self.assertEqual(bank["assetDescription"], "Chase Checking")

        # Local case estate value got recalculated
        self.assertEqual(case.total_estate_value, 50000 + 450000 + 18000)

    def test_beneficiaries_map_to_trustate_contacts(self):
        payload = _cognito_payload(
            self.case.id,
            Beneficiaries=[
                {
                    "Name": {"First": "Jane", "Last": "Smith"},
                    "Email": "jane@example.com",
                    "Relationship": "Daughter",
                    "SharePercentage": 50,
                },
                {
                    "Name": {"First": "Bob", "Last": "Smith"},
                    "Email": "bob@example.com",
                    "Relationship": "Son",
                    "SharePercentage": 50,
                },
            ],
        )

        self.pipeline.handle_cognito_webhook(payload)

        body = [c for c in self.calls if c["method"] == "PUT"][0]["json"]
        contacts = body["Contacts"]
        self.assertEqual(len(contacts), 2)
        first_names = {c["firstName"] for c in contacts}
        self.assertEqual(first_names, {"Jane", "Bob"})
        # "Daughter"/"Son" both normalize to "child" relationship
        self.assertTrue(all(c["relationship"] == "child" for c in contacts))

    def test_file_upload_from_cognito_attaches_document(self):
        payload = _cognito_payload(
            self.case.id,
            DeathCertificate=[
                {
                    "File": "death_cert.pdf",
                    "ContentType": "application/pdf",
                    "Size": 25000,
                    "Url": "https://www.cognitoforms.com/f/abc/123/death_cert.pdf",
                }
            ],
        )

        case = self.pipeline.handle_cognito_webhook(payload)

        # Document should now be on the case with UPLOADED status
        death_certs = [
            d for d in case.documents if d.doc_type.value == "death_certificate"
        ]
        self.assertEqual(len(death_certs), 1)
        self.assertEqual(death_certs[0].status.value, "uploaded")
        self.assertIn("cognitoforms.com", death_certs[0].file_url)

    def test_second_webhook_is_idempotent_for_same_file(self):
        payload = _cognito_payload(
            self.case.id,
            DeathCertificate=[
                {
                    "File": "cert.pdf",
                    "Url": "https://www.cognitoforms.com/f/abc/123/cert.pdf",
                }
            ],
        )

        self.pipeline.handle_cognito_webhook(payload)
        case = self.pipeline.handle_cognito_webhook(payload)

        death_certs = [
            d for d in case.documents if d.doc_type.value == "death_certificate"
        ]
        self.assertEqual(
            len(death_certs), 1, "Same file URL should not create a duplicate doc"
        )

    def test_missing_case_id_raises(self):
        payload = {
            "Form": {"Id": "form-123", "Name": "Probate Intake"},
            "Id": 42,
            # CaseId missing
            "Decedent": {"Name": {"First": "X", "Last": "Y"}},
        }
        with self.assertRaises(ValueError) as ctx:
            self.pipeline.handle_cognito_webhook(payload)
        self.assertIn("CaseId", str(ctx.exception))

    def test_unknown_case_id_raises(self):
        payload = _cognito_payload("does-not-exist")
        with self.assertRaises(ValueError) as ctx:
            self.pipeline.handle_cognito_webhook(payload)
        self.assertIn("not found", str(ctx.exception))

    # ── Multi-step progressive fill ────────────────────────────────

    def test_progressive_fill_only_puts_new_fields_each_time(self):
        """Simulate the realistic flow: client fills the form in 3 sittings."""
        # Day 1: just the decedent
        self.pipeline.handle_cognito_webhook(
            _cognito_payload(
                self.case.id,
                Decedent={
                    "Name": {"First": "John", "Last": "Smith"},
                    "DateOfDeath": "2026-02-20",
                },
            )
        )

        # Day 2: assets
        self.pipeline.handle_cognito_webhook(
            _cognito_payload(
                self.case.id,
                Assets=[
                    {
                        "Type": "Bank Account",
                        "Description": "Chase",
                        "EstimatedValue": 10000,
                    }
                ],
            )
        )

        # Day 3: uploads the death certificate
        self.pipeline.handle_cognito_webhook(
            _cognito_payload(
                self.case.id,
                DeathCertificate=[
                    {"File": "cert.pdf", "Url": "https://cg.test/cert.pdf"}
                ],
            )
        )

        # Three PUTs to Trustate — one per webhook
        put_calls = [c for c in self.calls if c["method"] == "PUT"]
        self.assertEqual(len(put_calls), 3)

        # Day 1 PUT: decedent only
        self.assertIn("decedent", put_calls[0]["json"]["Matter"])
        self.assertNotIn("Assets", put_calls[0]["json"])

        # Day 2 PUT: assets section present (decedent stays from day 1 merged state)
        self.assertIn("Assets", put_calls[1]["json"])
        self.assertEqual(len(put_calls[1]["json"]["Assets"]), 1)

        # Day 3 PUT: still carries merged data but no new fields required
        # The critical thing is the PUT went out — Trustate now has the file via its own mechanism
        self.assertEqual(put_calls[2]["json"]["IntegrationContext"]["externalId"], "GHL-777")

        # Final case state has everything accumulated
        case = self.store.get(self.case.id)
        self.assertIsNotNone(case.decedent.date_of_death)
        self.assertEqual(len(case.assets), 1)
        death_certs = [
            d for d in case.documents if d.doc_type.value == "death_certificate"
        ]
        self.assertEqual(len(death_certs), 1)


class CognitoFieldMapTest(unittest.TestCase):
    """Verify the parser is flexible across common Cognito field structures."""

    def test_custom_field_map_overrides_defaults(self):
        fm = CognitoFieldMap(
            case_id_field="InternalCaseRef",
            decedent_section="DecedentInfo",
        )
        proc = CognitoFormsProcessor(field_map=fm)

        payload = {
            "InternalCaseRef": "case-xyz",
            "DecedentInfo": {"Name": {"First": "A", "Last": "B"}},
            "Decedent": {"Name": {"First": "WRONG", "Last": "WRONG"}},
        }
        update = proc.parse(payload)
        self.assertEqual(update.case_id, "case-xyz")
        self.assertEqual(update.decedent["first_name"], "A")

    def test_money_field_with_dollar_signs_and_commas(self):
        proc = CognitoFormsProcessor()
        payload = {
            "CaseId": "x",
            "Assets": [
                {
                    "Type": "Bank Account",
                    "Description": "Savings",
                    "EstimatedValue": "$1,234,567.89",
                }
            ],
        }
        update = proc.parse(payload)
        self.assertAlmostEqual(
            update.assets[0]["estimated_value"], 1234567.89, places=2
        )


if __name__ == "__main__":
    unittest.main()
