"""Security and Authentication middleware for Discovery Copilot (Phase 4.1).

Enforces:
1. Google Cloud Identity-Aware Proxy (IAP) JWT Assertion validation:
   - Header: 'X-Goog-IAP-JWT-Assertion'
   - Retrieves public keys from https://www.gstatic.com/iap/verify/public_key
   - Validates cryptographic signature via google.auth.jwt.decode(iap_jwt, certs=keys)
   - Validates audience (IAP project & backend service ID) and expiration.
   - Validates issuer ('https://cloud.google.com/iap').
   - Handles certificate rotation and key retrieval failures safely.
   - Extracts email and subject from claims: 'email', 'sub'.
2. Local/Test Authentication Support:
   - Header: 'X-Enterprise-Test-Principal' (strictly permitted ONLY when ENABLE_LOCAL_TEST_AUTH='true').
   - Rejects unverified or malformed test identities.
3. Fail-Closed Security Policy:
   - Missing, invalid, expired, or untrusted authentication immediately halts request with 401 Unauthorized.
4. Session User-ID Enforcement & Anti-Spoofing:
   - For all ADK session routes (/apps/app/users/{user_id}/sessions), the URL {user_id} parameter
     MUST match the verified authenticated identity.
   - Any attempt to create or access a session using a different user_id returns 403 Forbidden.
"""

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

logger = logging.getLogger("discovery_copilot.security")

# Replay protection cache for short-lived gateway tokens: jti -> exp_timestamp
_seen_jti_cache: dict[str, float] = {}


def clear_jti_cache() -> None:
    """Clears the JTI replay cache (useful for testing)."""
    global _seen_jti_cache
    _seen_jti_cache = {}


def _cleanup_expired_jtis(current_time: float) -> None:
    """Removes expired JTIs from memory."""
    global _seen_jti_cache
    if len(_seen_jti_cache) > 10000:
        _seen_jti_cache = {jti: exp for jti, exp in _seen_jti_cache.items() if exp > current_time}
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")

IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
IAP_ISSUER = "https://cloud.google.com/iap"
GOOGLE_OIDC_CERTS_URL = "https://www.googleapis.com/oauth2/v1/certs"
GOOGLE_OIDC_ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})
CERTS_CACHE_TTL_SECONDS = 3600  # 1 hour cache TTL

# Gateway assertion configuration
GATEWAY_HEADER_NAME = "X-Enterprise-Gateway-Assertion"
GATEWAY_ISSUER = os.getenv("GATEWAY_ISSUER", "https://gateway.enterprise-graphrag.internal")

# Direct Gemini Enterprise Agent Runtime Integration Configuration
AUTHORIZED_DISCOVERY_ENGINE_SERVICE_ACCOUNT = os.getenv(
    "AUTHORIZED_DISCOVERY_ENGINE_SERVICE_ACCOUNT",
    "service-291679685305@gcp-sa-discoveryengine.iam.gserviceaccount.com",
)

_certs_cache: dict[str, str] = {}
_certs_cache_timestamp: float = 0.0

_oidc_certs_cache: dict[str, str] = {}
_oidc_certs_cache_timestamp: float = 0.0

_gateway_certs_cache: dict[str, str] = {}
_gateway_certs_cache_timestamp: float = 0.0


class AuthenticationError(Exception):
    """Raised when request identity authentication fails."""

    def __init__(self, message: str, status_code: int = status.HTTP_401_UNAUTHORIZED):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def is_production_environment() -> bool:
    """Determines whether the application is running in a production environment."""
    env = os.getenv("ENVIRONMENT") or os.getenv("ENV") or ""
    if env.lower() in ("prod", "production"):
        return True
    if os.getenv("K_SERVICE") or os.getenv("FUNCTION_NAME"):
        return True
    if os.getenv("GOOGLE_CLOUD_AGENT_ENGINE_ID") or os.getenv("AGENT_ENGINE_ID"):
        return True
    return False


def is_local_test_auth_enabled() -> bool:
    """Checks whether explicit local test authentication is enabled.

    Security Rule: Fails immediately and raises RuntimeError if enabled in a production environment.
    """
    flag = os.getenv("ENABLE_LOCAL_TEST_AUTH", "false").lower() in ("true", "1")
    if flag and is_production_environment():
        msg = "FATAL SECURITY CONFIGURATION: ENABLE_LOCAL_TEST_AUTH is strictly prohibited in production environments!"
        logger.critical(msg)
        raise RuntimeError(msg)
    return flag


def get_iap_audience() -> str | None:
    """Retrieves expected Google Cloud IAP audience string from environment."""
    return os.getenv("IAP_AUDIENCE")


def get_oidc_audience() -> str | None:
    """Retrieves expected Google OIDC audience string from environment."""
    return os.getenv("OIDC_AUDIENCE")


def get_gateway_audience() -> str:
    """Retrieves expected Gateway assertion audience string from environment."""
    return os.getenv("GATEWAY_AUDIENCE", "discovery-copilot-agent-runtime")


def get_allowed_email_domain() -> str | None:
    """Retrieves corporate domain requirement from environment (e.g. enterprise.com)."""
    return os.getenv("ALLOWED_EMAIL_DOMAIN")


def is_gemini_enterprise_direct_enabled() -> bool:
    """Checks whether direct Gemini Enterprise Agent Runtime integration is enabled.

    Defaults to True. Can be explicitly disabled by setting GEMINI_ENTERPRISE_DIRECT_ENABLED="false".
    """
    return os.getenv("GEMINI_ENTERPRISE_DIRECT_ENABLED", "true").lower() in ("true", "1")


def get_authorized_discovery_engine_service_account() -> str:
    """Returns the authorized Discovery Engine service account email."""
    return os.getenv(
        "AUTHORIZED_DISCOVERY_ENGINE_SERVICE_ACCOUNT",
        "service-291679685305@gcp-sa-discoveryengine.iam.gserviceaccount.com",
    )


def get_authorized_presenters() -> set[str]:
    """Retrieves allowed authorized party (azp) client IDs from environment if configured."""
    azp_env = os.getenv("AUTHORIZED_OIDC_AZP", "")
    if azp_env:
        return {item.strip() for item in azp_env.split(",") if item.strip()}
    return set()


def fetch_gateway_public_keys() -> dict[str, str]:
    """Retrieves Gateway public verification keys from environment or gateway endpoint."""
    global _gateway_certs_cache, _gateway_certs_cache_timestamp
    if _gateway_certs_cache:
        return _gateway_certs_cache

    keys_json = os.getenv("GATEWAY_PUBLIC_KEYS_JSON")
    if keys_json:
        try:
            certs = json.loads(keys_json)
            if isinstance(certs, dict) and certs:
                _gateway_certs_cache = certs
                return _gateway_certs_cache
        except Exception as exc:
            logger.error("Failed to parse GATEWAY_PUBLIC_KEYS_JSON: %s", exc)

    return _gateway_certs_cache


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
            headers={"User-Agent": "Enterprise-Discovery-Copilot/1.0"},
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


def fetch_oidc_public_keys(force_refresh: bool = False) -> dict[str, str]:
    """Retrieves Google OIDC public keys from Google.

    Caches public keys in memory with a 1-hour TTL.
    """
    global _oidc_certs_cache, _oidc_certs_cache_timestamp
    current_time = time.time()
    if not force_refresh and _oidc_certs_cache and (current_time - _oidc_certs_cache_timestamp < CERTS_CACHE_TTL_SECONDS):
        return _oidc_certs_cache

    try:
        req = urllib.request.Request(
            GOOGLE_OIDC_CERTS_URL,
            headers={"User-Agent": "Enterprise-Discovery-Copilot/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            if response.status != 200:
                raise AuthenticationError(f"Google OIDC certs endpoint returned HTTP {response.status}")
            data = response.read().decode("utf-8")
            certs = json.loads(data)
            if not isinstance(certs, dict) or not certs:
                raise AuthenticationError("Google OIDC certs endpoint returned empty or invalid keys")
            _oidc_certs_cache = certs
            _oidc_certs_cache_timestamp = current_time
            return _oidc_certs_cache
    except Exception as exc:
        logger.error("Failed to retrieve Google OIDC certs: %s", exc)
        if _oidc_certs_cache:
            return _oidc_certs_cache
        raise AuthenticationError(f"Failed to retrieve Google OIDC certs: {exc}") from exc


def clear_certs_cache() -> None:
    """Clears the public certificate cache (useful for testing rotation and retrieval errors)."""
    global _certs_cache, _certs_cache_timestamp, _oidc_certs_cache, _oidc_certs_cache_timestamp, _gateway_certs_cache, _gateway_certs_cache_timestamp
    _certs_cache = {}
    _certs_cache_timestamp = 0.0
    _oidc_certs_cache = {}
    _oidc_certs_cache_timestamp = 0.0
    _gateway_certs_cache = {}
    _gateway_certs_cache_timestamp = 0.0


def set_gateway_certs_override(certs: dict[str, str]) -> None:
    """Sets gateway certs in memory for testing or local development."""
    global _gateway_certs_cache
    _gateway_certs_cache = certs


def verify_identity_token(
    token: str,
    expected_audience: str | None = None,
    certs_override: dict[str, str] | None = None,
    token_type: str = "auto",
) -> dict[str, Any]:
    """Cryptographically validates a signed identity token (IAP JWT, Google OIDC, or Gateway assertion).

    Validates:
    - Signature against public certs matching token header 'kid'.
    - Expiration: 'exp' claim is strictly unexpired.
    - Issued-At: 'iat' claim is verified, not issued in the future.
    - Issuer: 'iss' must match expected issuer.
    - Audience: 'aud' must match expected_audience.
    - Algorithm: Must be an approved RS256 algorithm.
    - For OIDC: requires 'sub', 'email_verified' == True, 'azp' validation, and domain policy.
    - Extracts verified 'email'.
    """
    if not token or not isinstance(token, str):
        raise AuthenticationError("Missing or empty identity assertion token")

    try:
        header = google_jwt.decode_header(token)
    except Exception as exc:
        raise AuthenticationError(f"Malformed IAP JWT header: {exc}") from exc

    kid = header.get("kid")
    if not kid:
        raise AuthenticationError("Identity token header missing 'kid' key ID")

    alg = header.get("alg")

    # Unverified claims inspect to determine token type if auto
    try:
        unverified_claims = google_jwt.decode(token, verify=False)
    except Exception as exc:
        raise AuthenticationError(f"Malformed identity token payload: {exc}") from exc

    iss = unverified_claims.get("iss", "")
    is_iap = iss == IAP_ISSUER
    is_oidc = iss in GOOGLE_OIDC_ISSUERS
    is_gateway = iss == GATEWAY_ISSUER

    # If caller specifically requested token_type='iap', treat as IAP regardless of unverified iss
    if token_type == "iap" or is_iap:
        # Google IAP tokens are strictly signed with ECDSA P-256 (ES256) per Google documentation
        if alg != "ES256":
            raise AuthenticationError(f"Disallowed IAP signature algorithm: '{alg}' (only ES256 allowed)")
    else:
        # OIDC and Gateway assertions use RS256
        if alg != "RS256":
            raise AuthenticationError(f"Disallowed signature algorithm: '{alg}' (only RS256 allowed)")

    if token_type == "iap" or (token_type == "auto" and is_iap):
        certs_fetcher = fetch_iap_public_keys
        expected_iss_check = lambda i: i == IAP_ISSUER
        token_label = "IAP JWT"
        enforce_oidc_claims = False
    elif token_type == "gateway" or (token_type == "auto" and is_gateway):
        certs_fetcher = fetch_gateway_public_keys
        expected_iss_check = lambda i: i == GATEWAY_ISSUER
        token_label = "Gateway Assertion"
        enforce_oidc_claims = False
    elif token_type == "oidc" or (token_type == "auto" and is_oidc):
        certs_fetcher = fetch_oidc_public_keys
        expected_iss_check = lambda i: i in GOOGLE_OIDC_ISSUERS
        token_label = "Google OIDC"
        enforce_oidc_claims = True
    else:
        certs_fetcher = fetch_iap_public_keys
        expected_iss_check = lambda i: i == IAP_ISSUER
        token_label = "IAP JWT"
        enforce_oidc_claims = False

    if certs_override is not None:
        certs = certs_override
    else:
        certs = certs_fetcher()
        if kid not in certs:
            logger.info("Key ID '%s' not found in cached %s certs. Refreshing...", kid, token_label)
            if token_type == "gateway" or (token_type == "auto" and is_gateway):
                certs = fetch_gateway_public_keys()
            elif token_type == "oidc" or (token_type == "auto" and is_oidc):
                certs = fetch_oidc_public_keys(force_refresh=True)
            else:
                certs = fetch_iap_public_keys(force_refresh=True)
            if kid not in certs:
                raise AuthenticationError(f"Key ID '{kid}' not found in public keys")

    try:
        claims = google_jwt.decode(
            token,
            certs=certs,
            verify=True,
            audience=expected_audience,
        )
    except Exception as exc:
        logger.warning("%s cryptographic verification failed: %s", token_label, exc)
        raise AuthenticationError(f"Invalid or expired {token_label} assertion: {exc}") from exc

    issuer = claims.get("iss")
    if not expected_iss_check(issuer):
        logger.warning("Invalid token issuer '%s' for %s", issuer, token_label)
        if token_label == "IAP JWT":
            raise AuthenticationError(f"Invalid IAP token issuer: '{issuer}'")
        raise AuthenticationError(f"Invalid {token_label} token issuer: '{issuer}'")

    email = claims.get("email")
    if not email or not EMAIL_REGEX.match(str(email)):
        raise AuthenticationError(f"{token_label} token missing valid email claim: '{email}'")

    sub = claims.get("sub")
    if not sub or not str(sub).strip():
        raise AuthenticationError(f"{token_label} token missing valid 'sub' subject claim")

    # Enforce OIDC specific security claims
    if enforce_oidc_claims:
        # Check email_verified
        email_verified = claims.get("email_verified")
        if email_verified is not True and email_verified != "true":
            raise AuthenticationError(f"Google OIDC token email is not verified: email_verified={email_verified}")

        # Check authorized presenter (azp) if configured
        allowed_azp = get_authorized_presenters()
        if allowed_azp:
            azp = claims.get("azp")
            if not azp or azp not in allowed_azp:
                raise AuthenticationError(
                    f"Unauthorized party (azp): '{azp}' is not in authorized presenters: {allowed_azp}"
                )

    # Enforce corporate organization policy domain if configured
    allowed_domain = get_allowed_email_domain()
    if allowed_domain:
        domain = str(email).lower().split("@")[-1]
        if domain != allowed_domain.lower():
            raise AuthenticationError(
                f"Organization policy violation: principal domain '@{domain}' is not authorized (expected '@{allowed_domain}')"
            )

    # For Gateway assertions: enforce jti replay protection and separate session_id semantics
    if token_type == "gateway" or (token_type == "auto" and is_gateway):
        jti = claims.get("jti")
        if not jti or not str(jti).strip():
            raise AuthenticationError("Gateway Assertion missing required 'jti' (JWT ID) claim for replay protection")

        # Atomic Distributed JTI Consumption (Memorystore Redis)
        redis_host = os.getenv("REDIS_HOST")
        if redis_host:
            redis_port = int(os.getenv("REDIS_PORT", "6379"))
            cmd = f"*6\r\n$3\r\nSET\r\n${len(f'jti:dc:{jti}')}\r\njti:dc:{jti}\r\n$1\r\n1\r\n$2\r\nEX\r\n$3\r\n330\r\n$2\r\nNX\r\n".encode("utf-8")
            sock = None
            try:
                import socket
                sock = socket.create_connection((redis_host, redis_port), timeout=2.0)
                sock.sendall(cmd)
                resp = sock.recv(1024)
                if resp.startswith(b"$-1"):
                    logger.warning("Replay attack detected in Redis for gateway assertion jti='%s'", jti)
                    raise AuthenticationError("Replay attack detected: Gateway assertion 'jti' has already been used")
                elif not resp.startswith(b"+OK"):
                    raise ConnectionError(f"Unexpected Redis response: {resp!r}")
            except Exception as exc:
                if isinstance(exc, AuthenticationError):
                    raise
                logger.critical("Redis connectivity failure during JTI verification: %s", exc)
                raise AuthenticationError("Identity verification unavailable: Distributed replay store unreachable", status_code=503)
            finally:
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass
        else:
            # Fail closed in production if REDIS_HOST is missing
            if is_production_environment():
                logger.critical("REDIS_HOST is missing in production environment! Failing closed.")
                raise AuthenticationError("Identity verification unavailable: Distributed replay store is not configured", status_code=503)

            # Local fallback for unit tests
            current_time = time.time()
            _cleanup_expired_jtis(current_time)
            if jti in _seen_jti_cache:
                logger.warning("Replay attack detected for gateway assertion jti='%s'", jti)
                raise AuthenticationError("Replay attack detected: Gateway assertion 'jti' has already been used")

            exp_time = float(claims.get("exp", current_time + 300))
            _seen_jti_cache[jti] = exp_time

    return claims


def verify_iap_jwt(
    iap_jwt: str,
    expected_audience: str | None = None,
    certs_override: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Cryptographically validates Google Cloud IAP JWT token against Google public certs."""
    return verify_identity_token(
        iap_jwt,
        expected_audience=expected_audience,
        certs_override=certs_override,
        token_type="iap",
    )


def resolve_agent_runtime_direct_identity(
    request: Request,
    body_dict: dict[str, Any],
) -> str:
    """Resolves and validates end-user identity for direct Gemini Enterprise Agent Runtime calls.

    Trust boundary & security rules:
    1. Direct integration MUST be enabled (GEMINI_ENTERPRISE_DIRECT_ENABLED='true').
    2. Enforces caller service agent trust boundary: if a caller identity header is present
       (e.g., 'X-Caller-Service-Account', 'X-Authorized-Service-Agent', 'X-Forwarded-Service-Account'),
       it MUST match the authorized Discovery Engine service account.
    3. User identity is extracted from body 'input.user_id' or body 'user_id'.
    4. Missing, empty, or non-string user_id fails closed immediately (HTTP 401).
       Never falls back to a default administrator or unrestricted access.
    5. User identity must match valid email syntax (HTTP 400).
    6. Corporate domain enforcement: User email domain must match ALLOWED_EMAIL_DOMAIN
       (defaults to 'enterprise.com'). Mismatched domains fail closed (HTTP 403).
    """
    if not is_gemini_enterprise_direct_enabled():
        raise AuthenticationError(
            "Direct Gemini Enterprise Agent Runtime integration is disabled",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    # Trust boundary: Validate caller service account if caller identity header is provided
    caller_sa_header = (
        request.headers.get("X-Caller-Service-Account")
        or request.headers.get("X-Authorized-Service-Agent")
        or request.headers.get("X-Forwarded-Service-Account")
    )
    if caller_sa_header:
        caller_sa = caller_sa_header.strip().lower()
        authorized_sa = get_authorized_discovery_engine_service_account().strip().lower()
        if caller_sa != authorized_sa:
            logger.warning(
                "Direct Agent Runtime caller service account '%s' does not match authorized '%s'",
                caller_sa,
                authorized_sa,
            )
            raise AuthenticationError(
                f"Forbidden: Caller service account '{caller_sa}' is not authorized to invoke direct Agent Runtime path",
                status_code=status.HTTP_403_FORBIDDEN,
            )

    # Extract user_id from request body
    input_dict = body_dict.get("input") if isinstance(body_dict.get("input"), dict) else {}
    raw_user_id = input_dict.get("user_id") or body_dict.get("user_id")

    if not raw_user_id or not isinstance(raw_user_id, str) or not raw_user_id.strip():
        logger.warning("Direct Agent Runtime request missing user_id in payload: %s", body_dict.keys())
        raise AuthenticationError(
            "Authentication required: Missing user_id in Agent Runtime request",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    user_email = raw_user_id.strip().lower()

    if not EMAIL_REGEX.match(user_email):
        logger.warning("Direct Agent Runtime request invalid user_id format: '%s'", raw_user_id)
        raise AuthenticationError(
            f"Invalid user identity format: '{raw_user_id}' is not a valid email address",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Enforce corporate domain
    allowed_domain = (get_allowed_email_domain() or "enterprise.com").strip().lower()
    email_domain = user_email.split("@")[-1].lower()
    if email_domain != allowed_domain:
        logger.warning(
            "Direct Agent Runtime principal domain '@%s' rejected. Expected '@%s'",
            email_domain,
            allowed_domain,
        )
        raise AuthenticationError(
            f"Organization policy violation: principal domain '@{email_domain}' is not authorized (expected '@{allowed_domain}')",
            status_code=status.HTTP_403_FORBIDDEN,
        )

    return user_email


def extract_and_verify_principal(
    request: Request,
    body_dict: dict[str, Any] | None = None,
    certs_override: dict[str, str] | None = None,
    allow_agent_runtime_direct: bool = False,
) -> str:
    """Extracts and cryptographically validates caller identity from trusted request headers.

    Accepts ONLY cryptographically signed assertions from trusted HTTP headers:
    1. 'X-Enterprise-Gateway-Assertion': Short-lived assertion minted by trusted gateway
    2. 'X-Goog-IAP-JWT-Assertion': Signed JWT assertion minted by Google Cloud IAP
    3. 'Authorization: Bearer <token>': Google OIDC ID token or forwarded IAP token
    4. Isolated local test auth ('X-Enterprise-Test-Principal' header)
       strictly permitted ONLY when ENABLE_LOCAL_TEST_AUTH="true" AND NOT in production.
    5. Native Gemini Enterprise Agent Runtime Direct Integration (strictly enabled only
       when allow_agent_runtime_direct=True on internal Reasoning Engine routes).

    Security Rule: Request body fields (e.g. 'auth_token', 'iap_jwt', 'id_token') are NEVER
    trusted as proof of gateway identity or authentication.

    Validates anti-spoofing if caller supplied a user_id:
    If caller supplied user_id in URL or body, it MUST case-insensitively match the verified email.
    Fails closed with 401 Unauthorized or 403 Forbidden.
    """
    token = None
    expected_aud = None
    token_type = "auto"

    # 1. Check Gateway Header (First priority for gateway-forwarded calls)
    gateway_header = request.headers.get(GATEWAY_HEADER_NAME)
    if gateway_header:
        token = gateway_header.strip()
        expected_aud = get_gateway_audience()
        token_type = "gateway"

    # 2. Check Google Cloud IAP header
    if not token:
        iap_header = request.headers.get("X-Goog-IAP-JWT-Assertion")
        if iap_header:
            token = iap_header.strip()
            expected_aud = get_iap_audience()
            token_type = "iap"

    # 3. Check Authorization Bearer header
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            expected_aud = get_oidc_audience() or get_iap_audience()
            token_type = "auto"

    # If cryptographic token found in trusted headers, verify it
    if token:
        if token_type == "iap":
            claims = verify_iap_jwt(token, expected_audience=expected_aud, certs_override=certs_override)
        else:
            claims = verify_identity_token(token, expected_audience=expected_aud, certs_override=certs_override, token_type=token_type)
        verified_email = claims["email"].lower()
        _check_anti_spoofing(verified_email, request, body_dict)
        return verified_email

    # 4. Check Local Test Auth Header (Strict opt-in only, impossible in production)
    if is_local_test_auth_enabled():
        test_principal = request.headers.get("X-Enterprise-Test-Principal")
        if test_principal and isinstance(test_principal, str) and EMAIL_REGEX.match(test_principal.strip()):
            verified_email = test_principal.strip().lower()
            _check_anti_spoofing(verified_email, request, body_dict)
            return verified_email

    # 5. Check Native Gemini Enterprise Agent Runtime Direct Path
    if allow_agent_runtime_direct:
        return resolve_agent_runtime_direct_identity(request, body_dict or {})

    # 6. Fail closed: No valid cryptographic assertion provided in trusted headers
    raise AuthenticationError(
        "Authentication required: Missing valid X-Goog-IAP-JWT-Assertion."
    )


def _check_anti_spoofing(
    verified_email: str,
    request: Request,
    body_dict: dict[str, Any] | None = None,
) -> None:
    """Verifies that any caller-claimed user_id matches the cryptographically verified identity."""
    # Check URL path for ADK routes: /apps/app/users/{user_id}/...
    path_parts = request.url.path.strip("/").split("/")
    if len(path_parts) >= 4 and path_parts[0] == "apps" and path_parts[2] == "users":
        claimed_user_id = path_parts[3].lower()
        if claimed_user_id != verified_email:
            logger.error(
                "Identity mismatch detected in URL! Verified '%s' attempted to access user route for '%s'",
                verified_email,
                claimed_user_id,
            )
            raise AuthenticationError(
                f"Forbidden: Identity mismatch between verified token ('{verified_email}') and requested user ('{claimed_user_id}').",
                status_code=status.HTTP_403_FORBIDDEN,
            )

    # Check body input.user_id if present
    if body_dict:
        input_dict = body_dict.get("input") if isinstance(body_dict.get("input"), dict) else {}
        claimed_user_id = input_dict.get("user_id") or body_dict.get("user_id")
        if claimed_user_id and isinstance(claimed_user_id, str):
            claimed_user_id = claimed_user_id.strip().lower()
            if claimed_user_id != verified_email:
                logger.error(
                    "Identity mismatch detected in body! Verified '%s' attempted to invoke with user_id '%s'",
                    verified_email,
                    claimed_user_id,
                )
                raise AuthenticationError(
                    f"Forbidden: Identity mismatch between verified token ('{verified_email}') and claimed user_id ('{claimed_user_id}').",
                    status_code=status.HTTP_403_FORBIDDEN,
                )


def extract_authenticated_principal(request: Request) -> str:
    """Extracts and verifies the caller's authenticated identity from request headers.

    Fails closed if authentication is missing or invalid.
    """
    return extract_and_verify_principal(request)


class EnforceAuthenticatedPrincipalMiddleware(BaseHTTPMiddleware):
    """Middleware enforcing authenticated caller identity across all API and ADK routes."""

    # Paths exempt from authentication (strictly public metadata/docs routes only)
    EXEMPT_PATHS = frozenset({
        "/health",
        "/healthz",
        "/docs",
        "/openapi.json",
        "/redoc",
        "/.well-known/agent-card.json",
        "/.well-known/agent.json",
        "/a2a/app/.well-known/agent-card.json",
        "/a2a/app/.well-known/agent.json",
        "/a2a/discovery_copilot/.well-known/agent-card.json",
        "/a2a/discovery_copilot/.well-known/agent.json",
    })

    async def dispatch(self, request: Request, call_next) -> Response:
        # Check fail-safe: prohibit ENABLE_LOCAL_TEST_AUTH in production
        if is_production_environment() and os.getenv("ENABLE_LOCAL_TEST_AUTH", "false").lower() in ("true", "1"):
            logger.critical("FATAL: Startup/dispatch blocked: ENABLE_LOCAL_TEST_AUTH enabled in production!")
            return Response(
                content='{"detail": "Configuration error: ENABLE_LOCAL_TEST_AUTH is strictly prohibited in production."}',
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                media_type="application/json",
            )

        # Skip exempt public metadata paths
        if (
            request.url.path in self.EXEMPT_PATHS
            or request.url.path.endswith("/.well-known/agent-card.json")
            or request.url.path.endswith("/.well-known/agent.json")
        ):
            return await call_next(request)

        # For Reasoning Engine endpoints, authentication is handled in the route adapter
        # to enforce Gateway / IAP header validation before execution
        if request.url.path in ("/api/reasoning_engine", "/api/stream_reasoning_engine"):
            return await call_next(request)

        # 1. Authenticate Principal
        try:
            authenticated_email = extract_and_verify_principal(request)
        except AuthenticationError as exc:
            logger.warning("Rejecting unauthenticated request to '%s': %s", request.url.path, exc.message)
            return Response(
                content=f'{{"detail": "{exc.message}"}}',
                status_code=exc.status_code,
                media_type="application/json",
            )

        # 2. Bind verified identity to request state
        request.state.authenticated_user_email = authenticated_email

        return await call_next(request)

