"""Test direct invocation of Reasoning Engine via Agent Runtime /api passthrough with Gateway assertion."""

import json
import os
import subprocess
import time
import urllib.request
import urllib.error
import uuid
from google.auth import jwt as google_jwt
from google.auth.crypt import rsa

REASONING_ENGINE_URL = "https://asia-south1-aiplatform.googleapis.com/reasoningEngines/v1/projects/291679685305/locations/asia-south1/reasoningEngines/8796348108905644032/api"

def main():
    print("[*] Retrieving Gateway private key and GCP OAuth token...")
    # 1. Fetch GCP access token
    gcp_token = os.getenv("GCP_ACCESS_TOKEN")
    if not gcp_token:
        proc_tok = subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True, text=True, check=True)
        gcp_token = proc_tok.stdout.strip()

    # 2. Fetch Gateway private key from Secret Manager or temp file
    priv_key_pem = os.getenv("GATEWAY_PRIVATE_KEY_PEM")
    if not priv_key_pem and os.path.exists("/tmp/gw_key.pem"):
        with open("/tmp/gw_key.pem", "r", encoding="utf-8") as f:
            priv_key_pem = f.read().strip()
    if not priv_key_pem:
        proc_key = subprocess.run(["gcloud", "secrets", "versions", "access", "1", "--secret=gateway-signing-key"], capture_output=True, text=True, check=True)
        priv_key_pem = proc_key.stdout.strip()

    # 3. Mint short-lived gateway assertion
    signer = rsa.RSASigner.from_string(priv_key_pem, "gateway-key-2026-v1")
    now = int(time.time())
    payload = {
        "iss": "https://gateway.enterprise-graphrag.internal",
        "aud": "discovery-copilot-agent-runtime",
        "sub": "user_prod_tester_01",
        "session_id": f"sess-{uuid.uuid4().hex}",
        "email": "alex.chen@enterprise.com",
        "jti": f"gw-jti-{uuid.uuid4().hex}",
        "iat": now,
        "exp": now + 300,
    }
    assertion = google_jwt.encode(signer, payload, header={"kid": "gateway-key-2026-v1", "alg": "RS256", "typ": "JWT"})
    assertion_str = assertion.decode("utf-8") if isinstance(assertion, bytes) else assertion

    # 4. Request Agent Card with Gateway assertion
    agent_card_url = f"{REASONING_ENGINE_URL}/a2a/discovery_copilot/.well-known/agent.json"
    print(f"[*] Requesting Agent Card from: {agent_card_url}...")
    headers = {
        "Authorization": f"Bearer {gcp_token}",
        "X-Enterprise-Gateway-Assertion": assertion_str,
    }
    req = urllib.request.Request(agent_card_url, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            print(f"[+] Agent Card HTTP {resp.status}:")
            card_data = json.loads(resp.read().decode("utf-8"))
            print(json.dumps(card_data, indent=2)[:500])
    except urllib.error.HTTPError as exc:
        print(f"[-] HTTP {exc.code}: {exc.read().decode('utf-8')[:300]}")
    except Exception as exc:
        print(f"[-] Error: {exc}")

if __name__ == "__main__":
    main()
