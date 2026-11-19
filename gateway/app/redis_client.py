"""Lightweight Redis client for atomic JTI replay protection using RESP2."""

import asyncio
import logging
import os
import socket

logger = logging.getLogger("gateway.redis_client")


class RedisReplayError(Exception):
    """Raised when JTI replay is detected."""


class RedisUnavailableError(Exception):
    """Raised when Redis is unreachable, requiring fail-closed enforcement."""


class RedisJTIStore:
    """Manages atomic JTI consumption against Google Cloud Memorystore for Redis."""

    def __init__(self, host: str | None = None, port: int = 6379, timeout: float = 2.0):
        self.host = host or os.getenv("REDIS_HOST", "")
        self.port = port or int(os.getenv("REDIS_PORT", "6379"))
        self.timeout = timeout
        self._in_memory_fallback: dict[str, float] = {}

    def is_configured(self) -> bool:
        return bool(self.host and self.host.strip())

    async def consume_jti(self, key: str, ttl_seconds: int = 330) -> None:
        """Atomically checks and consumes a JTI nonce.

        If Redis is configured:
          - Sends atomic SET key 1 EX ttl NX via RESP2.
          - If key exists: raises RedisReplayError.
          - If connection fails: raises RedisUnavailableError (fail-closed).
        If Redis is not configured:
          - In production: raises RedisUnavailableError (fail-closed).
          - In test/local mode: falls back to in-memory dictionary.
        """
        if self.is_configured():
            try:
                success = await asyncio.to_thread(
                    self._sync_setnx,
                    self.host,
                    self.port,
                    key,
                    ttl_seconds,
                    self.timeout,
                )
                if not success:
                    raise RedisReplayError(f"Replay detected: JTI key '{key}' has already been consumed")
            except (ConnectionError, socket.timeout, OSError) as exc:
                logger.critical("Redis connectivity failure: %s (failing closed)", exc)
                raise RedisUnavailableError("Identity verification unavailable: Distributed replay store unreachable") from exc
        else:
            # Check if running in production
            is_prod = os.getenv("ENVIRONMENT", "").lower() in ("prod", "production") or bool(os.getenv("K_SERVICE"))
            if is_prod:
                logger.critical("REDIS_HOST is missing in production environment! Failing closed.")
                raise RedisUnavailableError("Identity verification unavailable: Distributed replay store is not configured")

            # Local test fallback
            import time
            now = time.time()
            if key in self._in_memory_fallback and self._in_memory_fallback[key] > now:
                raise RedisReplayError(f"Replay detected (local cache): JTI key '{key}' has already been consumed")
            self._in_memory_fallback[key] = now + ttl_seconds

    @staticmethod
    def _sync_setnx(host: str, port: int, key: str, ttl: int, timeout: float) -> bool:
        """Sends RESP2 array: *6\\r\\n$3\\r\\nSET\\r\\n...\\r\\n$2\\r\\nNX\\r\\n"""
        cmd = (
            f"*6\r\n"
            f"$3\r\nSET\r\n"
            f"${len(key)}\r\n{key}\r\n"
            f"$1\r\n1\r\n"
            f"$2\r\nEX\r\n"
            f"${len(str(ttl))}\r\n{ttl}\r\n"
            f"$2\r\nNX\r\n"
        ).encode("utf-8")
        sock = None
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
            sock.sendall(cmd)
            resp = sock.recv(1024)
            if resp.startswith(b"+OK"):
                return True
            elif resp.startswith(b"$-1"):
                return False
            else:
                raise ConnectionError(f"Unexpected Redis response: {resp!r}")
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass


redis_store = RedisJTIStore()
