"""Google OAuth 2.0 access token introspection and policy validation service."""

import logging
import re
from typing import Any
import httpx

from gateway.app.config import settings

logger = logging.getLogger("gateway.introspector")

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")


class TokenIntrospectionError(Exception):
    """Raised when access token introspection or policy validation fails."""

    def __init__(self, message: str, status_code: int = 401):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class TokenIntrospector:
    """Introspects Google OAuth 2.0 access tokens and validates enterprise security policies."""

    def __init__(self, http_client: httpx.AsyncClient | None = None):
        self._http_client = http_client
        self._cached_userinfo_endpoint: str | None = None

    async def get_userinfo_endpoint(self, client: httpx.AsyncClient | None = None) -> str:
        """Resolves UserInfo endpoint via Google OIDC discovery document with caching."""
        if self._cached_userinfo_endpoint:
            return self._cached_userinfo_endpoint

        discovery_url = settings.oidc_discovery_endpoint
        try:
            if client is not None:
                resp = await client.get(discovery_url, timeout=5.0)
                if resp.status_code == 200:
                    ep = resp.json().get("userinfo_endpoint")
                    if ep:
                        self._cached_userinfo_endpoint = str(ep)
                        return self._cached_userinfo_endpoint
            else:
                async with httpx.AsyncClient() as c:
                    resp = await c.get(discovery_url, timeout=5.0)
                    if resp.status_code == 200:
                        ep = resp.json().get("userinfo_endpoint")
                        if ep:
                            self._cached_userinfo_endpoint = str(ep)
                            return self._cached_userinfo_endpoint
        except Exception as exc:
            logger.warning(
                "OIDC discovery lookup to %s failed (%s); using fallback endpoint %s",
                discovery_url,
                exc,
                settings.userinfo_endpoint,
            )

        self._cached_userinfo_endpoint = settings.userinfo_endpoint
        return self._cached_userinfo_endpoint

    async def introspect(self, access_token: str) -> dict[str, Any]:
        """Calls Google tokeninfo and userinfo endpoints to authoritatively validate access token and identity."""
        if not access_token or not isinstance(access_token, str):
            raise TokenIntrospectionError("Missing or invalid access token")

        # Never log raw bearer access tokens
        logger.info("Initiating Google token introspection and userinfo lookup...")

        tokeninfo_url = f"{settings.tokeninfo_endpoint}?access_token={access_token}"
        client_to_use = self._http_client
        userinfo_url = await self.get_userinfo_endpoint(client=client_to_use)
        userinfo_headers = {"Authorization": f"Bearer {access_token}"}

        try:
            client_to_use = self._http_client
            if client_to_use is not None:
                # 1. Fetch tokeninfo
                token_resp = await client_to_use.get(tokeninfo_url, timeout=10.0)
                if token_resp.status_code != 200:
                    logger.warning("Token introspection failed with HTTP %s", token_resp.status_code)
                    try:
                        err_detail = token_resp.json().get("error_description") or "Invalid or expired access token"
                    except Exception:
                        err_detail = "Invalid or expired access token"
                    raise TokenIntrospectionError(f"Google token introspection rejected token: {err_detail}", status_code=401)
                try:
                    token_data = token_resp.json()
                except Exception:
                    raise TokenIntrospectionError("Malformed JSON response from Google tokeninfo endpoint", status_code=502)

                # 2. Fetch userinfo
                user_resp = await client_to_use.get(userinfo_url, headers=userinfo_headers, timeout=10.0)
                if user_resp.status_code != 200:
                    logger.warning("Userinfo lookup failed with HTTP %s", user_resp.status_code)
                    raise TokenIntrospectionError("Failed to retrieve user profile from Google UserInfo", status_code=401)
                try:
                    user_data = user_resp.json()
                except Exception:
                    raise TokenIntrospectionError("Malformed JSON response from Google UserInfo endpoint", status_code=502)
            else:
                async with httpx.AsyncClient() as client:
                    token_resp = await client.get(tokeninfo_url, timeout=10.0)
                    if token_resp.status_code != 200:
                        logger.warning("Token introspection failed with HTTP %s", token_resp.status_code)
                        try:
                            err_detail = token_resp.json().get("error_description") or "Invalid or expired access token"
                        except Exception:
                            err_detail = "Invalid or expired access token"
                        raise TokenIntrospectionError(f"Google token introspection rejected token: {err_detail}", status_code=401)
                    try:
                        token_data = token_resp.json()
                    except Exception:
                        raise TokenIntrospectionError("Malformed JSON response from Google tokeninfo endpoint", status_code=502)

                    user_resp = await client.get(userinfo_url, headers=userinfo_headers, timeout=10.0)
                    if user_resp.status_code != 200:
                        logger.warning("Userinfo lookup failed with HTTP %s", user_resp.status_code)
                        raise TokenIntrospectionError("Failed to retrieve user profile from Google UserInfo", status_code=401)
                    try:
                        user_data = user_resp.json()
                    except Exception:
                        raise TokenIntrospectionError("Malformed JSON response from Google UserInfo endpoint", status_code=502)

            return self.validate_token_and_user_data(token_data, user_data)

        except httpx.RequestError as exc:
            logger.error("Network error during token introspection/userinfo: %s", exc)
            raise TokenIntrospectionError("Failed to reach identity provider for token verification", status_code=503)

    def validate_token_and_user_data(
        self,
        token_data: dict[str, Any],
        user_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Validates tokeninfo metadata (issued_to, audience, scope, expires_in) alongside userinfo (id, email, verified_email, hd)."""
        if not isinstance(token_data, dict) or not isinstance(user_data, dict):
            raise TokenIntrospectionError("Malformed response payload from identity provider", status_code=502)
        # 1. Check expiration from tokeninfo
        expires_in = token_data.get("expires_in")
        try:
            if expires_in is not None and int(expires_in) <= 0:
                raise TokenIntrospectionError("Token has expired", status_code=401)
        except ValueError:
            raise TokenIntrospectionError("Invalid expires_in claim", status_code=401)

        # 2. Check scopes from tokeninfo (strictly verifying openid, email, and cloud-platform)
        token_scopes = set(token_data.get("scope", "").split())
        for req_scope in settings.required_scopes:
            has_scope = req_scope in token_scopes
            if not has_scope and req_scope in ("email", "https://www.googleapis.com/auth/userinfo.email"):
                has_scope = "email" in token_scopes or "https://www.googleapis.com/auth/userinfo.email" in token_scopes
            if not has_scope:
                logger.warning("Missing required scope: '%s' (present: %s)", req_scope, token_scopes)
                raise TokenIntrospectionError(
                    f"Insufficient permissions: Token is missing required scope '{req_scope}'",
                    status_code=403,
                )

        # 3. Check client identity: issued_to / audience in tokeninfo
        token_client = token_data.get("issued_to") or token_data.get("audience") or token_data.get("aud")
        if settings.expected_oauth_client_id:
            if token_client != settings.expected_oauth_client_id:
                logger.warning("Token client mismatch: got '%s', expected '%s'", token_client, settings.expected_oauth_client_id)
                raise TokenIntrospectionError("Unauthorized client: Token was not issued to expected client", status_code=403)

        # 4. Check authorized presenters if configured
        if settings.authorized_presenters:
            allowed_azps = {a.strip() for a in settings.authorized_presenters.split(",") if a.strip()}
            token_azp = token_data.get("issued_to") or token_data.get("azp")
            if not token_azp or token_azp not in allowed_azps:
                logger.warning("Unauthorized presenter: got '%s', expected one of %s", token_azp, allowed_azps)
                raise TokenIntrospectionError(f"Unauthorized party: Presenter '{token_azp}' is not authorized", status_code=403)

        # 5. Support documented 'sub' or legacy 'user_id' in TokenInfo schemas and compare with UserInfo 'sub'
        token_sub = token_data.get("sub")
        token_uid = token_data.get("user_id")

        token_identities: list[str] = []
        if token_sub and str(token_sub).strip():
            token_identities.append(str(token_sub).strip())
        if token_uid and str(token_uid).strip():
            token_identities.append(str(token_uid).strip())

        if not token_identities:
            logger.warning("TokenInfo response missing both 'sub' and legacy 'user_id' identity fields")
            raise TokenIntrospectionError(
                "TokenInfo response missing immutable user identity ('sub' or 'user_id')",
                status_code=401,
            )

        # If both sub and user_id are present in TokenInfo, they must be consistent
        if len(token_identities) == 2 and token_identities[0] != token_identities[1]:
            logger.error("Inconsistent identities within TokenInfo: sub='%s' vs user_id='%s'", token_sub, token_uid)
            raise TokenIntrospectionError(
                "Inconsistent identity claims in token introspection response",
                status_code=403,
            )

        token_identity = token_identities[0]

        # Extract OIDC UserInfo 'sub'
        userinfo_sub = user_data.get("sub") or user_data.get("id")
        if not userinfo_sub or not str(userinfo_sub).strip():
            logger.warning("OIDC UserInfo response missing required 'sub' claim")
            raise TokenIntrospectionError("OIDC UserInfo response missing required 'sub' claim", status_code=401)

        userinfo_identity = str(userinfo_sub).strip()

        # Cross-verify TokenInfo identity against UserInfo sub
        if token_identity != userinfo_identity:
            logger.error(
                "Identity mismatch detected between UserInfo sub ('%s') and TokenInfo identity ('%s')",
                userinfo_identity,
                token_identity,
            )
            raise TokenIntrospectionError(
                "Cryptographic identity mismatch between token metadata and user profile",
                status_code=403,
            )

        user_id = userinfo_identity

        # 6. Extract and validate email from userinfo
        email = user_data.get("email") or token_data.get("email")
        if not email or not EMAIL_REGEX.match(str(email)):
            raise TokenIntrospectionError("User profile does not contain a valid email address", status_code=401)

        # 7. Check email_verified (OIDC schema: email_verified, fallback verified_email)
        verified = user_data.get("email_verified")
        if verified is None:
            verified = user_data.get("verified_email") or token_data.get("email_verified") or token_data.get("verified_email")
        if verified not in (True, "true", "True"):
            raise TokenIntrospectionError(f"Email '{email}' is not verified by Google (email_verified=False)", status_code=403)

        # 8. Check Google Workspace Hosted Domain (hd) Policy
        if settings.required_workspace_domain:
            expected_domain = settings.required_workspace_domain.lower()
            domain_from_email = email.split("@")[-1].lower()
            if domain_from_email != expected_domain:
                raise TokenIntrospectionError(
                    f"Organization policy violation: User domain '@{domain_from_email}' is not authorized",
                    status_code=403,
                )

            hd = user_data.get("hd") or token_data.get("hd")
            if not hd:
                raise TokenIntrospectionError(
                    f"Organization policy violation: Missing required Workspace 'hd' claim (must be '@{expected_domain}')",
                    status_code=403,
                )
            if str(hd).lower() != expected_domain:
                raise TokenIntrospectionError(
                    f"Organization policy violation: Workspace hosted domain '{hd}' does not match '{expected_domain}'",
                    status_code=403,
                )

        return {
            "sub": str(user_id),
            "email": str(email).lower(),
            "hd": user_data.get("hd") or token_data.get("hd"),
            "scopes": list(token_scopes),
            "client_id": token_client,
            "expires_in": int(expires_in) if expires_in else 3600,
        }

    # Backward compatibility helper for testing
    def validate_token_claims(self, token_data: dict[str, Any]) -> dict[str, Any]:
        """Validates tokeninfo structure with fallback user mock (used in direct unit tests)."""
        user_mock = {
            "sub": token_data.get("user_id") or token_data.get("sub", "mock-sub"),
            "email": token_data.get("email", "mock@enterprise.com"),
            "email_verified": token_data.get("email_verified", token_data.get("verified_email", True)),
            "hd": token_data.get("hd", settings.required_workspace_domain),
        }
        return self.validate_token_and_user_data(token_data, user_mock)
