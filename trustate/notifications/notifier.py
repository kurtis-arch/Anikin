"""Notification system for all stakeholders.

Sends automated notifications via email, SMS, and in-app messages
at every stage of the probate workflow. Keeps clients, attorneys,
and internal team informed without manual follow-up.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from trustate.workflow.models import CaseStage, ProbateCase

logger = logging.getLogger(__name__)


class NotificationChannel(str, Enum):
    EMAIL = "email"
    SMS = "sms"
    IN_APP = "in_app"
    SLACK = "slack"


class NotificationPriority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


@dataclass
class Notification:
    recipient_email: str
    recipient_phone: str
    channel: NotificationChannel
    subject: str
    body: str
    priority: NotificationPriority = NotificationPriority.NORMAL
    case_id: str = ""
    metadata: dict[str, Any] = None


class NotificationSender(ABC):
    """Abstract sender — implement for your email/SMS provider."""

    @abstractmethod
    def send_email(self, to: str, subject: str, body: str, **kwargs) -> bool: ...

    @abstractmethod
    def send_sms(self, to: str, body: str, **kwargs) -> bool: ...


class SendGridSender(NotificationSender):
    """SendGrid email + Twilio SMS sender (placeholder for production)."""

    def __init__(self, sendgrid_api_key: str = "", twilio_sid: str = "", twilio_token: str = ""):
        self.sendgrid_api_key = sendgrid_api_key
        self.twilio_sid = twilio_sid
        self.twilio_token = twilio_token

    def send_email(self, to: str, subject: str, body: str, **kwargs) -> bool:
        logger.info("Sending email to %s: %s", to, subject)
        # In production:
        # sg = sendgrid.SendGridAPIClient(self.sendgrid_api_key)
        # message = Mail(from_email="noreply@trustate.com", to_emails=to, ...)
        # sg.send(message)
        return True

    def send_sms(self, to: str, body: str, **kwargs) -> bool:
        logger.info("Sending SMS to %s: %s...", to, body[:50])
        # In production:
        # client = Client(self.twilio_sid, self.twilio_token)
        # client.messages.create(body=body, from_="+1...", to=to)
        return True


# Stage-specific notification templates
STAGE_NOTIFICATIONS: dict[CaseStage, dict[str, str]] = {
    CaseStage.INTAKE_IN_PROGRESS: {
        "client_subject": "Welcome to Trustate — Let's Get Started",
        "client_body": (
            "Hi {client_name},\n\n"
            "Welcome! We've started your probate case for {decedent_name}. "
            "Please complete your intake form to get things moving:\n\n"
            "{intake_url}\n\n"
            "This should take about 10-15 minutes. The more details you "
            "provide now, the faster we can prepare your petition.\n\n"
            "Best,\nThe Trustate Team"
        ),
    },
    CaseStage.DOCUMENTS_REQUESTED: {
        "client_subject": "Documents Needed for Your Probate Case",
        "client_body": (
            "Hi {client_name},\n\n"
            "Great progress! We now need the following documents:\n\n"
            "{document_list}\n\n"
            "Upload them securely here: {upload_url}\n\n"
            "Best,\nThe Trustate Team"
        ),
    },
    CaseStage.PETITION_GENERATED: {
        "attorney_subject": "New Petition Ready for Review — {decedent_name}",
        "attorney_body": (
            "A new probate petition has been generated and is ready for "
            "your review.\n\n"
            "Case: {decedent_name} (Estate)\n"
            "Petitioner: {petitioner_name}\n"
            "Type: {probate_type}\n"
            "Estate Value: ${estate_value:,.2f}\n\n"
            "Please review and approve at: {review_url}\n"
        ),
    },
    CaseStage.ATTORNEY_APPROVED: {
        "client_subject": "Your Petition Has Been Approved — Filing Soon",
        "client_body": (
            "Hi {client_name},\n\n"
            "Great news! Your attorney has reviewed and approved the "
            "probate petition for {decedent_name}'s estate. We're now "
            "preparing to file with the court.\n\n"
            "We'll keep you updated on the filing status.\n\n"
            "Best,\nThe Trustate Team"
        ),
    },
    CaseStage.FILED_WITH_COURT: {
        "client_subject": "Your Probate Petition Has Been Filed!",
        "client_body": (
            "Hi {client_name},\n\n"
            "Your probate petition for {decedent_name}'s estate has been "
            "successfully filed with {court_name}.\n\n"
            "Case Number: {case_number}\n"
            "Filing Date: {filing_date}\n\n"
            "Next steps: The court will schedule a hearing. We'll notify "
            "you as soon as we have the date.\n\n"
            "Best,\nThe Trustate Team"
        ),
        "attorney_subject": "Filing Confirmed — {decedent_name} Estate",
        "attorney_body": (
            "Filing confirmed for {decedent_name} estate.\n"
            "Case Number: {case_number}\n"
            "Court: {court_name}\n"
        ),
    },
    CaseStage.HEARING_SCHEDULED: {
        "client_subject": "Court Hearing Scheduled — {decedent_name} Estate",
        "client_body": (
            "Hi {client_name},\n\n"
            "A hearing has been scheduled for {decedent_name}'s estate:\n\n"
            "Date: {hearing_date}\n"
            "Location: {hearing_location}\n"
            "Case Number: {case_number}\n\n"
            "Your attorney will represent you. We'll send a reminder "
            "before the hearing.\n\n"
            "Best,\nThe Trustate Team"
        ),
    },
    CaseStage.LETTERS_ISSUED: {
        "client_subject": "Letters Issued — You're Now the Personal Representative",
        "client_body": (
            "Hi {client_name},\n\n"
            "Congratulations! The court has issued Letters "
            "{letters_type} for {decedent_name}'s estate. You are now "
            "the official personal representative.\n\n"
            "This means you can now:\n"
            "- Access the decedent's bank accounts\n"
            "- Transfer property titles\n"
            "- Settle debts and distribute assets\n\n"
            "We'll guide you through the next steps.\n\n"
            "Best,\nThe Trustate Team"
        ),
    },
}


class NotificationOrchestrator:
    """Sends the right notifications at each stage transition."""

    def __init__(self, sender: NotificationSender):
        self.sender = sender

    def notify_stage_change(
        self, case: ProbateCase, from_stage: CaseStage, to_stage: CaseStage
    ):
        """Send all notifications triggered by a stage transition."""
        templates = STAGE_NOTIFICATIONS.get(to_stage, {})
        if not templates:
            return

        context = self._build_context(case)

        # Client notifications
        if "client_subject" in templates and case.petitioner:
            subject = templates["client_subject"].format(**context)
            body = templates["client_body"].format(**context)
            self.sender.send_email(case.petitioner.contact.email, subject, body)

            # Also send SMS for high-priority milestones
            if to_stage in (
                CaseStage.FILED_WITH_COURT,
                CaseStage.HEARING_SCHEDULED,
                CaseStage.LETTERS_ISSUED,
            ):
                sms_body = f"Trustate update: {subject}. Check your email for details."
                self.sender.send_sms(case.petitioner.contact.phone, sms_body)

        # Attorney notifications
        if "attorney_subject" in templates and case.assigned_attorney_email:
            subject = templates["attorney_subject"].format(**context)
            body = templates["attorney_body"].format(**context)
            self.sender.send_email(case.assigned_attorney_email, subject, body)

    def send_document_reminder(self, case: ProbateCase):
        """Send a reminder for outstanding documents."""
        pending = case.get_pending_documents()
        if not pending or not case.petitioner:
            return

        doc_names = ", ".join(d.doc_type.value.replace("_", " ") for d in pending)
        subject = "Reminder: Documents Still Needed"
        body = (
            f"Hi {case.petitioner.contact.first_name},\n\n"
            f"We're still waiting on: {doc_names}\n\n"
            f"Upload here: https://portal.trustate.com/upload?case_id={case.id}\n\n"
            f"Best,\nThe Trustate Team"
        )
        self.sender.send_email(case.petitioner.contact.email, subject, body)

    def _build_context(self, case: ProbateCase) -> dict[str, Any]:
        """Build the template context from case data."""
        decedent = case.decedent
        petitioner = case.petitioner

        pending_docs = case.get_pending_documents()
        doc_list = "\n".join(
            f"  - {d.doc_type.value.replace('_', ' ').title()}"
            for d in pending_docs
        )

        return {
            "client_name": petitioner.contact.first_name if petitioner else "",
            "petitioner_name": petitioner.contact.full_name if petitioner else "",
            "decedent_name": decedent.full_name if decedent else "",
            "probate_type": case.probate_type.value.replace("_", " ").title(),
            "estate_value": case.total_estate_value,
            "case_number": case.court.case_number or "Pending",
            "court_name": case.court.court_name or "County Court",
            "hearing_date": (
                case.court.hearing_date.strftime("%B %d, %Y at %I:%M %p")
                if case.court.hearing_date and hasattr(case.court.hearing_date, "strftime")
                else str(case.court.hearing_date) if case.court.hearing_date
                else "TBD"
            ),
            "hearing_location": case.court.hearing_location or "TBD",
            "filing_date": case.updated_at.strftime("%B %d, %Y"),
            "intake_url": f"https://intake.trustate.com/form?case_id={case.id}",
            "upload_url": f"https://portal.trustate.com/upload?case_id={case.id}",
            "review_url": f"https://portal.trustate.com/review?case_id={case.id}",
            "document_list": doc_list,
            "letters_type": (
                "Testamentary" if decedent and decedent.had_will else "of Administration"
            ),
        }
