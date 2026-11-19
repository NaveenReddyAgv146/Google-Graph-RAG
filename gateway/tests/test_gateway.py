"""Integration and unit tests for Authenticated Gateway Proxy (Step 4B.2)."""

import json
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi.testclient import TestClient
import httpx

from gateway.app.config import settings
from gateway.app.key_manager import key_manager
from gateway.app.main import app
from gateway.app.token_introspector import TokenIntrospectionError, TokenIntrospector


@pytest.fixture
def client():
    return TestClient(app)


def _make_valid_google_tokeninfo(
    issued_to: str = "",
    audience: str = "",
    user_id: str = "110169484474386276334",
    scopes: str = "https://www.googleapis.com/auth/cloud-platform openid email",
    expires_in: int = 3500,
):
    """Realistic Google OAuth 2.0 access-token tokeninfo schema."""
    return {
        "issued_to": issued_to or settings.expected_oauth_client_id or "client-123.apps.googleusercontent.com",
        "audience": audience or settings.expected_oauth_client_id or "client-123.apps.googleusercontent.com",
        "user_id": user_id,
        "scope": scopes,
        "expires_in": str(expires_in),
        "access_type": "offline",
    }


def _make_valid_google_userinfo(
    sub: str = "110169484474386276334",
    email: str = "architect@enterprise.com",
    email_verified: bool = True,
    hd: str = "enterprise.com",
):
    """Realistic Google OIDC UserInfo response schema (https://openidconnect.googleapis.com/v1/userinfo)."""
    return {
        "sub": sub,
        "email": email,
        "email_verified": email_verified,
        "hd": hd,
    }


# ============================================================================
# 1. HEALTH & JWKS ENDPOINTS
# ============================================================================


def test_gateway_health(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "HEALTHY"


def test_gateway_jwks_publishes_keys(client):
    res = client.get("/.well-known/jwks.json")
    assert res.status_code == 200
    keys = res.json().get("keys", {})
    assert len(keys) >= 1
    assert key_manager.get_active_kid() in keys


# ============================================================================
# 2. TOKEN INTROSPECTION & POLICY TESTS (STEP 4B.2)
# ============================================================================


@pytest.mark.asyncio
async def test_token_introspection_missing_token():
    introspector = TokenIntrospector()
    with pytest.raises(TokenIntrospectionError, match="Missing or invalid access token"):
        await introspector.introspect("")


def test_token_introspection_expired():
    introspector = TokenIntrospector()
    token_data = _make_valid_google_tokeninfo(expires_in=-10)
    user_data = _make_valid_google_userinfo()
    with pytest.raises(TokenIntrospectionError, match="Token has expired"):
        introspector.validate_token_and_user_data(token_data, user_data)


def test_token_introspection_unverified_email():
    introspector = TokenIntrospector()
    token_data = _make_valid_google_tokeninfo()
    user_data = _make_valid_google_userinfo(email_verified=False)
    with pytest.raises(TokenIntrospectionError, match="not verified by Google"):
        introspector.validate_token_and_user_data(token_data, user_data)


def test_token_introspection_missing_cloud_platform_scope():
    introspector = TokenIntrospector()
    token_data = _make_valid_google_tokeninfo(scopes="openid email")
    user_data = _make_valid_google_userinfo()
    with pytest.raises(TokenIntrospectionError, match="missing required scope 'https://www.googleapis.com/auth/cloud-platform'"):
        introspector.validate_token_and_user_data(token_data, user_data)


def test_token_introspection_missing_openid_scope():
    introspector = TokenIntrospector()
    token_data = _make_valid_google_tokeninfo(scopes="https://www.googleapis.com/auth/cloud-platform email")
    user_data = _make_valid_google_userinfo()
    with pytest.raises(TokenIntrospectionError, match="missing required scope 'openid'"):
        introspector.validate_token_and_user_data(token_data, user_data)


def test_token_introspection_missing_email_scope():
    introspector = TokenIntrospector()
    token_data = _make_valid_google_tokeninfo(scopes="https://www.googleapis.com/auth/cloud-platform")
    user_data = _make_valid_google_userinfo()
    with pytest.raises(TokenIntrospectionError, match="missing required scope"):
        introspector.validate_token_and_user_data(token_data, user_data)


def test_token_introspection_wrong_workspace_domain():
    introspector = TokenIntrospector()
    token_data = _make_valid_google_tokeninfo()
    user_data = _make_valid_google_userinfo(email="attacker@external-domain.com", hd="external-domain.com")
    with pytest.raises(TokenIntrospectionError, match="Organization policy violation"):
        introspector.validate_token_and_user_data(token_data, user_data)


def test_token_introspection_missing_workspace_hd():
    introspector = TokenIntrospector()
    token_data = _make_valid_google_tokeninfo()
    user_data = _make_valid_google_userinfo(hd="")
    with pytest.raises(TokenIntrospectionError, match="Missing required Workspace 'hd' claim"):
        introspector.validate_token_and_user_data(token_data, user_data)


def test_token_introspection_missing_userinfo_sub():
    introspector = TokenIntrospector()
    token_data = _make_valid_google_tokeninfo()
    user_data = _make_valid_google_userinfo(sub="")
    with pytest.raises(TokenIntrospectionError, match="OIDC UserInfo response missing required 'sub' claim"):
        introspector.validate_token_and_user_data(token_data, user_data)


def test_token_introspection_supports_documented_sub_schema():
    """Google access token TokenInfo schema providing 'sub' instead of 'user_id'."""
    introspector = TokenIntrospector()
    token_data = {
        "issued_to": "client-123.apps.googleusercontent.com",
        "audience": "client-123.apps.googleusercontent.com",
        "sub": "110169484474386276334",
        "scope": "https://www.googleapis.com/auth/cloud-platform openid email",
        "expires_in": "3500",
    }
    user_data = _make_valid_google_userinfo(sub="110169484474386276334")
    res = introspector.validate_token_and_user_data(token_data, user_data)
    assert res["sub"] == "110169484474386276334"


def test_token_introspection_supports_legacy_user_id_schema():
    """Legacy Google TokenInfo schema providing 'user_id' instead of 'sub'."""
    introspector = TokenIntrospector()
    token_data = {
        "issued_to": "client-123.apps.googleusercontent.com",
        "audience": "client-123.apps.googleusercontent.com",
        "user_id": "110169484474386276334",
        "scope": "https://www.googleapis.com/auth/cloud-platform openid email",
        "expires_in": "3500",
    }
    user_data = _make_valid_google_userinfo(sub="110169484474386276334")
    res = introspector.validate_token_and_user_data(token_data, user_data)
    assert res["sub"] == "110169484474386276334"


def test_token_introspection_supports_both_sub_and_user_id_matching():
    """Google TokenInfo schema providing both consistent 'sub' and 'user_id'."""
    introspector = TokenIntrospector()
    token_data = {
        "issued_to": "client-123.apps.googleusercontent.com",
        "audience": "client-123.apps.googleusercontent.com",
        "sub": "110169484474386276334",
        "user_id": "110169484474386276334",
        "scope": "https://www.googleapis.com/auth/cloud-platform openid email",
        "expires_in": "3500",
    }
    user_data = _make_valid_google_userinfo(sub="110169484474386276334")
    res = introspector.validate_token_and_user_data(token_data, user_data)
    assert res["sub"] == "110169484474386276334"


def test_token_introspection_inconsistent_sub_and_user_id_rejected():
    """TokenInfo response with conflicting 'sub' and 'user_id' must be rejected."""
    introspector = TokenIntrospector()
    token_data = {
        "issued_to": "client-123.apps.googleusercontent.com",
        "sub": "110169484474386276334",
        "user_id": "999999999999999999999",
        "scope": "https://www.googleapis.com/auth/cloud-platform openid email",
        "expires_in": "3500",
    }
    user_data = _make_valid_google_userinfo(sub="110169484474386276334")
    with pytest.raises(TokenIntrospectionError, match="Inconsistent identity claims in token introspection response") as exc_info:
        introspector.validate_token_and_user_data(token_data, user_data)
    assert exc_info.value.status_code == 403


def test_token_introspection_missing_both_sub_and_user_id_rejected():
    """TokenInfo response missing any identity field must be rejected."""
    introspector = TokenIntrospector()
    token_data = {
        "issued_to": "client-123.apps.googleusercontent.com",
        "scope": "https://www.googleapis.com/auth/cloud-platform openid email",
        "expires_in": "3500",
    }
    user_data = _make_valid_google_userinfo()
    with pytest.raises(TokenIntrospectionError, match="TokenInfo response missing immutable user identity"):
        introspector.validate_token_and_user_data(token_data, user_data)


def test_token_introspection_email_scope_alias_accepted():
    """Verify standard scope alias 'https://www.googleapis.com/auth/userinfo.email' satisfies 'email'."""
    introspector = TokenIntrospector()
    token_data = _make_valid_google_tokeninfo(
        scopes="https://www.googleapis.com/auth/cloud-platform openid https://www.googleapis.com/auth/userinfo.email"
    )
    user_data = _make_valid_google_userinfo()
    res = introspector.validate_token_and_user_data(token_data, user_data)
    assert res["sub"] == "110169484474386276334"


@pytest.mark.asyncio
async def test_oidc_discovery_resolves_userinfo_endpoint():
    """Verify UserInfo endpoint is dynamically resolved from Google OIDC discovery document."""
    mock_discovery_resp = MagicMock()
    mock_discovery_resp.status_code = 200
    mock_discovery_resp.json.return_value = {
        "issuer": "https://accounts.google.com",
        "userinfo_endpoint": "https://openidconnect.googleapis.com/v1/userinfo",
    }

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get = AsyncMock(return_value=mock_discovery_resp)

    introspector = TokenIntrospector(http_client=mock_client)
    resolved_endpoint = await introspector.get_userinfo_endpoint(client=mock_client)
    assert resolved_endpoint == "https://openidconnect.googleapis.com/v1/userinfo"
    mock_client.get.assert_called_with("https://accounts.google.com/.well-known/openid-configuration", timeout=5.0)


def test_mismatched_tokeninfo_user_id_and_userinfo_sub_rejected():
    """Identity mismatch between tokeninfo user_id and userinfo sub must be rejected."""
    introspector = TokenIntrospector()
    token_data = _make_valid_google_tokeninfo(user_id="110169484474386276334")
    user_data = _make_valid_google_userinfo(sub="999999999999999999999")
    with pytest.raises(TokenIntrospectionError, match="Cryptographic identity mismatch between token metadata and user profile") as exc_info:
        introspector.validate_token_and_user_data(token_data, user_data)
    assert exc_info.value.status_code == 403


def test_token_introspection_unauthorized_azp():
    with patch.object(settings, "authorized_presenters", "authorized-app-client-id"):
        introspector = TokenIntrospector()
        token_data = _make_valid_google_tokeninfo(issued_to="unauthorized-malicious-client")
        user_data = _make_valid_google_userinfo()
        with pytest.raises(TokenIntrospectionError, match="Unauthorized party"):
            introspector.validate_token_and_user_data(token_data, user_data)


def test_token_introspection_wrong_audience():
    with patch.object(settings, "expected_oauth_client_id", "expected-aud-123"):
        introspector = TokenIntrospector()
        token_data = _make_valid_google_tokeninfo(audience="wrong-aud-999", issued_to="wrong-aud-999")
        user_data = _make_valid_google_userinfo()
        with pytest.raises(TokenIntrospectionError, match="Unauthorized client"):
            introspector.validate_token_and_user_data(token_data, user_data)


def test_malformed_tokeninfo_or_userinfo_payload_rejected():
    introspector = TokenIntrospector()
    with pytest.raises(TokenIntrospectionError, match="Malformed response payload from identity provider"):
        introspector.validate_token_and_user_data("not-a-dict", _make_valid_google_userinfo())  # type: ignore

    with pytest.raises(TokenIntrospectionError, match="Malformed response payload from identity provider"):
        introspector.validate_token_and_user_data(_make_valid_google_tokeninfo(), "not-a-dict")  # type: ignore


@pytest.mark.asyncio
async def test_malformed_json_response_from_endpoints():
    mock_resp_bad_json = MagicMock()
    mock_resp_bad_json.status_code = 200
    mock_resp_bad_json.json.side_effect = ValueError("Invalid JSON")

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get = AsyncMock(return_value=mock_resp_bad_json)

    introspector = TokenIntrospector(http_client=mock_client)
    with pytest.raises(TokenIntrospectionError, match="Malformed JSON response from Google tokeninfo endpoint"):
        await introspector.introspect("ya29.test_token")


# ============================================================================
# 3. END-TO-END GATEWAY PROXY TESTS (MOCK BACKEND & INTROSPECTION)
# ============================================================================


def test_missing_authorization_header_rejected_401(client):
    res = client.post("/v1/message:stream", json={"message": "hello"})
    assert res.status_code == 401
    assert "Missing or malformed Authorization Bearer header" in res.json()["detail"]


@pytest.mark.asyncio
async def test_valid_user_request_forwards_assertion():
    mock_introspector = MagicMock()
    mock_introspector.introspect = AsyncMock(
        return_value={
            "sub": "110169484474386276334",
            "email": "architect@enterprise.com",
            "hd": "enterprise.com",
            "scopes": settings.required_scopes,
            "expires_in": 3600,
        }
    )

    mock_backend_client = MagicMock()
    async def mock_stream(*args, **kwargs):
        yield b'{"chunk": 1}\n'
        yield b'{"chunk": 2}\n'

    mock_backend_client.forward_streaming_request = mock_stream

    app.state.introspector = mock_introspector
    app.state.backend_client = mock_backend_client

    test_c = TestClient(app)
    response = test_c.post(
        "/v1/message:stream",
        headers={"Authorization": "Bearer ya29.mock_valid_token"},
        json={"message": "Discover architecture"},
    )
    assert response.status_code == 200
    content = response.text
    assert '{"chunk": 1}' in content
    assert '{"chunk": 2}' in content


@pytest.mark.asyncio
async def test_key_rotation_updates_kid_and_verification():
    """Verify KeyManager supports key rotation without restarting gateway."""
    old_kid = key_manager.get_active_kid()
    new_kid = "gateway-rotated-key-2"
    key_manager.generate_and_register_key(new_kid)
    key_manager.set_active_key(new_kid)

    assert key_manager.get_active_kid() == new_kid

    # Public keys include both old and new
    pub_keys = key_manager.get_public_keys()
    assert old_kid in pub_keys
    assert new_kid in pub_keys

    # Mint assertion and verify it carries new_kid
    from gateway.app.assertion_minter import AssertionMinter
    token = AssertionMinter.mint_assertion(
        user_email="architect@enterprise.com",
        user_sub="110169484474386276334",
    )
    from google.auth import jwt as google_jwt
    header = google_jwt.decode_header(token)
    assert header["kid"] == new_kid
