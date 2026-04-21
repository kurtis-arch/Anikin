"""End-to-end test: GHL opportunity won -> Trustate + Cognito intake + PandaDoc fee agreement.

Simulates the full fan-out when a sales closer marks the deal won in
GoHighLevel, with both Trustate and PandaDoc HTTP mocked.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from trustate.integrations.ghl import (
    event_to_crm_payload,
    is_won_status,
    parse_ghl_webhook,
)
from trustate.integrations.mappers import MapperConfig
from trustate.integrations.pandadoc import (
    PandaDocClient,
    parse_webhook_event,
)
from trustate.integrations.trustate_client import TrustateClient
from trustate.pipeline import ProbatePipeline
from trustate.workflow.store import JSONFileStore


class FakeHTTP:
    """Mock HTTP layer that captures calls and returns scripted responses.

    Both trustate_client and pandadoc import `requests`, which is the
    same module object, so we use ONE side_effect routed by URL.
    """

    def __init__(self):
        self.calls: list[dict] = []
        self.trustate_status = 200
        self.trustate_body = {"message": "ok", "importId": "imp-999"}
        self.pandadoc_template_response = {"id": "doc-abc", "status": "document.uploaded"}
        self.pandadoc_status_response = {"id": "doc-abc", "status": "document.draft"}
        self.pandadoc_send_response = {"id": "doc-abc", "status": "document.sent"}
        self.pandadoc_override = None  # per-test override to simulate errors

    def side_effect(self, method, url, headers=None, json=None, timeout=None):
        if "trustate" in url or url.endswith("/import"):
            self.calls.append({"svc": "trustate", "method": method, "url": url, "json": json})
            return _FakeResponse(self.trustate_status, self.trustate_body)
        if "pandadoc" in url:
            self.calls.append({"svc": "pandadoc", "method": method, "url": url, "json": json})
            if self.pandadoc_override is not None:
                return self.pandadoc_override
            if url.endswith("/send"):
                return _FakeResponse(200, self.pandadoc_send_response)
            if method == "GET" and "/documents/" in url:
                return _FakeResponse(200, self.pandadoc_status_response)
            if method == "POST" and url.endswith("/documents"):
                return _FakeResponse(201, self.pandadoc_template_response)
        return _FakeResponse(200, {})


class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.content = json.dumps(body).encode()
        self.reason = "OK"
        self.text = json.dumps(body)

    def json(self):
        return self._body


def _ghl_webhook(**overrides) -> dict:
    """Build a realistic GHL native opportunity webhook payload."""
    payload = {
        "type": "OpportunityStatusUpdate",
        "locationId": "loc-abc",
        "opportunity": {
            "id": "opp-12345",
            "name": "Garcia Probate - Roberto",
            "pipelineId": "pipeline-probate",
            "pipelineStageId": "stage-won",
            "status": "won",
            "monetaryValue": 2500.0,
            "assignedTo": "closer-mike",
        },
        "contact": {
            "id": "contact-98",
            "firstName": "Maria",
            "lastName": "Garcia",
            "email": "maria.garcia@example.com",
            "phone": "+15551234567",
            "address1": "456 Oak Avenue",
            "city": "Los Angeles",
            "state": "CA",
            "postalCode": "90001",
            "customFields": [
                {"name": "decedent_first_name", "value": "Roberto"},
                {"name": "decedent_last_name", "value": "Garcia"},
                {"name": "relationship_to_decedent", "value": "child"},
                {"name": "law_firm_id", "value": "firm-001"},
                {"name": "county", "value": "Los Angeles"},
            ],
        },
    }
    payload.update(overrides)
    return payload


class GHLFanoutTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.http = FakeHTTP()

        http_patcher = patch(
            "requests.request",
            side_effect=self.http.side_effect,
        )
        http_patcher.start()
        self.addCleanup(http_patcher.stop)

        self.pipeline = ProbatePipeline(
            store=JSONFileStore(data_dir=os.path.join(self.tmpdir, "cases")),
            trustate_client=TrustateClient(
                host="https://fake-trustate.test",
                public_token="pub",
                private_key="priv",
            ),
            mapper_config=MapperConfig(source="test-pipeline"),
            pandadoc_client=PandaDocClient(api_key="fake-pd-key"),
            fee_agreement_template_id="tmpl-fee-abc",
        )

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ── GHL parser ──────────────────────────────────────────────────

    def test_ghl_parser_extracts_contact_and_custom_fields(self):
        event = parse_ghl_webhook(_ghl_webhook())
        self.assertEqual(event.opportunity_id, "opp-12345")
        self.assertEqual(event.first_name, "Maria")
        self.assertEqual(event.last_name, "Garcia")
        self.assertEqual(event.email, "maria.garcia@example.com")
        self.assertEqual(event.state, "CA")
        self.assertEqual(event.decedent_first_name, "Roberto")
        self.assertEqual(event.decedent_last_name, "Garcia")
        self.assertEqual(event.relationship_to_decedent, "child")
        self.assertEqual(event.law_firm_id, "firm-001")
        self.assertEqual(event.county, "Los Angeles")

    def test_ghl_parser_handles_flat_workflow_shape(self):
        """GHL workflow webhook action can send a flat custom payload."""
        flat = {
            "opportunityId": "opp-99",
            "contactId": "c-1",
            "firstName": "Jane",
            "lastName": "Doe",
            "email": "jane@example.com",
            "phone": "+15550000000",
            "state": "tx",
            "decedent_first_name": "John",
            "decedent_last_name": "Doe",
            "relationship_to_decedent": "spouse",
            "law_firm_id": "firm-777",
        }
        event = parse_ghl_webhook(flat)
        self.assertEqual(event.opportunity_id, "opp-99")
        self.assertEqual(event.email, "jane@example.com")
        self.assertEqual(event.decedent_first_name, "John")
        self.assertEqual(event.law_firm_id, "firm-777")

    def test_is_won_status_filters_non_won(self):
        self.assertTrue(is_won_status(_ghl_webhook()))
        self.assertFalse(
            is_won_status(
                _ghl_webhook(opportunity={"id": "x", "status": "lost"})
            )
        )
        # No status field -> trusted trigger (workflow webhook)
        self.assertTrue(is_won_status({"opportunityId": "x"}))

    # ── Full fan-out ────────────────────────────────────────────────

    def test_won_webhook_fires_trustate_and_pandadoc_and_intake_email(self):
        payload = _ghl_webhook()
        event = parse_ghl_webhook(payload)
        crm_payload = event_to_crm_payload(event)

        case = self.pipeline.handle_new_client(crm_payload)

        # Trustate: one POST /import (create)
        trustate_calls = [c for c in self.http.calls if c["svc"] == "trustate"]
        self.assertGreaterEqual(len(trustate_calls), 1)
        create = trustate_calls[0]
        self.assertEqual(create["method"], "POST")
        self.assertIn("/import", create["url"])
        self.assertEqual(
            create["json"]["IntegrationContext"]["externalId"], "opp-12345"
        )
        self.assertEqual(create["json"]["Matter"]["displayFirstName"], "Maria")

        # PandaDoc: POST /documents (create from template) + GET (status poll) + POST /documents/{id}/send
        pd_calls = [c for c in self.http.calls if c["svc"] == "pandadoc"]
        creates = [c for c in pd_calls if c["method"] == "POST" and c["url"].endswith("/documents")]
        sends = [c for c in pd_calls if c["method"] == "POST" and c["url"].endswith("/send")]
        self.assertEqual(len(creates), 1)
        self.assertEqual(len(sends), 1)

        # The PandaDoc body uses the configured template and pre-fills client info
        create_body = creates[0]["json"]
        self.assertEqual(create_body["template_uuid"], "tmpl-fee-abc")
        self.assertEqual(create_body["recipients"][0]["email"], "maria.garcia@example.com")
        self.assertEqual(create_body["recipients"][0]["first_name"], "Maria")
        # Fields are pre-filled from our case data
        self.assertIn("Client.FullName", create_body["fields"])
        self.assertEqual(
            create_body["fields"]["Client.FullName"]["value"], "Maria Garcia"
        )
        # Metadata links back to our case for the signed webhook
        self.assertEqual(create_body["metadata"]["case_id"], case.id)

        # Case now tracks BOTH integration IDs
        self.assertEqual(case.trustate_import_id, "imp-999")
        self.assertEqual(case.pandadoc_document_id, "doc-abc")
        self.assertEqual(case.pandadoc_status, "document.sent")
        self.assertIsNotNone(case.intake_email_sent_at)
        # And sits in the right stage for the client to fill the form
        self.assertEqual(case.stage.value, "intake_in_progress")

    def test_pandadoc_failure_does_not_break_trustate_sync(self):
        """If PandaDoc is down, we still want the Trustate matter created."""
        self.http.pandadoc_override = _FakeResponse(500, {"error": "boom"})

        case = self.pipeline.handle_new_client(
            event_to_crm_payload(parse_ghl_webhook(_ghl_webhook()))
        )

        # Trustate still got the create
        self.assertEqual(case.trustate_import_id, "imp-999")
        # But PandaDoc failure is noted on the case
        self.assertEqual(case.pandadoc_document_id, "")
        self.assertTrue(any("PandaDoc" in n for n in case.notes))
        # Pipeline still reached intake
        self.assertEqual(case.stage.value, "intake_in_progress")

    # ── PandaDoc signed webhook ─────────────────────────────────────

    def test_pandadoc_completed_event_marks_fee_agreement_signed(self):
        # Set up a case with a PandaDoc doc id
        case = self.pipeline.handle_new_client(
            event_to_crm_payload(parse_ghl_webhook(_ghl_webhook()))
        )
        self.assertEqual(case.pandadoc_document_id, "doc-abc")
        self.assertFalse(case.fee_agreement_signed)

        # Simulate PandaDoc webhook for a completed signing
        completed_event = {
            "event": "document_state_changed",
            "data": {
                "id": "doc-abc",
                "status": "document.completed",
                "recipients": [
                    {
                        "email": "maria.garcia@example.com",
                        "has_completed": True,
                    }
                ],
            },
        }

        self.http.calls.clear()
        updated = self.pipeline.handle_pandadoc_webhook([completed_event])

        self.assertEqual(len(updated), 1)
        case = updated[0]
        self.assertTrue(case.fee_agreement_signed)
        self.assertIsNotNone(case.fee_agreement_signed_at)
        self.assertEqual(case.pandadoc_status, "document.completed")

        # Trustate got a sync update reflecting the signed fee agreement
        trustate_puts = [
            c for c in self.http.calls if c["svc"] == "trustate" and c["method"] == "PUT"
        ]
        self.assertGreaterEqual(len(trustate_puts), 1)

    def test_pandadoc_viewed_event_updates_status_but_not_signed(self):
        case = self.pipeline.handle_new_client(
            event_to_crm_payload(parse_ghl_webhook(_ghl_webhook()))
        )

        viewed_event = {
            "event": "document_state_changed",
            "data": {
                "id": "doc-abc",
                "status": "document.viewed",
                "recipients": [{"email": "maria.garcia@example.com"}],
            },
        }

        updated = self.pipeline.handle_pandadoc_webhook([viewed_event])
        self.assertEqual(updated[0].pandadoc_status, "document.viewed")
        self.assertFalse(updated[0].fee_agreement_signed)

    def test_pandadoc_event_for_unknown_document_is_ignored(self):
        """Events for documents we didn't create (or already deleted) don't error."""
        unknown_event = {
            "event": "document_state_changed",
            "data": {
                "id": "doc-unknown-xyz",
                "status": "document.completed",
                "recipients": [],
            },
        }
        updated = self.pipeline.handle_pandadoc_webhook([unknown_event])
        self.assertEqual(updated, [])

    def test_pandadoc_event_parser_extracts_fields(self):
        event = parse_webhook_event(
            {
                "event": "document_state_changed",
                "data": {
                    "id": "doc-abc",
                    "status": "document.completed",
                    "recipients": [
                        {"email": "a@example.com"},
                        {"email": "b@example.com", "has_completed": True},
                    ],
                    "metadata": {"case_id": "case-xyz"},
                },
            }
        )
        self.assertEqual(event.document_id, "doc-abc")
        self.assertEqual(event.status, "document.completed")
        self.assertEqual(event.recipient_email, "b@example.com")
        self.assertEqual(event.metadata["case_id"], "case-xyz")


if __name__ == "__main__":
    unittest.main()
