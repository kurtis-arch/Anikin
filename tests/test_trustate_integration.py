"""End-to-end test of the Trustate Import integration.

Mocks the Trustate HTTP endpoint and verifies:
  1. A new deal-won CRM webhook calls POST /import with the right payload.
  2. A completed intake form calls PUT /import (upsert) with decedent + assets + contacts.
  3. importId returned by Trustate is stored on the case.
  4. Payloads match the schema from the Trustate API docs (auth headers, required fields, enum values).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from trustate.integrations.mappers import MapperConfig, case_to_trustate_payload
from trustate.integrations.trustate_client import (
    TrustateAPIError,
    TrustateClient,
)
from trustate.pipeline import ProbatePipeline
from trustate.workflow.store import JSONFileStore


class FakeResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body
        self.content = json.dumps(body).encode()
        self.reason = "OK"
        self.text = json.dumps(body)

    def json(self):
        return self._body


class TrustateIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = JSONFileStore(data_dir=os.path.join(self.tmpdir, "cases"))
        self.trustate = TrustateClient(
            host="https://fake-trustate.test",
            public_token="test-public",
            private_key="test-private",
        )
        self.pipeline = ProbatePipeline(
            store=self.store,
            trustate_client=self.trustate,
            mapper_config=MapperConfig(source="test-pipeline"),
        )
        self.requests_calls: list[dict] = []

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _capture_request(self, status_code: int, import_id: str):
        """Build a requests.request side_effect that captures calls."""

        def side_effect(method, url, headers=None, json=None, timeout=None):
            self.requests_calls.append(
                {
                    "method": method,
                    "url": url,
                    "headers": dict(headers or {}),
                    "json": json,
                }
            )
            return FakeResponse(
                status_code,
                {"message": "ok", "importId": import_id},
            )

        return side_effect

    def test_deal_won_creates_matter_in_trustate(self):
        crm_payload = {
            "deal_id": "GHL-2026-999",
            "closer_id": "closer-alice",
            "contact_first_name": "Jane",
            "contact_last_name": "Smith",
            "contact_email": "jane@example.com",
            "contact_phone": "+15551112222",
            "contact_address": "123 Main St",
            "contact_city": "Austin",
            "contact_state": "TX",
            "contact_zip": "78701",
            "decedent_first_name": "John",
            "decedent_last_name": "Smith",
            "relationship_to_decedent": "child",
            "law_firm_id": "firm-001",
        }

        with patch(
            "trustate.integrations.trustate_client.requests.request",
            side_effect=self._capture_request(200, "import-abc-123"),
        ):
            case = self.pipeline.handle_new_client(crm_payload)

        # Expect exactly one Trustate call for case creation
        create_calls = [c for c in self.requests_calls if c["method"] == "POST"]
        self.assertEqual(len(create_calls), 1, "Should POST to /import once")

        call = create_calls[0]
        self.assertEqual(call["url"], "https://fake-trustate.test/import")
        self.assertEqual(call["headers"]["X-PUBLIC-TOKEN"], "test-public")
        self.assertEqual(call["headers"]["X-PRIVATE-KEY"], "test-private")
        self.assertEqual(call["headers"]["Content-Type"], "application/json")

        payload = call["json"]
        self.assertEqual(payload["IntegrationContext"]["externalId"], "GHL-2026-999")
        self.assertEqual(payload["IntegrationContext"]["source"], "test-pipeline")
        self.assertEqual(payload["Matter"]["displayFirstName"], "Jane")
        self.assertEqual(payload["Matter"]["displayLastName"], "Smith")
        self.assertEqual(payload["Matter"]["matterType"], "ESTATE_ADMINISTRATION")

        # Decedent name is present even before full intake
        self.assertEqual(payload["Matter"]["decedent"]["firstName"], "John")
        self.assertEqual(payload["Matter"]["decedent"]["lastName"], "Smith")

        # Client (petitioner) is populated from CRM
        self.assertEqual(payload["Matter"]["client"]["firstName"], "Jane")
        self.assertEqual(payload["Matter"]["client"]["emailAddress"], "jane@example.com")
        self.assertEqual(payload["Matter"]["client"]["address"]["state"], "TX")

        # importId is persisted on the case
        self.assertEqual(case.trustate_import_id, "import-abc-123")

    def test_intake_submission_updates_matter_with_assets_and_contacts(self):
        # First: create the case via deal-won
        crm_payload = {
            "deal_id": "GHL-2026-999",
            "contact_first_name": "Jane",
            "contact_last_name": "Smith",
            "contact_email": "jane@example.com",
            "contact_phone": "+15551112222",
            "decedent_first_name": "John",
            "decedent_last_name": "Smith",
            "relationship_to_decedent": "child",
        }
        with patch(
            "trustate.integrations.trustate_client.requests.request",
            side_effect=self._capture_request(200, "import-abc-123"),
        ):
            case = self.pipeline.handle_new_client(crm_payload)

        self.requests_calls.clear()

        # Then: submit intake with full data
        intake_data = {
            "case_id": case.id,
            "decedent": {
                "date_of_birth": "1945-03-15",
                "date_of_death": "2026-02-20",
                "address": "789 Elm St",
                "city": "Austin",
                "state": "TX",
                "zip_code": "78702",
                "county": "Travis",
                "had_will": True,
                "marital_status": "widowed",
            },
            "assets": [
                {
                    "type": "bank_account",
                    "description": "Chase Checking",
                    "estimated_value": 50000,
                    "institution": "JPMorgan Chase",
                },
                {
                    "type": "real_property",
                    "description": "789 Elm St primary residence",
                    "estimated_value": 450000,
                },
                {
                    "type": "vehicle",
                    "description": "2020 Honda Civic",
                    "estimated_value": 18000,
                },
            ],
            "beneficiaries": [
                {
                    "first_name": "Jane",
                    "last_name": "Smith",
                    "email": "jane@example.com",
                    "relationship": "child",
                    "share_percentage": 50,
                },
                {
                    "first_name": "Bob",
                    "last_name": "Smith",
                    "email": "bob@example.com",
                    "relationship": "child",
                    "share_percentage": 50,
                },
            ],
        }

        with patch(
            "trustate.integrations.trustate_client.requests.request",
            side_effect=self._capture_request(200, "import-abc-123"),
        ):
            case = self.pipeline.handle_intake_submitted(case.id, intake_data)

        # Intake should trigger upsert_matter -> tries PUT first
        put_calls = [c for c in self.requests_calls if c["method"] == "PUT"]
        self.assertGreaterEqual(len(put_calls), 1, "Should PUT /import on intake")

        payload = put_calls[0]["json"]
        self.assertEqual(payload["Matter"]["displayFirstName"], "Jane")

        # Decedent details from intake should be in the payload
        decedent = payload["Matter"]["decedent"]
        self.assertEqual(decedent["dateOfBirth"], "1945-03-15T00:00:00.000Z")
        self.assertEqual(decedent["dateOfDeath"], "2026-02-20T00:00:00.000Z")
        self.assertEqual(decedent["address"]["state"], "TX")
        self.assertEqual(decedent["address"]["zip"], "78702")

        # Assets mapped correctly
        assets = payload["Assets"]
        self.assertEqual(len(assets), 3)
        asset_types = {a["assetType"] for a in assets}
        self.assertEqual(
            asset_types,
            {"BANK_ACCOUNT", "REAL_PROPERTY", "MOTOR_VEHICLES"},
        )
        bank = next(a for a in assets if a["assetType"] == "BANK_ACCOUNT")
        self.assertEqual(bank["assetDescription"], "Chase Checking")
        self.assertEqual(bank["currentValue"], 50000.0)

        # Beneficiaries mapped to Contacts with relationship
        contacts = payload["Contacts"]
        self.assertEqual(len(contacts), 2)
        self.assertTrue(all(c["relationship"] == "child" for c in contacts))
        self.assertEqual(contacts[0]["firstName"], "Jane")
        self.assertEqual(contacts[1]["firstName"], "Bob")

    def test_upsert_falls_back_to_create_on_404(self):
        """If PUT returns 404 (record not found), client should fall back to POST."""
        call_log: list[str] = []

        def side_effect(method, url, headers=None, json=None, timeout=None):
            call_log.append(method)
            if method == "PUT":
                return FakeResponse(404, {"success": False, "error": "not found"})
            return FakeResponse(200, {"message": "ok", "importId": "new-id-456"})

        with patch(
            "trustate.integrations.trustate_client.requests.request",
            side_effect=side_effect,
        ):
            result = self.trustate.upsert_matter(
                {
                    "IntegrationContext": {"externalId": "x", "source": "y"},
                    "Matter": {"displayFirstName": "A", "displayLastName": "B"},
                }
            )

        self.assertEqual(call_log, ["PUT", "POST"])
        self.assertEqual(result["importId"], "new-id-456")

    def test_client_validates_required_fields(self):
        """Client-side validation should reject payloads missing required fields."""
        with self.assertRaises(ValueError):
            self.trustate.create_matter({"Matter": {"displayFirstName": "A", "displayLastName": "B"}})
        with self.assertRaises(ValueError):
            self.trustate.create_matter(
                {
                    "IntegrationContext": {"externalId": "x", "source": "y"},
                    "Matter": {"displayFirstName": "A"},  # missing displayLastName
                }
            )

    def test_api_error_raised_on_400(self):
        with patch(
            "trustate.integrations.trustate_client.requests.request",
            return_value=FakeResponse(400, {"success": False, "error": "bad payload"}),
        ):
            with self.assertRaises(TrustateAPIError) as ctx:
                self.trustate.create_matter(
                    {
                        "IntegrationContext": {"externalId": "x", "source": "y"},
                        "Matter": {"displayFirstName": "A", "displayLastName": "B"},
                    }
                )
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("bad payload", ctx.exception.message)

    def test_pipeline_survives_trustate_outage(self):
        """If Trustate returns 500, the local pipeline still advances."""
        with patch(
            "trustate.integrations.trustate_client.requests.request",
            return_value=FakeResponse(500, {"success": False, "error": "boom"}),
        ):
            case = self.pipeline.handle_new_client(
                {
                    "deal_id": "GHL-OUTAGE",
                    "contact_first_name": "Jane",
                    "contact_last_name": "Smith",
                    "contact_email": "jane@example.com",
                    "decedent_first_name": "John",
                    "decedent_last_name": "Smith",
                    "relationship_to_decedent": "child",
                }
            )

        # Local case still progressed to intake_in_progress
        self.assertEqual(case.stage.value, "intake_in_progress")
        # Failure is noted on the case for later reconciliation
        self.assertTrue(any("Trustate sync failed" in n for n in case.notes))


class MapperUnitTest(unittest.TestCase):
    """Unit tests for the payload mapper — pure functions, no HTTP."""

    def test_minimal_case_produces_valid_payload(self):
        from trustate.workflow.models import (
            ContactInfo,
            Decedent,
            PetitionerInfo,
            ProbateCase,
            RelationshipToDecedent,
        )

        case = ProbateCase(
            crm_deal_id="DEAL-1",
            petitioner=PetitionerInfo(
                contact=ContactInfo(
                    first_name="Jane",
                    last_name="Doe",
                    email="jane@example.com",
                    phone="+15550000000",
                ),
                relationship=RelationshipToDecedent.CHILD,
            ),
            decedent=Decedent(first_name="John", last_name="Doe"),
        )

        payload = case_to_trustate_payload(case, MapperConfig(source="unit-test"))

        self.assertEqual(payload["IntegrationContext"]["externalId"], "DEAL-1")
        self.assertEqual(payload["IntegrationContext"]["source"], "unit-test")
        self.assertEqual(payload["Matter"]["displayFirstName"], "Jane")
        self.assertEqual(payload["Matter"]["displayLastName"], "Doe")
        self.assertEqual(payload["Matter"]["matterType"], "ESTATE_ADMINISTRATION")
        # Empty Contacts/Assets should be omitted from PUT-safe payload
        self.assertNotIn("Contacts", payload)
        self.assertNotIn("Assets", payload)


if __name__ == "__main__":
    unittest.main()
