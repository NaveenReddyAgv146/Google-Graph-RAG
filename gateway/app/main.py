"""FastAPI Application for Authenticated Gateway Proxy."""

import collections
import hashlib
import logging
import time
from typing import Any
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse

from gateway.app.assertion_minter import AssertionMinter
from gateway.app.backend_client import BackendClient
from gateway.app.config import settings
from gateway.app.key_manager import key_manager
from gateway.app.redis_client import RedisReplayError, RedisUnavailableError, redis_store
from gateway.app.token_introspector import TokenIntrospectionError, TokenIntrospector

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gateway.app")

app = FastAPI(
    title="enterprise-discovery-gateway",
    description="Authenticated Gateway Proxy bridging Gemini Enterprise OAuth to Discovery Copilot",
    version="0.1.0",
)

# In-memory sliding-window rate limiter: client_ip -> list of timestamps
_rate_limit_records: dict[str, list[float]] = collections.defaultdict(list)


def get_client_ip(request: Request) -> str:
    """Extracts client IP safely without trusting arbitrary spoofed X-Forwarded-For headers.

    Security Rule: Behind Google Cloud Load Balancer / Cloud Run, the trusted client IP is
    the immediate connecting peer (request.client.host) or the first (leftmost) IP in X-Forwarded-For
    only when verified, but never arbitrary uncontrolled client inputs.
    """
    # In Cloud Run / GFE, request.client.host represents the connecting client or downstream proxy
    peer_ip = request.client.host if request.client else "unknown"
    return peer_ip


def check_rate_limit(client_ip: str) -> None:
    """Enforces sliding window rate limit per client IP."""
    now = time.time()
    window_start = now - 60.0
    records = _rate_limit_records[client_ip]

    # Evict timestamps older than 60 seconds
    valid_records = [t for t in records if t > window_start]
    if len(valid_records) >= settings.rate_limit_per_minute:
        logger.warning("Rate limit exceeded for client IP '%s'", client_ip)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please retry after some time.",
        )

    valid_records.append(now)
    _rate_limit_records[client_ip] = valid_records


def extract_bearer_token(request: Request) -> str:
    """Extracts bearer token strictly from HTTP Authorization header."""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required: Missing or malformed Authorization Bearer header.",
        )
    return auth_header[7:].strip()


# Public / Health endpoints
@app.get("/health")
@app.get("/healthz")
async def health():
    return {"status": "HEALTHY", "service": settings.service_name}


@app.get("/.well-known/jwks.json")
async def jwks():
    """Publishes public verification keys for Agent Runtime / Copilot verification."""
    return {"keys": key_manager.get_public_keys()}


# A2A Message Streaming Endpoint (Gemini Enterprise A2A contract)
@app.post("/v1/message:stream")
@app.post("/a2a/app/message:stream")
async def stream_a2a_message(request: Request) -> StreamingResponse:
    client_ip = get_client_ip(request)
    check_rate_limit(client_ip)

    bearer_token = extract_bearer_token(request)
    body = await request.json()

    # Inbound Replay Protection (Fail-Closed)
    token_fingerprint = hashlib.sha256(bearer_token.encode("utf-8")).hexdigest()
    try:
        await redis_store.consume_jti(f"jti:gw:{token_fingerprint}")
    except RedisReplayError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token replay detected: Access token presentation was already consumed.",
        )
    except RedisUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        )

    # 1. Introspect Google Access Token and fetch UserInfo
    introspector = getattr(request.app.state, "introspector", None) or TokenIntrospector()
    try:
        user_info = await introspector.introspect(bearer_token)
    except TokenIntrospectionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    # 2. Extract Session ID if present in body, or generate one
    session_id = body.get("session_id") or (body.get("input", {}).get("session_id") if isinstance(body.get("input"), dict) else None)

    # 3. Mint short-lived gateway assertion with verified sub and email
    gateway_assertion = AssertionMinter.mint_assertion(
        user_email=user_info["email"],
        user_sub=user_info["sub"],
        session_id=session_id,
    )

    # 4. Forward to private backend
    backend_client = getattr(request.app.state, "backend_client", None) or BackendClient()
    target_url = f"{settings.target_backend_url.rstrip('/')}{settings.target_stream_path}"

    async def stream_generator():
        async for chunk in backend_client.forward_streaming_request(
            endpoint_url=target_url,
            body=body,
            gateway_assertion=gateway_assertion,
        ):
            yield chunk

    return StreamingResponse(stream_generator(), media_type="application/json")


# Reasoning Engine direct dispatch endpoint
@app.post("/api/stream_reasoning_engine")
async def stream_reasoning_engine(request: Request) -> StreamingResponse:
    return await stream_a2a_message(request)


@app.post("/api/reasoning_engine")
async def query_reasoning_engine(request: Request) -> JSONResponse:
    client_ip = get_client_ip(request)
    check_rate_limit(client_ip)

    bearer_token = extract_bearer_token(request)
    body = await request.json()

    # Inbound Replay Protection (Fail-Closed)
    token_fingerprint = hashlib.sha256(bearer_token.encode("utf-8")).hexdigest()
    try:
        await redis_store.consume_jti(f"jti:gw:{token_fingerprint}")
    except RedisReplayError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token replay detected: Access token presentation was already consumed.",
        )
    except RedisUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        )

    # 1. Introspect Google Access Token
    introspector = getattr(request.app.state, "introspector", None) or TokenIntrospector()
    try:
        user_info = await introspector.introspect(bearer_token)
    except TokenIntrospectionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    # 2. Mint short-lived gateway assertion
    gateway_assertion = AssertionMinter.mint_assertion(
        user_email=user_info["email"],
        user_sub=user_info["sub"],
    )

    # 3. Forward sync request to private backend
    backend_client = getattr(request.app.state, "backend_client", None) or BackendClient()
    target_url = f"{settings.target_backend_url.rstrip('/')}{settings.target_sync_path}"

    status_code, response_data = await backend_client.forward_sync_request(
        endpoint_url=target_url,
        body=body,
        gateway_assertion=gateway_assertion,
    )
    return JSONResponse(status_code=status_code, content=response_data)
