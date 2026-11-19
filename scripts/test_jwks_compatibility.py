"""Test Gateway JWKS response compatibility with Discovery Copilot's JWT verification implementation."""

import importlib.util
import json
import os
import ssl
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from google.auth import jwt as google_jwt

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))

def load_module(name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(name, str(file_path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

# Load gateway modules
km_mod = load_module("gateway.app.key_manager", root_dir / "gateway" / "app" / "key_manager.py")
am_mod = load_module("gateway.app.assertion_minter", root_dir / "gateway" / "app" / "assertion_minter.py")
KeyManager = km_mod.KeyManager
AssertionMinter = am_mod.AssertionMinter

# Load discovery-copilot security module
sec_mod = load_module("discovery_copilot.app.app_utils.security", root_dir / "discovery-copilot" / "app" / "app_utils" / "security.py")
verify_identity_token = sec_mod.verify_identity_token

def main():
    print("Testing Gateway JWKS compatibility with Discovery Copilot...")

    # 1. Fetch live JWKS from Gateway
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(
        "https://136.82.174.245/.well-known/jwks.json",
        headers={"Host": "gateway.enterprise.com"}
    )
    with urllib.request.urlopen(req, context=ctx) as r:
        jwks_resp = json.loads(r.read())
    
    print(f"[+] Retrieved Gateway JWKS: {list(jwks_resp.get('keys', {}).keys())}")
    public_keys = jwks_resp["keys"]
    assert "gateway-key-2026-v1" in public_keys

    # 2. Mint an assertion using the Gateway's AssertionMinter
    minter = AssertionMinter()
    token = minter.mint_assertion(
        user_email="alex.chen@enterprise.com",
        user_sub="user_123456",
        session_id="session-test-compat-001"
    )
    print(f"[+] Minted test gateway assertion: {token[:40]}...")

    # 2. Test compatibility using KeyManager's exported public key schema
    local_pub_keys = km_mod.key_manager.get_public_keys()
    print(f"[+] KeyManager public key schema: {list(local_pub_keys.keys())}")
    
    # Verify the JWKS format match
    assert type(public_keys["gateway-key-2026-v1"]) == type(local_pub_keys["gateway-key-2026-v1"])
    assert public_keys["gateway-key-2026-v1"].startswith("-----BEGIN PUBLIC KEY-----")
    
    # Verify local token against local public key
    claims_local = verify_identity_token(
        token=token,
        expected_audience="discovery-copilot-agent-runtime",
        certs_override=local_pub_keys,
        token_type="gateway"
    )
    print(f"[+] Verified against KeyManager public key schema: sub={claims_local.get('sub')}")

    # 3. If production key file or env is available, test against live Gateway JWKS
    prod_pem = os.getenv("GATEWAY_PRIVATE_KEY_PEM")
    key_file = Path("/tmp/gateway_key.pem")
    if not prod_pem and key_file.exists():
        prod_pem = key_file.read_text()
    
    if prod_pem:
        from google.auth.crypt import rsa
        prod_signer = rsa.RSASigner.from_string(prod_pem, "gateway-key-2026-v1")
        now = int(time.time())
        payload = {
            "iss": "https://gateway.enterprise-graphrag.internal",
            "aud": "discovery-copilot-agent-runtime",
            "sub": "user_prod_123456",
            "session_id": "sess-live-compat-001",
            "email": "alex.chen@enterprise.com",
            "jti": f"gw-jti-{uuid.uuid4().hex}",
            "iat": now,
            "exp": now + 300,
        }
        prod_token = google_jwt.encode(prod_signer, payload, header={"kid": "gateway-key-2026-v1", "alg": "RS256", "typ": "JWT"})
        prod_token_str = prod_token.decode("utf-8") if isinstance(prod_token, bytes) else prod_token
        
        # Verify using the LIVE JWKS retrieved from the deployed Gateway
        claims_prod = verify_identity_token(
            token=prod_token_str,
            expected_audience="discovery-copilot-agent-runtime",
            certs_override=public_keys,
            token_type="gateway"
        )
        print(f"[+] Verified against LIVE Gateway JWKS: sub={claims_prod.get('sub')}, email={claims_prod.get('email')}, jti={claims_prod.get('jti')}")
        print("[+] JWKS Compatibility Verified: 100% Validated with production keys!")

if __name__ == "__main__":
    main()
