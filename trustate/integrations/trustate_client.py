"""Trustate Import API client.

Wraps the Trustate Import API for creating and updating matters.

Endpoints:
    POST /import   -> create a new matter + contacts + assets + liabilities
    PUT  /import   -> update an existing matter (identified by source + externalId)

Authentication:
    X-PUBLIC-TOKEN + X-PRIVATE-KEY headers (issued by Trustate).

Payload schema is defined in integrations.mappers.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)


class TrustateAPIError(Exception):
    """Raised for non-2xx responses from the Trustate API."""

    def __init__(
        self,
        status_code: int,
        message: str,
        payload: Optional[dict] = None,
    ):
        super().__init__(f"Trustate API {status_code}: {message}")
        self.status_code = status_code
        self.message = message
        self.payload = payload or {}


class TrustateClient:
    """HTTP client for the Trustate Import API."""

    def __init__(
        self,
        host: str,
        public_token: str,
        private_key: str,
        timeout: int = 30,
        max_retries: int = 3,
    ):
        if not host:
            raise ValueError("Trustate host is required")
        if not public_token or not private_key:
            raise ValueError("Trustate public_token and private_key are required")

        self.host = host.rstrip("/")
        self.public_token = public_token
        self.private_key = private_key
        self.timeout = timeout
        self.max_retries = max_retries

    # ── Public methods ──────────────────────────────────────────────

    def create_matter(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a new matter in Trustate (POST /import).

        Identified by payload["IntegrationContext"]["source"] +
        payload["IntegrationContext"]["externalId"].

        Returns the JSON response, typically:
            {"message": "...", "importId": "<estateImportId>"}
        """
        return self._request("POST", "/import", payload)

    def update_matter(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Update an existing matter (PUT /import).

        Only fields present in payload will be updated. The record
        is identified by IntegrationContext.source + externalId,
        which must match an existing record (404 otherwise).
        """
        return self._request("PUT", "/import", payload)

    def upsert_matter(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create-or-update: try PUT first, fall back to POST on 404.

        This is the recommended entry point when you're not sure
        whether the matter already exists in Trustate.
        """
        try:
            return self.update_matter(payload)
        except TrustateAPIError as e:
            if e.status_code == 404:
                logger.info("Matter not found on update — creating new record")
                return self.create_matter(payload)
            raise

    # ── Internal ────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-PUBLIC-TOKEN": self.public_token,
            "X-PRIVATE-KEY": self.private_key,
        }

    def _request(
        self, method: str, path: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Send a request with retries for transient errors."""
        url = f"{self.host}{path}"
        self._validate_payload(payload)

        last_err: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.request(
                    method,
                    url,
                    headers=self._headers(),
                    json=payload,
                    timeout=self.timeout,
                )
            except requests.RequestException as e:
                last_err = e
                if attempt < self.max_retries:
                    backoff = 2 ** (attempt - 1)
                    logger.warning(
                        "Trustate %s %s network error (attempt %d/%d): %s — retrying in %ds",
                        method,
                        path,
                        attempt,
                        self.max_retries,
                        e,
                        backoff,
                    )
                    time.sleep(backoff)
                    continue
                raise TrustateAPIError(0, f"Network error: {e}") from e

            # Parse body
            try:
                body = resp.json() if resp.content else {}
            except ValueError:
                body = {"error": resp.text}

            # Retry on 5xx
            if 500 <= resp.status_code < 600 and attempt < self.max_retries:
                backoff = 2 ** (attempt - 1)
                logger.warning(
                    "Trustate %s %s returned %d (attempt %d/%d) — retrying in %ds",
                    method,
                    path,
                    resp.status_code,
                    attempt,
                    self.max_retries,
                    backoff,
                )
                time.sleep(backoff)
                continue

            if resp.status_code >= 400:
                raise TrustateAPIError(
                    resp.status_code,
                    body.get("error") or body.get("message") or resp.reason,
                    body,
                )

            logger.info(
                "Trustate %s %s -> %d importId=%s",
                method,
                path,
                resp.status_code,
                body.get("importId", ""),
            )
            return body

        raise TrustateAPIError(0, f"Exhausted retries: {last_err}")

    @staticmethod
    def _validate_payload(payload: dict[str, Any]) -> None:
        """Minimal client-side validation — catches obvious mistakes before HTTP."""
        ctx = payload.get("IntegrationContext") or {}
        if not ctx.get("externalId") or not ctx.get("source"):
            raise ValueError(
                "IntegrationContext.externalId and .source are required"
            )
        matter = payload.get("Matter") or {}
        if not matter.get("displayFirstName") or not matter.get("displayLastName"):
            raise ValueError(
                "Matter.displayFirstName and Matter.displayLastName are required"
            )
