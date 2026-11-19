

import json
import subprocess
import urllib.request
import urllib.error
import time
import sys

PROJECT = "xenon-broker-513704-s9"
BACKEND = "ingestion-copilot-backend"
CLIENT_ID = (
    "291679685305-vdipi2u0sqa1jbjtp9toqoc5m4d5qo6a"
    ".apps.googleusercontent.com"
)
SECRET_NAME = "iap-oauth-client-secret"
SECRET_VERSION = "1"


def gcloud(*args):
    result = subprocess.run(
        ["gcloud", *args],
        check=True,
        capture_output=True,
        text=True
    )
    return result.stdout.strip()


def api_request(url, token, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        },
        method=method
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def main():
    token = None
    secret = None

    try:
        print("1. Getting Google access token...")
        token = gcloud("auth", "print-access-token")

        print("2. Reading OAuth secret from Secret Manager...")
        secret = gcloud(
            "secrets", "versions", "access", SECRET_VERSION,
            f"--secret={SECRET_NAME}",
            f"--project={PROJECT}"
        )

        if not secret:
            raise RuntimeError("Secret Manager returned an empty secret.")

        base_url = (
            "https://compute.googleapis.com/compute/v1/"
            f"projects/{PROJECT}/global/backendServices/{BACKEND}"
        )

        print("3. Submitting IAP configuration...")
        result = api_request(
            base_url,
            token,
            method="PATCH",
            body={
                "iap": {
                    "enabled": True,
                    "oauth2ClientId": CLIENT_ID,
                    "oauth2ClientSecret": secret
                }
            }
        )

        operation = result.get("name")
        if not operation:
            raise RuntimeError("No Compute operation ID was returned.")

        print("4. Waiting for the Compute operation...")
        operation_url = (
            "https://compute.googleapis.com/compute/v1/"
            f"projects/{PROJECT}/global/operations/{operation}"
        )

        for _ in range(90):
            status = api_request(operation_url, token)

            if status.get("status") == "DONE":
                if status.get("error"):
                    raise RuntimeError(
                        "Compute operation reported an error. "
                        "Inspect it using gcloud."
                    )
                break

            time.sleep(2)
        else:
            raise TimeoutError(
                "Operation is still running. Do not rerun yet; "
                "check its status first."
            )

        print("5. Verifying backend IAP configuration...")
        backend = api_request(base_url, token)
        iap = backend.get("iap", {})

        enabled = iap.get("enabled") is True
        client_matches = iap.get("oauth2ClientId") == CLIENT_ID
        secret_hash = iap.get("oauth2ClientSecretSha256")

        print("IAP enabled:", enabled)
        print("OAuth client ID matches:", client_matches)
        print("Secret SHA-256 returned:", bool(secret_hash))

        if not enabled or not client_matches or not secret_hash:
            raise RuntimeError(
                "IAP verification incomplete. Do not proceed to routing."
            )

        print("SUCCESS: IAP is enabled and verified on the backend.")

    except urllib.error.HTTPError as exc:
        print("HTTP error:", exc.code)
        print(
            "IAP configuration was not confirmed. "
            "Check permissions and API error details without sharing secrets."
        )
        sys.exit(1)

    except Exception as exc:
        print("ERROR:", type(exc).__name__, str(exc))
        sys.exit(1)

    finally:
        secret = None
        token = None


if __name__ == "__main__":
    main()


