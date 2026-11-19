"""Backend client for invoking the private Discovery Copilot Reasoning Engine / Agent Runtime."""

import logging
from typing import Any, AsyncIterator
import httpx
from google.auth.transport.requests import Request as GoogleAuthRequest
import google.auth

from gateway.app.config import settings

logger = logging.getLogger("gateway.backend_client")


class BackendClient:
    """Dispatches authenticated A2A calls to private Agent Runtime / Cloud Run."""

    def __init__(self, http_client: httpx.AsyncClient | None = None):
        self._http_client = http_client

    def _get_service_auth_header(self, target_url: str) -> dict[str, str]:
        """Obtains Google ADC service-to-service authorization token if running on GCP."""
        try:
            credentials, _ = google.auth.default()
            auth_req = GoogleAuthRequest()
            credentials.before_request(auth_req, "POST", target_url, {})
            # Extract Authorization header from credentials
            if hasattr(credentials, "token") and credentials.token:
                return {"Authorization": f"Bearer {credentials.token}"}
        except Exception as exc:
            logger.debug("Google ADC not configured or local environment: %s", exc)
        return {}

    async def forward_streaming_request(
        self,
        endpoint_url: str,
        body: dict[str, Any],
        gateway_assertion: str,
    ) -> AsyncIterator[bytes]:
        """Forwards A2A / reasoning_engine streaming request and streams back chunks."""
        headers = {
            "Content-Type": "application/json",
            "X-Enterprise-Gateway-Assertion": gateway_assertion,
        }
        headers.update(self._get_service_auth_header(endpoint_url))

        # Never log gateway_assertion or Authorization header
        logger.info("Forwarding streaming request to target backend: %s", endpoint_url)

        client = self._http_client or httpx.AsyncClient(timeout=settings.request_timeout_seconds)
        try:
            async with client.stream(
                "POST",
                endpoint_url,
                json=body,
                headers=headers,
            ) as response:
                if response.status_code >= 400:
                    error_bytes = await response.aread()
                    logger.warning("Backend returned HTTP %s: %s", response.status_code, error_bytes[:200])
                    yield error_bytes
                    return

                async for chunk in response.aiter_bytes():
                    yield chunk
        finally:
            if not self._http_client:
                await client.aclose()

    async def forward_sync_request(
        self,
        endpoint_url: str,
        body: dict[str, Any],
        gateway_assertion: str,
    ) -> tuple[int, dict[str, Any]]:
        """Forwards sync A2A request to target backend."""
        headers = {
            "Content-Type": "application/json",
            "X-Enterprise-Gateway-Assertion": gateway_assertion,
        }
        headers.update(self._get_service_auth_header(endpoint_url))

        logger.info("Forwarding sync request to target backend: %s", endpoint_url)
        client = self._http_client or httpx.AsyncClient(timeout=settings.request_timeout_seconds)
        try:
            response = await client.post(
                endpoint_url,
                json=body,
                headers=headers,
            )
            try:
                data = response.json()
            except Exception:
                data = {"detail": response.text}
            return response.status_code, data
        finally:
            if not self._http_client:
                await client.aclose()
