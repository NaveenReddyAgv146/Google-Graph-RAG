"""Key manager for Gateway RS256 token minting and key rotation."""

import json
import logging
import os
from typing import Any
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth.crypt import RSASigner

logger = logging.getLogger("gateway.key_manager")


class KeyManager:
    """Manages RSA key pairs, active signer selection, and public JWKS export."""

    def __init__(self, active_kid: str = "gateway-key-2026-v1"):
        self._active_kid = active_kid
        self._signers: dict[str, RSASigner] = {}
        self._public_pems: dict[str, str] = {}
        self._initialize_keys()

    def _initialize_keys(self) -> None:
        """Loads private keys from environment or generates an ephemeral key pair for local/test."""
        priv_key_env = os.getenv("GATEWAY_PRIVATE_KEY_PEM")
        if priv_key_env:
            try:
                signer = RSASigner.from_string(priv_key_env, key_id=self._active_kid)
                self._signers[self._active_kid] = signer
                # Extract public key
                priv_obj = serialization.load_pem_private_key(priv_key_env.encode(), password=None)
                pem_pub = priv_obj.public_key().public_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PublicFormat.SubjectPublicKeyInfo,
                ).decode("utf-8")
                self._public_pems[self._active_kid] = pem_pub
                logger.info("Loaded gateway private key from environment for kid='%s'", self._active_kid)
                return
            except Exception as exc:
                logger.error("Failed to load GATEWAY_PRIVATE_KEY_PEM: %s", exc)

        # Fallback to ephemeral in-memory key pair
        self.generate_and_register_key(self._active_kid)

    def generate_and_register_key(self, kid: str) -> str:
        """Generates a new RSA 2048-bit key pair, registers it, and returns the public PEM."""
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem_private = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        pem_public = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")

        signer = RSASigner.from_string(pem_private, key_id=kid)
        self._signers[kid] = signer
        self._public_pems[kid] = pem_public
        logger.info("Registered RSA key pair for kid='%s'", kid)
        return pem_public

    def set_active_key(self, kid: str) -> None:
        """Rotates the active signing key ID."""
        if kid not in self._signers:
            raise ValueError(f"Key ID '{kid}' is not registered in KeyManager")
        self._active_kid = kid
        logger.info("Rotated active signing key to kid='%s'", kid)

    def get_active_signer(self) -> RSASigner:
        """Retrieves the active RSASigner for minting assertions."""
        if self._active_kid not in self._signers:
            raise RuntimeError(f"Active key '{self._active_kid}' not found in KeyManager")
        return self._signers[self._active_kid]

    def get_public_keys(self) -> dict[str, str]:
        """Returns mapping of kid -> public PEM strings for all registered keys."""
        return dict(self._public_pems)

    def get_active_kid(self) -> str:
        return self._active_kid


# Global key manager singleton
key_manager = KeyManager()
