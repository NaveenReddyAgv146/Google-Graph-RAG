"""Verification script for live Enterprise Discovery Gateway deployment."""

import json
import urllib.request
import urllib.error

import os
import ssl

BASE_URL = os.getenv("GATEWAY_URL", "https://136.82.174.245")
HOST_HEADER = os.getenv("GATEWAY_HOST_HEADER", "gateway.enterprise.com")

# Permissive SSL context for self-signed load-balancer certificates
ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

def test_endpoint(name: str, path: str, method: str = "GET", headers: dict = None, data: dict = None):
    url = f"{BASE_URL}{path}"
    headers = headers or {}
    if HOST_HEADER and "Host" not in headers:
        headers["Host"] = HOST_HEADER
    encoded_data = json.dumps(data).encode("utf-8") if data else None
    if encoded_data and "Content-Type" not in headers:
        headers["Content-Type"] = "application/json"
    
    req = urllib.request.Request(url, data=encoded_data, headers=headers, method=method)
    print(f"[*] Testing {name} -> {method} {path}...")
    try:
        with urllib.request.urlopen(req, context=ssl_ctx) as resp:
            status = resp.status
            body = resp.read().decode("utf-8")
            print(f"    [+] SUCCESS (HTTP {status}): {body[:150]}")
            return status, body
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read().decode("utf-8")
        print(f"    [!] HTTP {status}: {body[:150]}")
        return status, body
    except Exception as exc:
        print(f"    [-] ERROR: {exc}")
        return 0, str(exc)

if __name__ == "__main__":
    print("=" * 60)
    print("LIVE GATEWAY DEPLOYMENT VERIFICATION")
    print("=" * 60)

    # 1. Health endpoint
    s1, b1 = test_endpoint("Health Check", "/health")
    assert s1 == 200, f"Expected 200, got {s1}"

    # 2. JWKS endpoint
    s2, b2 = test_endpoint("JWKS Discovery", "/.well-known/jwks.json")
    assert s2 == 200, f"Expected 200, got {s2}"
    jwks = json.loads(b2)
    assert "gateway-key-2026-v1" in jwks.get("keys", {}), "Missing active kid"

    # 3. Missing Auth on A2A streaming route
    s3, b3 = test_endpoint(
        "Missing Auth Header",
        "/v1/message:stream",
        method="POST",
        data={"message": "hello"}
    )
    assert s3 == 401, f"Expected 401, got {s3}"
    assert "Missing or malformed Authorization Bearer header" in b3

    # 4. Invalid Token
    import uuid
    fresh_test_token = f"ya29.live_test_{uuid.uuid4().hex}"
    s4, b4 = test_endpoint(
        "Invalid Bearer Token (First Attempt)",
        "/v1/message:stream",
        method="POST",
        headers={"Authorization": f"Bearer {fresh_test_token}"},
        data={"message": "hello"}
    )
    assert s4 == 401, f"Expected 401, got {s4}"
    assert "Google token introspection rejected token" in b4, f"Expected token introspection error, got: {b4}"

    # 5. Replay Protection: present same token again
    s5, b5 = test_endpoint(
        "Token Replay Attempt (Duplicate Presentation)",
        "/v1/message:stream",
        method="POST",
        headers={"Authorization": f"Bearer {fresh_test_token}"},
        data={"message": "hello"}
    )
    assert s5 == 401, f"Expected 401, got {s5}"
    assert "Token replay detected" in b5

    print("=" * 60)
    print("ALL VERIFICATION CHECKS PASSED!")
    print("=" * 60)
