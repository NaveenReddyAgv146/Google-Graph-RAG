"""Configuration settings for Authenticated Gateway Proxy."""

import os
from pydantic_settings import BaseSettings


class GatewaySettings(BaseSettings):
    # Gateway Environment & Service Info
    service_name: str = "enterprise-discovery-gateway"
    host: str = "0.0.0.0"
    port: int = int(os.getenv("PORT", "8080"))

    # Target Backend (Discovery Copilot private Reasoning Engine / Cloud Run endpoint)
    target_backend_url: str = os.getenv(
        "TARGET_BACKEND_URL",
        "https://asia-south1-aiplatform.googleapis.com/v1/projects/291679685305/locations/asia-south1/reasoningEngines/8796348108905644032",
    )
    # Direct endpoint path in container or stream URL
    target_stream_path: str = os.getenv("TARGET_STREAM_PATH", "/api/stream_reasoning_engine")
    target_sync_path: str = os.getenv("TARGET_SYNC_PATH", "/api/reasoning_engine")

    # OAuth 2.0 Client & Token Introspection Configuration
    tokeninfo_endpoint: str = os.getenv(
        "TOKENINFO_ENDPOINT", "https://oauth2.googleapis.com/tokeninfo"
    )
    oidc_discovery_endpoint: str = os.getenv(
        "OIDC_DISCOVERY_ENDPOINT", "https://accounts.google.com/.well-known/openid-configuration"
    )
    userinfo_endpoint: str = os.getenv(
        "USERINFO_ENDPOINT", "https://openidconnect.googleapis.com/v1/userinfo"
    )
    expected_oauth_client_id: str = os.getenv("EXPECTED_OAUTH_CLIENT_ID", "")
    authorized_presenters: str = os.getenv("AUTHORIZED_OAUTH_AZP", "")
    required_scopes: list[str] = [
        "https://www.googleapis.com/auth/cloud-platform",
        "openid",
        "email",
    ]
    # Google Workspace Hosted Domain Policy
    required_workspace_domain: str = os.getenv("REQUIRED_WORKSPACE_DOMAIN", "enterprise.com")

    # Gateway Token Minting Configuration
    gateway_issuer: str = os.getenv(
        "GATEWAY_ISSUER", "https://gateway.enterprise-graphrag.internal"
    )
    gateway_audience: str = os.getenv(
        "GATEWAY_AUDIENCE", "discovery-copilot-agent-runtime"
    )
    assertion_ttl_seconds: int = int(os.getenv("ASSERTION_TTL_SECONDS", "300"))  # 5 min

    # Active Signing Key ID
    active_key_id: str = os.getenv("GATEWAY_ACTIVE_KID", "gateway-key-2026-v1")

    # Security & Rate Limiting
    rate_limit_per_minute: int = int(os.getenv("RATE_LIMIT_PER_MINUTE", "60"))
    request_timeout_seconds: float = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "120.0"))


settings = GatewaySettings()
