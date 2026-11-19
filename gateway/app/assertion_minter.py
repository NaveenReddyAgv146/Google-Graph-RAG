"""Mints short-lived RS256 assertions for private Agent Runtime dispatch."""

import logging
import time
import uuid
from typing import Any
from google.auth import jwt as google_jwt

from gateway.app.config import settings
from gateway.app.key_manager import key_manager

logger = logging.getLogger("gateway.minter")


class AssertionMinter:
    """Mints cryptographically signed, short-lived gateway assertions."""

    @staticmethod
    def mint_assertion(
        user_email: str,
        user_sub: str,
        session_id: str | None = None,
    ) -> str:
        """Creates an RS256 JWT assertion with distinct sub, session_id, and jti."""
        now = int(time.time())
        exp = now + settings.assertion_ttl_seconds
        jti = f"gw-jti-{uuid.uuid4().hex}"
        sess_id = session_id or f"sess-{uuid.uuid4().hex}"

        payload = {
            "iss": settings.gateway_issuer,
            "aud": settings.gateway_audience,
            "sub": user_sub,  # Stable user identity
            "session_id": sess_id,  # Distinct session identifier
            "email": user_email.lower(),
            "jti": jti,  # Nonce for replay protection
            "iat": now,
            "exp": exp,
        }

        signer = key_manager.get_active_signer()
        header = {
            "kid": key_manager.get_active_kid(),
            "alg": "RS256",
            "typ": "JWT",
        }

        token = google_jwt.encode(signer, payload, header=header)
        token_str = token.decode("utf-8") if isinstance(token, bytes) else token

        # Security: Never log signed assertions
        logger.debug("Minted short-lived assertion for '%s' (kid='%s', jti='%s')", user_email, header["kid"], jti)
        return token_str
