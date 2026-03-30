"""Configuration for the Trustate automation pipeline.

All secrets and environment-specific settings are loaded from
environment variables (via .env file in development).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass
class TrustateConfig:
    """Central configuration — reads from environment variables."""

    # API Server
    api_host: str = os.getenv("TRUSTATE_API_HOST", "0.0.0.0")
    api_port: int = int(os.getenv("TRUSTATE_API_PORT", "8000"))

    # Data storage
    data_dir: str = os.getenv("TRUSTATE_DATA_DIR", "data/cases")

    # CRM Integration (GoHighLevel, HubSpot, etc.)
    crm_api_key: str = os.getenv("CRM_API_KEY", "")
    crm_webhook_secret: str = os.getenv("CRM_WEBHOOK_SECRET", "")

    # Email (SendGrid)
    sendgrid_api_key: str = os.getenv("SENDGRID_API_KEY", "")
    notification_from_email: str = os.getenv(
        "NOTIFICATION_FROM_EMAIL", "noreply@trustate.com"
    )

    # SMS (Twilio)
    twilio_account_sid: str = os.getenv("TWILIO_ACCOUNT_SID", "")
    twilio_auth_token: str = os.getenv("TWILIO_AUTH_TOKEN", "")
    twilio_from_number: str = os.getenv("TWILIO_FROM_NUMBER", "")

    # Court E-Filing (Tyler Technologies)
    efiling_api_url: str = os.getenv("EFILING_API_URL", "")
    efiling_api_key: str = os.getenv("EFILING_API_KEY", "")
    efiling_firm_id: str = os.getenv("EFILING_FIRM_ID", "")

    # Aircall (for call recordings integration)
    aircall_api_id: str = os.getenv("AIRCALL_API_ID", "")
    aircall_api_token: str = os.getenv("AIRCALL_API_TOKEN", "")

    # Notion (for internal tracking)
    notion_api_key: str = os.getenv("NOTION_API_KEY", "")
    notion_database_id: str = os.getenv("NOTION_DATABASE_ID", "")

    # Client Portal
    portal_base_url: str = os.getenv(
        "PORTAL_BASE_URL", "https://portal.trustate.com"
    )
    intake_form_base_url: str = os.getenv(
        "INTAKE_FORM_BASE_URL", "https://intake.trustate.com/form"
    )


config = TrustateConfig()
