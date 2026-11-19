"""Security and Authentication middleware for Ingestion Copilot.

Enforces:
1. Google Cloud Identity-Aware Proxy (IAP) JWT Assertion validation:
   - Header: 'X-Goog-IAP-JWT-Assertion'
   - Retrieves public keys from https://www.gstatic.com/iap/verify/public_key
   - Validates cryptographic signature via google.auth.jwt.decode(iap_jwt, certs=keys)
   - Validates audience (IAP project & backend service ID) and expiration.
   - Validates issuer ('https://cloud.google.com/iap').
   - Handles certificate rotation and key retrieval failures safely.
   - Extracts email and subject from claims: 'email', 'sub'.
2. Google Cloud Pub/Sub Push OIDC JWT Authentication:
   - Header: 'Authorization: Bearer <ID_TOKEN>'
   - Retrieves public keys from https://www.googleapis.com/oauth2/v1/certs
   - Validates cryptographic signature, expiry, audience, issuer ('https://accounts.google.com')
   - Validates service account identity matches authorized Pub/Sub publisher SA.
3. Local/Test Authentication Support:
   - Header: 'X-Enterprise-Test-Principal' (strictly permitted ONLY when ENABLE_LOCAL_TEST_AUTH='true').
   - Header: 'X-Enterprise-Test-PubSub-Principal' for Pub/Sub push testing.
   - Rejects unverified or malformed test identities.
4. Fail-Closed Security Policy:
   - Missing, invalid, expired, or untrusted authentication immediately halts request with 401 Unauthorized or 403 Forbidden.
5. Session User-ID Enforcement & Anti-Spoofing:
   - For all ADK session routes (/apps/app/users/{user_id}/sessions), the URL {user_id} parameter
     MUST match the verified authenticated identity.
   - Any attempt to create or access a session using a different user_id returns 403 Forbidden.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any
import urllib.error
import urllib.request

from fastapi import Request, Response, status
from google.auth import jwt as google_jwt
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("ingestion_copilot.security")

# RFC 5322 simplified email regex
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")

IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
IAP_ISSUER = "https://cloud.google.com/iap"
GOOGLE_OAUTH2_CERTS_URL = "https://www.googleapis.com/oauth2/v1/certs"
GOOGLE_OIDC_ISSUER = "https://accounts.google.com"

CERTS_CACHE_TTL_SECONDS = 3600  # 1 hour cache TTL

_certs_cache: dict[str, str] = {}
_certs_cache_timestamp: float = 0.0

_oauth2_certs_cache: dict[str, str] = {}
_oauth2_certs_cache_timestamp: float = 0.0

DEFAULT_ALLOWED_PUBSUB_SERVICE_ACCOUNTS = frozenset({
    "ingestion-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com",
    "cloud-run-pubsub-invoker@xenon-broker-513704-s9.iam.gserviceaccount.com",
})


class AuthenticationError(Exception):
    """Raised when request identity authentication fails."""


class AuthorizationError(AuthenticationError):
    """Raised when authenticated identity is not authorized for the requested resource."""


def is_local_test_auth_enabled() -> bool:
    """Checks whether explicit local test authentication is enabled."""
    return os.getenv("ENABLE_LOCAL_TEST_AUTH", "false").lower() in ("true", "1")


def get_iap_audience() -> str | None:
    """Retrieves expected Google Cloud IAP audience string from environment."""
    return os.getenv("IAP_AUDIENCE")


def get_pubsub_push_audience() -> str | None:
    """Retrieves expected Pub/Sub push audience string from environment."""
    return os.getenv("PUBSUB_PUSH_AUDIENCE")


def get_allowed_pubsub_service_accounts() -> frozenset[str]:
    """Retrieves set of authorized service account emails for Pub/Sub push authentication."""
    env_sa = os.getenv("PUBSUB_ALLOWED_SERVICE_ACCOUNTS")
    if env_sa:
        return frozenset(sa.strip().lower() for sa in env_sa.split(",") if sa.strip())
    return DEFAULT_ALLOWED_PUBSUB_SERVICE_ACCOUNTS


def fetch_iap_public_keys(force_refresh: bool = False) -> dict[str, str]:
    """Retrieves Google Cloud IAP public keys from Google.

    Caches public keys in memory with a 1-hour TTL. Refreshes on cache expiry or on demand (e.g. key rotation).
    Fails closed if the public key endpoint is unreachable and no cache is present.
    """
    global _certs_cache, _certs_cache_timestamp
    current_time = time.time()
    if not force_refresh and _certs_cache and (current_time - _certs_cache_timestamp < CERTS_CACHE_TTL_SECONDS):
        return _certs_cache

    try:
        req = urllib.request.Request(
            IAP_CERTS_URL,
            headers={"User-Agent": "Enterprise-Ingestion-Copilot/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            if response.status != 200:
                raise AuthenticationError(f"IAP public key endpoint returned HTTP {response.status}")
            data = response.read().decode("utf-8")
            certs = json.loads(data)
            if not isinstance(certs, dict) or not certs:
                raise AuthenticationError("IAP public key endpoint returned empty or invalid keys")
            _certs_cache = certs
            _certs_cache_timestamp = current_time
            return _certs_cache
    except Exception as exc:
        logger.error("Failed to retrieve Google Cloud IAP public keys: %s", exc)
        if _certs_cache:
            return _certs_cache
        raise AuthenticationError(f"Failed to retrieve IAP public keys: {exc}") from exc


def fetch_google_oauth2_public_keys(force_refresh: bool = False) -> dict[str, str]:
    """Retrieves Google OAuth2 public keys for Pub/Sub OIDC token verification.

    Caches public keys in memory with a 1-hour TTL.
    """
    global _oauth2_certs_cache, _oauth2_certs_cache_timestamp
    current_time = time.time()
    if not force_refresh and _oauth2_certs_cache and (current_time - _oauth2_certs_cache_timestamp < CERTS_CACHE_TTL_SECONDS):
        return _oauth2_certs_cache

    try:
        req = urllib.request.Request(
            GOOGLE_OAUTH2_CERTS_URL,
            headers={"User-Agent": "Enterprise-Ingestion-Copilot/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            if response.status != 200:
                raise AuthenticationError(f"Google OAuth2 public key endpoint returned HTTP {response.status}")
            data = response.read().decode("utf-8")
            certs = json.loads(data)
            if not isinstance(certs, dict) or not certs:
                raise AuthenticationError("Google OAuth2 cert endpoint returned empty or invalid keys")
            _oauth2_certs_cache = certs
            _oauth2_certs_cache_timestamp = current_time
            return _oauth2_certs_cache
    except Exception as exc:
        logger.error("Failed to retrieve Google OAuth2 public keys: %s", exc)
        if _oauth2_certs_cache:
            return _oauth2_certs_cache
        raise AuthenticationError(f"Failed to retrieve Google OAuth2 public keys: {exc}") from exc


def clear_certs_cache() -> None:
    """Clears all public certificate caches."""
    global _certs_cache, _certs_cache_timestamp, _oauth2_certs_cache, _oauth2_certs_cache_timestamp
    _certs_cache = {}
    _certs_cache_timestamp = 0.0
    _oauth2_certs_cache = {}
    _oauth2_certs_cache_timestamp = 0.0


def verify_iap_jwt(
    iap_jwt: str,
    expected_audience: str | None = None,
    certs_override: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Cryptographically validates Google Cloud IAP JWT token against Google public certs."""
    try:
        header = google_jwt.decode_header(iap_jwt)
    except Exception as exc:
        logger.warning("Failed to decode IAP JWT header: %s", exc)
        raise AuthenticationError(f"Invalid IAP JWT header: {exc}") from exc

    kid = header.get("kid")
    if not kid:
        raise AuthenticationError("IAP JWT header missing 'kid' key ID")

    if certs_override is not None:
        certs = certs_override
    else:
        certs = fetch_iap_public_keys(force_refresh=False)
        if kid not in certs:
            logger.info("Key ID '%s' not found in cached IAP certs. Refreshing certs...", kid)
            certs = fetch_iap_public_keys(force_refresh=True)
            if kid not in certs:
                raise AuthenticationError(f"Key ID '{kid}' not found in Google IAP public keys")

    try:
        claims = google_jwt.decode(
            iap_jwt,
            certs=certs,
            verify=True,
            audience=expected_audience,
        )
    except Exception as exc:
        logger.warning("IAP JWT cryptographic verification failed: %s", exc)
        raise AuthenticationError(f"Invalid or expired IAP JWT assertion: {exc}") from exc

    issuer = claims.get("iss")
    if issuer != IAP_ISSUER:
        logger.warning("Invalid IAP token issuer '%s' (expected '%s')", issuer, IAP_ISSUER)
        raise AuthenticationError(f"Invalid IAP token issuer: '{issuer}'")

    return claims


def verify_pubsub_oidc_jwt(
    bearer_token: str,
    expected_audience: str | None = None,
    allowed_service_accounts: frozenset[str] | None = None,
    certs_override: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Cryptographically validates Google Cloud Pub/Sub OIDC ID token.

    Verifies:
    1. Header decoding and key ID resolution against Google OAuth2 certs.
    2. Signature, expiry, and audience via google.auth.jwt.decode.
    3. Issuer is https://accounts.google.com.
    4. Service account email claim is present, valid, and matches allowed SAs.
    """
    token = bearer_token.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    if not token:
        raise AuthenticationError("Pub/Sub OIDC token is empty")

    try:
        header = google_jwt.decode_header(token)
    except Exception as exc:
        logger.warning("Failed to decode Pub/Sub OIDC JWT header: %s", exc)
        raise AuthenticationError(f"Invalid Pub/Sub OIDC JWT header: {exc}") from exc

    kid = header.get("kid")
    if not kid:
        raise AuthenticationError("Pub/Sub OIDC JWT header missing 'kid' key ID")

    if certs_override is not None:
        certs = certs_override
    else:
        certs = fetch_google_oauth2_public_keys(force_refresh=False)
        if kid not in certs:
            logger.info("Key ID '%s' not found in cached OAuth2 certs. Refreshing...", kid)
            certs = fetch_google_oauth2_public_keys(force_refresh=True)
            if kid not in certs:
                raise AuthenticationError(f"Key ID '{kid}' not found in Google OAuth2 public keys")

    try:
        claims = google_jwt.decode(
            token,
            certs=certs,
            verify=True,
            audience=expected_audience,
        )
    except Exception as exc:
        logger.warning("Pub/Sub OIDC JWT cryptographic verification failed: %s", exc)
        raise AuthenticationError(f"Invalid or expired Pub/Sub OIDC JWT: {exc}") from exc

    # Validate issuer
    issuer = claims.get("iss")
    if issuer not in (GOOGLE_OIDC_ISSUER, "accounts.google.com"):
        logger.warning("Invalid Pub/Sub OIDC token issuer '%s'", issuer)
        raise AuthenticationError(f"Invalid Pub/Sub OIDC token issuer: '{issuer}'")

    # Validate email claim
    email = claims.get("email")
    if not email or not EMAIL_REGEX.match(email):
        raise AuthenticationError(f"Pub/Sub OIDC token missing valid email claim: '{email}'")

    sa_email = email.lower()
    allowed_sas = allowed_service_accounts or get_allowed_pubsub_service_accounts()
    if allowed_sas and sa_email not in allowed_sas:
        logger.warning("Unauthorized Pub/Sub service account: '%s' not in allowed %s", sa_email, allowed_sas)
        raise AuthorizationError(f"Unauthorized Pub/Sub publisher identity: '{sa_email}'")

    return claims


def extract_authenticated_principal(request: Request) -> str:
    """Extracts and verifies the caller's authenticated identity via Google Cloud IAP.

    Fails closed if authentication is missing or invalid.
    """
    iap_jwt = request.headers.get("X-Goog-IAP-JWT-Assertion")
    if iap_jwt:
        audience = get_iap_audience()
        claims = verify_iap_jwt(iap_jwt, expected_audience=audience)
        email = claims.get("email")
        if not email or not EMAIL_REGEX.match(email):
            raise AuthenticationError(f"IAP token missing valid email claim: '{email}'")
        return email.lower()

    if is_local_test_auth_enabled():
        test_principal = request.headers.get("X-Enterprise-Test-Principal")
        if test_principal and EMAIL_REGEX.match(test_principal.strip()):
            return test_principal.strip().lower()

    raise AuthenticationError("Authentication required: Missing valid X-Goog-IAP-JWT-Assertion.")


def extract_and_verify_pubsub_principal(request: Request) -> str:
    """Extracts and verifies Google Cloud Pub/Sub push subscription OIDC identity.

    Fails closed if Authorization header is missing, token is invalid/expired,
    audience does not match, or publisher service account is unauthorized.
    """
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.strip().lower().startswith("bearer "):
        audience = get_pubsub_push_audience()
        allowed_sas = get_allowed_pubsub_service_accounts()
        claims = verify_pubsub_oidc_jwt(
            auth_header,
            expected_audience=audience,
            allowed_service_accounts=allowed_sas,
        )
        return claims["email"].lower()

    if is_local_test_auth_enabled():
        test_sa = request.headers.get("X-Enterprise-Test-PubSub-Principal")
        if test_sa and EMAIL_REGEX.match(test_sa.strip()):
            sa_email = test_sa.strip().lower()
            allowed_sas = get_allowed_pubsub_service_accounts()
            if allowed_sas and sa_email not in allowed_sas:
                raise AuthorizationError(f"Unauthorized Pub/Sub test service account: '{sa_email}'")
            return sa_email

    raise AuthenticationError("Pub/Sub OIDC authentication required: Missing or invalid Authorization Bearer token.")


class EnforceAuthenticatedPrincipalMiddleware(BaseHTTPMiddleware):
    """Middleware enforcing authenticated caller identity across all API and ADK routes."""

    EXEMPT_PATHS = frozenset({"/health", "/healthz", "/docs", "/openapi.json", "/redoc", "/api/v1/pubsub/push"})

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path in self.EXEMPT_PATHS:
            return await call_next(request)

        try:
            authenticated_email = extract_authenticated_principal(request)
        except AuthenticationError as exc:
            logger.warning("Rejecting unauthenticated request to '%s': %s", request.url.path, exc)
            return Response(
                content=f'{{"detail": "{str(exc)}"}}',
                status_code=status.HTTP_401_UNAUTHORIZED,
                media_type="application/json",
            )

        request.state.authenticated_user_email = authenticated_email

        path_parts = request.url.path.strip("/").split("/")
        if len(path_parts) >= 4 and path_parts[0] == "apps" and path_parts[2] == "users":
            requested_user_id = path_parts[3].lower()
            if requested_user_id != authenticated_email:
                logger.error(
                    "Identity mismatch detected! Authenticated '%s' attempted to access user route for '%s'",
                    authenticated_email,
                    requested_user_id,
                )
                return Response(
                    content='{"detail": "Forbidden: Identity mismatch between authenticated token and user route."}',
                    status_code=status.HTTP_403_FORBIDDEN,
                    media_type="application/json",
                )

        return await call_next(request)
