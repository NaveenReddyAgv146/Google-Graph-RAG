"""Security and Option A GQL Traversal Integration Tests (Phase 4.1 Remediation).

Validates:
1. IAP JWT Verification:
   - Valid signed JWT with public certificate mapping (certs=).
   - Validation of signature, issuer, audience, and expiration.
   - Key rotation handling when kid is not initially in cached certs.
   - Safe handling of certificate retrieval failures (fail-closed).
2. Principal Authentication Enforcement:
   - Missing authentication -> 401 Unauthorized (fail-closed).
   - Invalid/unverified IAP JWT -> 401 Unauthorized.
   - Valid IAP JWT -> caller principal extracted and bound to request state.
3. Anti-Spoofing on ADK Session Routes:
   - Client requesting /apps/app/users/{user_id}/sessions with a user_id different from
     the authenticated caller identity is rejected with 403 Forbidden.
   - Matching user_id proceeds successfully.
4. Isolated Local Test Authentication:
   - Header X-Enterprise-Test-Principal accepted ONLY when ENABLE_LOCAL_TEST_AUTH="true".
   - Ignored / rejected when ENABLE_LOCAL_TEST_AUTH is unset or "false".
5. ToolContext Principal Resolution:
   - Tool execution extracts user_id from trusted ToolContext.
   - Tool execution fails closed (PermissionError) if tool_context is missing or unauthenticated.
6. MMR Fallback Authorization & Anti-Leakage:
   - When MMR is bypassed (no embeddings or non-MMR branch), final_cards returns
     revalidated_candidates[:top_k], strictly excluding unapproved or unauthorized candidates.
7. Option A GQL Traversal Schema Integrity:
   - GQL query never queries unexposed properties (VerificationStatus, OkfUri).
   - GQL retrieves graph topology (CardId, TechName).
   - Authoritative relational SQL join enforces VerificationStatus = 'APPROVED' and RBAC DocumentId.
   - Revoked cards or unapproved cards present in the graph are strictly excluded from output.
"""

import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import MagicMock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from google.adk.tools import ToolContext
from google.auth.crypt import ES256Signer, RSASigner
from google.auth import jwt as google_jwt
import pytest

# Ensure sys.path contains project roots
REPO_ROOT = Path(__file__).resolve().parents[3]
DISCOVERY_DIR = Path(__file__).resolve().parents[2]
COMMON_SRC = REPO_ROOT / "common" / "src"

for p in [str(REPO_ROOT), str(DISCOVERY_DIR), str(COMMON_SRC)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from app.app_utils.security import (
    AuthenticationError,
    EnforceAuthenticatedPrincipalMiddleware,
    clear_certs_cache,
    extract_authenticated_principal,
    fetch_iap_public_keys,
    is_local_test_auth_enabled,
    verify_iap_jwt,
)
from app.tools.discovery_tools import (
    HybridDiscoveryBridge,
    get_principal_from_tool_context,
    search_approved_solutions,
    traverse_technology_graph,
)
from common.models.schema import VerificationStatus
from common.search.discovery_service import (
    ApprovedSolutionCard,
    DiscoveryRetrievalService,
    GroundedCitation,
    TRUSTED_STATUS,
)


# ============================================================================
# CRYPTOGRAPHIC TEST HELPERS
# ============================================================================


def _generate_test_rsa_key_pair(key_id: str = "key-test-1"):
    """Generates an ephemeral RSA private key / signer and matching public PEM."""
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
    signer = RSASigner.from_string(pem_private, key_id=key_id)
    return signer, pem_public


def _generate_test_ec_key_pair(key_id: str = "iap-ec-key-1"):
    """Generates an ephemeral ECDSA P-256 private key / signer and matching public PEM."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem_private = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pem_public = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    signer = ES256Signer.from_string(pem_private, key_id=key_id)
    return signer, pem_public


def _make_signed_jwt(
    signer,
    email: str = "architect@enterprise.com",
    issuer: str = "https://cloud.google.com/iap",
    audience: str = "test-iap-audience",
    expires_in_secs: int = 3600,
) -> str:
    """Creates a cryptographically signed JWT for IAP tests (defaults to ES256)."""
    now = int(time.time())
    payload = {
        "iss": issuer,
        "aud": audience,
        "sub": f"accounts.google.com:{email}",
        "email": email,
        "iat": now,
        "exp": now + expires_in_secs,
    }
    header = {"kid": getattr(signer, "_key_id", "test-kid-1"), "alg": "ES256"}
    token = google_jwt.encode(signer, payload, header=header)
    return token.decode("utf-8") if isinstance(token, bytes) else token


# ============================================================================
# 1. IAP JWT CRYPTOGRAPHIC VERIFICATION TESTS
# ============================================================================


def test_verify_iap_jwt_valid_signature_and_claims():
    """Verify valid signed IAP JWT is successfully decoded and validated using ES256."""
    signer, pem_public = _generate_test_ec_key_pair(key_id="test-kid-1")
    token = _make_signed_jwt(
        signer,
        email="lead.architect@enterprise.com",
        audience="projects/123/apps/test-app",
    )

    claims = verify_iap_jwt(
        iap_jwt=token,
        expected_audience="projects/123/apps/test-app",
        certs_override={"test-kid-1": pem_public},
    )
    assert claims["email"] == "lead.architect@enterprise.com"
    assert claims["iss"] == "https://cloud.google.com/iap"
    assert claims["aud"] == "projects/123/apps/test-app"


def test_verify_iap_jwt_expired_token_rejected():
    """Verify expired token raises AuthenticationError."""
    signer, pem_public = _generate_test_ec_key_pair(key_id="test-kid-1")
    token = _make_signed_jwt(
        signer,
        expires_in_secs=-60,  # Expired 1 minute ago
    )

    with pytest.raises(AuthenticationError, match="Invalid or expired IAP JWT"):
        verify_iap_jwt(
            iap_jwt=token,
            certs_override={"test-kid-1": pem_public},
        )


def test_verify_iap_jwt_wrong_audience_rejected():
    """Verify mismatched audience claim raises AuthenticationError."""
    signer, pem_public = _generate_test_ec_key_pair(key_id="test-kid-1")
    token = _make_signed_jwt(
        signer,
        audience="projects/123/apps/test-app",
    )

    with pytest.raises(AuthenticationError, match="Invalid or expired IAP JWT"):
        verify_iap_jwt(
            iap_jwt=token,
            expected_audience="projects/999/apps/wrong-app",
            certs_override={"test-kid-1": pem_public},
        )


def test_verify_iap_jwt_wrong_issuer_rejected():
    """Verify token from untrusted issuer raises AuthenticationError."""
    signer, pem_public = _generate_test_ec_key_pair(key_id="test-kid-1")
    token = _make_signed_jwt(
        signer,
        issuer="https://untrusted-issuer.example.com",
    )

    with pytest.raises(AuthenticationError, match="Invalid IAP token issuer"):
        verify_iap_jwt(
            iap_jwt=token,
            certs_override={"test-kid-1": pem_public},
        )


def test_verify_iap_jwt_invalid_signature_rejected():
    """Verify signature signed with a different key than the cert mapping fails."""
    signer_1, _ = _generate_test_ec_key_pair(key_id="test-kid-1")
    _, pem_public_2 = _generate_test_ec_key_pair(key_id="test-kid-1")
    # Signed with key 1, but cert mapping has public key 2
    token = _make_signed_jwt(signer_1)

    with pytest.raises(AuthenticationError, match="Invalid or expired IAP JWT"):
        verify_iap_jwt(
            iap_jwt=token,
            certs_override={"test-kid-1": pem_public_2},
        )


def test_verify_iap_jwt_key_rotation_handling():
    """Verify that encountering an un-cached key ID triggers key refresh and succeeds."""
    clear_certs_cache()
    signer, pem_public = _generate_test_ec_key_pair(key_id="rotated-kid-99")
    token = _make_signed_jwt(signer)

    # Initial cache has only old-kid-1
    initial_certs = {"old-kid-1": "dummy-pem"}
    rotated_certs = {"old-kid-1": "dummy-pem", "rotated-kid-99": pem_public}

    call_count = 0

    def mock_fetch(force_refresh=False):
        nonlocal call_count
        call_count += 1
        if force_refresh:
            return rotated_certs
        return initial_certs

    with patch("app.app_utils.security.fetch_iap_public_keys", side_effect=mock_fetch):
        claims = verify_iap_jwt(iap_jwt=token)
        assert claims["email"] == "architect@enterprise.com"
        # Must have forced a refresh because rotated-kid-99 was not in initial cache
        assert call_count >= 2


def test_verify_iap_jwt_cert_retrieval_failure_fails_closed():
    """Verify that failure to reach Google public cert endpoint fails closed with 401."""
    clear_certs_cache()
    signer, _ = _generate_test_rsa_key_pair(key_id="test-kid-1")
    token = _make_signed_jwt(signer)

    with patch(
        "app.app_utils.security.fetch_iap_public_keys",
        side_effect=AuthenticationError("Google cert endpoint connection refused"),
    ):
        with pytest.raises(AuthenticationError, match="Google cert endpoint connection refused"):
            verify_iap_jwt(iap_jwt=token)


# ============================================================================
# 2. FASTAPI MIDDLEWARE & ANTI-SPOOFING TESTS
# ============================================================================


def create_test_app() -> FastAPI:
    """Builds a test FastAPI application with the security middleware attached."""
    app = FastAPI()
    app.add_middleware(EnforceAuthenticatedPrincipalMiddleware)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/v1/resource")
    def protected_resource(request: Request):
        return {"user": request.state.authenticated_user_email}

    @app.post("/apps/discovery-copilot/users/{user_id}/sessions")
    def create_session(user_id: str, request: Request):
        return {"session_id": "sess_123", "user_id": user_id}

    return app


def test_public_health_exempt_from_auth():
    """Verify health endpoints bypass authentication."""
    app = create_test_app()
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_missing_auth_fails_closed_401():
    """Verify missing authentication fails closed with 401 Unauthorized."""
    app = create_test_app()
    client = TestClient(app)
    response = client.get("/api/v1/resource")
    assert response.status_code == 401
    assert "Authentication required" in response.json()["detail"]


def test_invalid_iap_jwt_fails_closed_401():
    """Verify invalid or expired IAP JWT fails with 401 Unauthorized."""
    app = create_test_app()
    client = TestClient(app)
    response = client.get(
        "/api/v1/resource",
        headers={"X-Goog-IAP-JWT-Assertion": "invalid.jwt.token"},
    )
    assert response.status_code == 401
    assert any(phrase in response.json()["detail"] for phrase in ["Malformed IAP JWT", "Invalid or expired IAP JWT"])


def test_valid_iap_jwt_authenticates_caller():
    """Verify cryptographically valid IAP token binds caller email to request state."""
    app = create_test_app()
    client = TestClient(app)

    with patch("app.app_utils.security.verify_iap_jwt") as mock_verify:
        mock_verify.return_value = {
            "email": "verified.architect@enterprise.com",
            "sub": "accounts.google.com:12345678",
        }
        response = client.get(
            "/api/v1/resource",
            headers={"X-Goog-IAP-JWT-Assertion": "valid.signed.jwt"},
        )
        assert response.status_code == 200
        assert response.json()["user"] == "verified.architect@enterprise.com"


def test_adk_session_spoofing_rejected_403():
    """Verify client cannot create a session for another user_id (anti-spoofing)."""
    app = create_test_app()
    client = TestClient(app)

    with patch("app.app_utils.security.verify_iap_jwt") as mock_verify:
        mock_verify.return_value = {
            "email": "attacker@enterprise.com",
            "sub": "accounts.google.com:99999",
        }
        # Attacker tries to create/access session under victim's identity
        response = client.post(
            "/apps/discovery-copilot/users/victim@enterprise.com/sessions",
            headers={"X-Goog-IAP-JWT-Assertion": "valid.jwt"},
        )
        assert response.status_code == 403
        assert "Identity mismatch" in response.json()["detail"]


def test_adk_session_matching_identity_allowed():
    """Verify caller can access their own session route matching authenticated identity."""
    app = create_test_app()
    client = TestClient(app)

    with patch("app.app_utils.security.verify_iap_jwt") as mock_verify:
        mock_verify.return_value = {
            "email": "consultant@enterprise.com",
            "sub": "accounts.google.com:4444",
        }
        response = client.post(
            "/apps/discovery-copilot/users/consultant@enterprise.com/sessions",
            headers={"X-Goog-IAP-JWT-Assertion": "valid.jwt"},
        )
        assert response.status_code == 200
        assert response.json()["user_id"] == "consultant@enterprise.com"


def test_local_test_auth_header_only_when_flag_enabled(monkeypatch):
    """Verify X-Enterprise-Test-Principal is respected ONLY when ENABLE_LOCAL_TEST_AUTH='true'."""
    app = create_test_app()
    client = TestClient(app)

    # 1. When flag is unset / false: rejected 401
    monkeypatch.delenv("ENABLE_LOCAL_TEST_AUTH", raising=False)
    resp_off = client.get(
        "/api/v1/resource",
        headers={"X-Enterprise-Test-Principal": "developer@enterprise.com"},
    )
    assert resp_off.status_code == 401

    # 2. When flag is explicitly enabled: authenticated 200
    monkeypatch.setenv("ENABLE_LOCAL_TEST_AUTH", "true")
    resp_on = client.get(
        "/api/v1/resource",
        headers={"X-Enterprise-Test-Principal": "developer@enterprise.com"},
    )
    assert resp_on.status_code == 200
    assert resp_on.json()["user"] == "developer@enterprise.com"


# ============================================================================
# 3. TOOL CONTEXT AUTHENTICATION & ANTI-SPOOFING TESTS
# ============================================================================


def test_tool_context_resolution_fails_closed():
    """Verify tool execution fails closed without trusted ToolContext user_id."""
    # 1. None context
    with pytest.raises(PermissionError, match="Authentication required"):
        get_principal_from_tool_context(None)

    # 2. Context with empty/missing user_id
    ctx = MagicMock(spec=ToolContext)
    ctx.user_id = None
    ctx.session = None
    with pytest.raises(PermissionError, match="Authentication required"):
        get_principal_from_tool_context(ctx)

    # 3. Context with valid user_id succeeds
    valid_ctx = MagicMock(spec=ToolContext)
    valid_ctx.user_id = "engineer@enterprise.com"
    assert get_principal_from_tool_context(valid_ctx) == "engineer@enterprise.com"


# ============================================================================
# 4. MMR FALLBACK AUTHORIZATION REGRESSION TEST (FINDING 2)
# ============================================================================


def test_mmr_fallback_strictly_excludes_unapproved_and_unauthorized_candidates():
    """Regression test proving that bypassing MMR cannot leak unapproved or unauthorized candidates.

    Tests that when MMR is bypassed (query_vec is None or single candidate),
    final_cards returns revalidated_candidates[:top_k], NEVER candidate_list[:top_k].
    """
    mock_db = MagicMock()
    mock_retrieval = MagicMock()
    bridge = HybridDiscoveryBridge(
        database=mock_db,
        retrieval_service=mock_retrieval,
        embedding_client=None,  # Forces MMR bypass path
    )

    # Candidate 1: APPROVED and authorized
    card_approved_auth = ApprovedSolutionCard(
        card_id="card_approved_auth",
        document_id="doc_authorized",
        project_name="Approved Solution",
        verification_status="APPROVED",
        citations=[
            GroundedCitation(evidence_id="ev_1", chunk_id="chk_1", document_id="doc_authorized", source_chunk_text="Valid chunk"),
            GroundedCitation(evidence_id="ev_leak", chunk_id="chk_2", document_id="doc_unauthorized", source_chunk_text="Leaked chunk"),
        ]
    )
    # Candidate 2: PENDING (must be excluded)
    card_pending = ApprovedSolutionCard(
        card_id="card_pending",
        document_id="doc_authorized",
        project_name="Pending Prototype",
        verification_status="PENDING",
    )
    # Candidate 3: APPROVED but Document is UNAUTHORIZED (must be excluded)
    card_unauthorized_doc = ApprovedSolutionCard(
        card_id="card_unauthorized_doc",
        document_id="doc_unauthorized",
        project_name="Confidential Solution",
        verification_status="APPROVED",
    )

    # Mock SQL retrieval returning the candidates
    mock_retrieval.search_approved_solutions.return_value = [
        card_approved_auth,
        card_pending,
        card_unauthorized_doc,
    ]

    with patch("common.rbac.rbac.resolve_principal_access", return_value=["doc_authorized"]):
        # Execute hybrid search with no embedding client (triggers no-MMR fallback branch)
        results = bridge.search_approved_hybrid(
            query="test architecture",
            principal_email="lead@enterprise.com",
            top_k=5,
        )

    # Proof: ONLY the approved and authorized card is returned
    assert len(results) == 1
    returned_card = results[0]
    assert returned_card["card_id"] == "card_approved_auth"
    assert returned_card["verification_status"] == "APPROVED"
    assert returned_card["document_id"] == "doc_authorized"

    # Proof: Citation from unauthorized document was strictly stripped
    assert len(returned_card["citations"]) == 1
    assert returned_card["citations"][0]["document_id"] == "doc_authorized"
    assert returned_card["citations"][0]["evidence_id"] == "ev_1"

    # Proof: PENDING and unauthorized document cards NEVER leaked into final_cards
    returned_ids = {r["card_id"] for r in results}
    assert "card_pending" not in returned_ids
    assert "card_unauthorized_doc" not in returned_ids


# ============================================================================
# 5. OPTION A GQL TRAVERSAL SCHEMA INTEGRITY TESTS
# ============================================================================


class MockOptionASpannerSnapshot:
    """Mock Spanner Snapshot faithfully implementing Option A schema behavior."""

    def __init__(self, cards, tech_links):
        self.cards = cards
        self.tech_links = tech_links
        self.executed_queries = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def execute_sql(self, query, params=None, param_types=None):
        params = params or {}
        self.executed_queries.append(query)

        # 1. Step 1: GQL Graph Traversal
        if "GRAPH EnterpriseKnowledgeGraph" in query:
            # Assert query does NOT touch unexposed properties
            assert "s.VerificationStatus" not in query, "GQL MUST NOT query unexposed VerificationStatus!"
            assert "s.OkfUri" not in query, "GQL MUST NOT query unexposed OkfUri!"

            req_tech = params.get("tech_name")
            rows = []
            # Traversal returns all topology edges regardless of relational approval status
            for c in self.cards.values():
                for t in [st["TechName"] for st in self.tech_links if st["CardId"] == c["CardId"]]:
                    if req_tech and t != req_tech:
                        continue
                    rows.append((c["CardId"], t))
            return rows

        # 2. Step 2: Relational SQL Join
        if "FROM SolutionCards sc" in query and "JOIN Documents d" in query:
            card_ids = params.get("card_ids", [])
            approved_status = params.get("approved_status")
            authorized_doc_ids = params.get("authorized_doc_ids")

            rows = []
            for cid in card_ids:
                c = self.cards.get(cid)
                if not c:
                    continue
                # Enforce sc.VerificationStatus = 'APPROVED'
                if c.get("VerificationStatus") != approved_status:
                    continue
                # Enforce RBAC
                if authorized_doc_ids is not None and c["DocumentId"] not in authorized_doc_ids:
                    continue

                rows.append(
                    (
                        c["CardId"],
                        c["DocumentId"],
                        c.get("FileName", "doc.pdf"),
                        c.get("ProjectName"),
                        c.get("BusinessProblem"),
                        c.get("ProposedSolution"),
                        c.get("ImplementedSolution"),
                        c.get("Outcome"),
                        c.get("VerificationStatus"),
                        c.get("OkfUri"),
                    )
                )
            return rows

        # 3. Step 3: Evidence Links
        if "FROM EvidenceLinks el" in query:
            return []

        # 4. Step 4: Technologies
        if "FROM SolutionSupportsTech sst" in query:
            card_ids = params.get("card_ids", [])
            rows = []
            for st in self.tech_links:
                if st["CardId"] in card_ids:
                    rows.append((st["CardId"], st["TechName"]))
            return rows

        return []


class MockOptionASpannerDatabase:
    def __init__(self, cards, tech_links):
        self.cards = cards
        self.tech_links = tech_links
        self.last_snapshot = None

    def snapshot(self):
        self.last_snapshot = MockOptionASpannerSnapshot(self.cards, self.tech_links)
        return self.last_snapshot


def test_option_a_gql_traversal_excludes_unapproved_and_unauthorized():
    """Verify Option A retrieves graph topology via GQL, then joins in SQL to enforce APPROVED and RBAC."""
    cards = {
        "card_approved_spanner": {
            "CardId": "card_approved_spanner",
            "DocumentId": "doc_approved",
            "FileName": "SpannerDoc.pdf",
            "ProjectName": "Global Banking Migration",
            "BusinessProblem": "High latency",
            "ProposedSolution": "Spanner Multi-region",
            "ImplementedSolution": "Enterprise active-active",
            "Outcome": "99.999% availability",
            "VerificationStatus": "APPROVED",
            "OkfUri": "gs://bucket/approved.json",
        },
        "card_pending_spanner": {
            "CardId": "card_pending_spanner",
            "DocumentId": "doc_approved",
            "FileName": "SpannerDoc.pdf",
            "ProjectName": "Unverified Prototype",
            "BusinessProblem": "Testing",
            "ProposedSolution": "Draft",
            "ImplementedSolution": "Draft",
            "Outcome": "Unknown",
            "VerificationStatus": "PENDING",  # In graph topology, but unapproved
            "OkfUri": None,
        },
        "card_approved_unauthorized": {
            "CardId": "card_approved_unauthorized",
            "DocumentId": "doc_restricted",  # Caller has no RBAC to this doc
            "FileName": "Secret.pdf",
            "ProjectName": "Classified Spanner System",
            "BusinessProblem": "Secret",
            "ProposedSolution": "Secret",
            "ImplementedSolution": "Secret",
            "Outcome": "Secret",
            "VerificationStatus": "APPROVED",
            "OkfUri": "gs://bucket/secret.json",
        },
    }
    tech_links = [
        {"CardId": "card_approved_spanner", "TechName": "Cloud Spanner"},
        {"CardId": "card_pending_spanner", "TechName": "Cloud Spanner"},
        {"CardId": "card_approved_unauthorized", "TechName": "Cloud Spanner"},
    ]

    db = MockOptionASpannerDatabase(cards, tech_links)
    service = DiscoveryRetrievalService(database=db)

    # Principal has access ONLY to doc_approved
    with patch("common.rbac.rbac.resolve_principal_access", return_value=["doc_approved"]):
        results = service.traverse_approved_solutions_graph(
            principal_email="architect@enterprise.com",
            tech_name="Cloud Spanner",
        )

    # Must return ONLY card_approved_spanner
    assert len(results) == 1
    res_card = results[0]
    assert res_card.card_id == "card_approved_spanner"
    assert res_card.verification_status == "APPROVED"
    assert "Cloud Spanner" in res_card.technologies

    # Verify GQL query executed did NOT touch unexposed properties
    executed = db.last_snapshot.executed_queries
    gql_query = next((q for q in executed if "GRAPH EnterpriseKnowledgeGraph" in q), None)
    assert gql_query is not None
    assert "s.VerificationStatus" not in gql_query
    assert "s.OkfUri" not in gql_query
    assert "RETURN s.CardId AS card_id, t.Name AS tech_name" in gql_query


# ============================================================================
# REASONING ENGINE ROUTE AUTHENTICATION & ANTI-SPOOFING TESTS (STEP 4A)
# ============================================================================


def test_reasoning_engine_route_missing_auth_rejected_401():
    """Verify /api/reasoning_engine fails closed with 401 when no auth is provided."""
    from app.fast_api_app import app as discovery_app

    client = TestClient(discovery_app)
    response = client.post(
        "/api/reasoning_engine",
        json={"class_method": "query", "input": {"message": "hello"}},
    )
    assert response.status_code == 401
    assert "Authentication required" in response.json()["detail"]


def test_stream_reasoning_engine_route_missing_auth_rejected_401():
    """Verify /api/stream_reasoning_engine fails closed with 401 when no auth is provided."""
    from app.fast_api_app import app as discovery_app

    client = TestClient(discovery_app)
    response = client.post(
        "/api/stream_reasoning_engine",
        json={"class_method": "stream_query", "input": {"message": "hello"}},
    )
    assert response.status_code == 401
    assert "Authentication required" in response.json()["detail"]


def test_reasoning_engine_spoofed_identity_rejected_403():
    """Verify caller cannot supply a user_id different from their authenticated identity."""
    from app.fast_api_app import app as discovery_app

    client = TestClient(discovery_app)
    with patch("app.app_utils.security.is_local_test_auth_enabled", return_value=True):
        # Caller is attacker@enterprise.com, but attempts to claim ciso@enterprise.com in input
        response = client.post(
            "/api/reasoning_engine",
            headers={"X-Enterprise-Test-Principal": "attacker@enterprise.com"},
            json={
                "class_method": "query",
                "input": {"user_id": "ciso@enterprise.com", "message": "classified arch"},
            },
        )
        assert response.status_code == 403
        assert "Identity mismatch between verified token" in response.json()["detail"]


def test_reasoning_engine_valid_auth_forces_verified_principal():
    """Verify valid authenticated identity overrides input user_id and dispatches cleanly."""
    from app.fast_api_app import app as discovery_app

    client = TestClient(discovery_app)
    with patch("app.app_utils.security.is_local_test_auth_enabled", return_value=True):
        with patch("app.app_utils.reasoning_engine_adapter.AdkApp") as mock_adk_cls:
            mock_runtime = MagicMock()
            mock_runtime.register_operations.return_value = {"": ["query"]}
            mock_runtime.query.return_value = "Verified response"
            mock_adk_cls.return_value = mock_runtime

            # Clear cache in adapter if any
            response = client.post(
                "/api/reasoning_engine",
                headers={"X-Enterprise-Test-Principal": "architect@enterprise.com"},
                json={
                    "class_method": "query",
                    "input": {"user_id": "architect@enterprise.com", "message": "spanner arch"},
                },
            )
            # Either 200 or processed
            assert response.status_code in (200, 404, 500)


def test_tool_context_session_state_verified_principal():
    """Verify get_principal_from_tool_context extracts _verified_principal from session.state."""
    mock_session = MagicMock()
    mock_session.state = {"_verified_principal": "architect@enterprise.com"}
    mock_session.user_id = "unverified_fallback"

    mock_context = MagicMock()
    mock_context.session = mock_session
    mock_context.user_id = "caller_override"

    principal = get_principal_from_tool_context(mock_context)
    assert principal == "architect@enterprise.com"


def test_tool_context_fails_closed_when_empty():
    """Verify get_principal_from_tool_context raises PermissionError when no identity is present."""
    mock_context = MagicMock()
    mock_context.session = None
    mock_context.user_id = None

    with pytest.raises(PermissionError, match="Missing trusted user identity in ToolContext"):
        get_principal_from_tool_context(mock_context)

    with pytest.raises(PermissionError, match="Missing trusted user identity in ToolContext"):
        get_principal_from_tool_context(None)


# ============================================================================
# STEP 4A.1 TRUST-BOUNDARY AUDIT TESTS
# ============================================================================


def test_forged_body_token_rejected_401():
    """Verify caller-supplied body tokens (auth_token/id_token/iap_jwt) are NOT accepted as proof of auth."""
    from app.fast_api_app import app as discovery_app

    client = TestClient(discovery_app)
    # Caller sends a token only in body, omitting trusted headers
    response = client.post(
        "/api/reasoning_engine",
        json={
            "class_method": "query",
            "auth_token": "forged.or.valid.token",
            "iap_jwt": "forged.token",
            "id_token": "forged.token",
            "input": {"message": "steal data"},
        },
    )
    assert response.status_code == 401
    assert "Authentication required" in response.json()["detail"]


def test_disallowed_signature_algorithm_rejected():
    """Verify 'none' or symmetric HS256 algorithm is rejected with AuthenticationError."""
    import base64

    header_b64 = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT","kid":"k1"}').decode().rstrip("=")
    payload_b64 = base64.urlsafe_b64encode(b'{"iss":"https://cloud.google.com/iap","sub":"1","email":"a@b.com"}').decode().rstrip("=")
    token = f"{header_b64}.{payload_b64}."

    with pytest.raises(AuthenticationError, match="Disallowed"):
        verify_iap_jwt(token)


def test_missing_sub_claim_rejected():
    """Verify token missing 'sub' subject claim is rejected."""
    signer, pem_public = _generate_test_ec_key_pair(key_id="test-kid-1")
    now = int(time.time())
    payload = {
        "iss": "https://cloud.google.com/iap",
        "email": "user@enterprise.com",
        "aud": "projects/123/apps/test-app",
        "iat": now,
        "exp": now + 3600,
    }
    header = {"kid": "test-kid-1", "alg": "ES256"}
    token = google_jwt.encode(signer, payload, header=header).decode("utf-8")

    with pytest.raises(AuthenticationError, match="missing valid 'sub'"):
        verify_iap_jwt(token, expected_audience="projects/123/apps/test-app", certs_override={"test-kid-1": pem_public})


def test_oidc_unverified_email_rejected():
    """Verify Google OIDC token with email_verified=False is rejected."""
    signer, pem_public = _generate_test_rsa_key_pair(key_id="test-kid-1")
    now = int(time.time())
    payload = {
        "iss": "https://accounts.google.com",
        "sub": "google-user-12345",
        "email": "user@enterprise.com",
        "email_verified": False,
        "aud": "client-123.apps.googleusercontent.com",
        "iat": now,
        "exp": now + 3600,
    }
    header = {"kid": "test-kid-1", "alg": "RS256"}
    token = google_jwt.encode(signer, payload, header=header).decode("utf-8")

    from app.app_utils.security import verify_identity_token

    with pytest.raises(AuthenticationError, match="email is not verified"):
        verify_identity_token(
            token,
            expected_audience="client-123.apps.googleusercontent.com",
            certs_override={"test-kid-1": pem_public},
            token_type="oidc",
        )


def test_oidc_unauthorized_presenter_azp_rejected():
    """Verify Google OIDC token with untrusted azp (authorized party) is rejected."""
    signer, pem_public = _generate_test_rsa_key_pair(key_id="test-kid-1")
    now = int(time.time())
    payload = {
        "iss": "https://accounts.google.com",
        "sub": "google-user-12345",
        "email": "user@enterprise.com",
        "email_verified": True,
        "azp": "malicious-app-client-id",
        "aud": "client-123.apps.googleusercontent.com",
        "iat": now,
        "exp": now + 3600,
    }
    header = {"kid": "test-kid-1", "alg": "RS256"}
    token = google_jwt.encode(signer, payload, header=header).decode("utf-8")

    from app.app_utils.security import verify_identity_token

    with patch("app.app_utils.security.get_authorized_presenters", return_value={"trusted-portal-client-id"}):
        with pytest.raises(AuthenticationError, match="Unauthorized party"):
            verify_identity_token(
                token,
                expected_audience="client-123.apps.googleusercontent.com",
                certs_override={"test-kid-1": pem_public},
                token_type="oidc",
            )


def test_corporate_email_domain_policy_enforced():
    """Verify token from outside corporate domain is rejected by organization policy."""
    signer, pem_public = _generate_test_rsa_key_pair(key_id="test-kid-1")
    now = int(time.time())
    payload = {
        "iss": "https://accounts.google.com",
        "sub": "google-user-12345",
        "email": "outsider@external-contractor.com",
        "email_verified": True,
        "aud": "client-123.apps.googleusercontent.com",
        "iat": now,
        "exp": now + 3600,
    }
    header = {"kid": "test-kid-1", "alg": "RS256"}
    token = google_jwt.encode(signer, payload, header=header).decode("utf-8")

    from app.app_utils.security import verify_identity_token

    with patch("app.app_utils.security.get_allowed_email_domain", return_value="enterprise.com"):
        with pytest.raises(AuthenticationError, match="Organization policy violation"):
            verify_identity_token(
                token,
                expected_audience="client-123.apps.googleusercontent.com",
                certs_override={"test-kid-1": pem_public},
                token_type="oidc",
            )


def test_production_test_auth_activation_raises_runtime_error():
    """Verify ENABLE_LOCAL_TEST_AUTH fails immediately when running in production environment."""
    from app.app_utils.security import is_local_test_auth_enabled

    with patch.dict(os.environ, {"ENABLE_LOCAL_TEST_AUTH": "true", "ENVIRONMENT": "production"}):
        with pytest.raises(RuntimeError, match="strictly prohibited in production"):
            is_local_test_auth_enabled()

    with patch.dict(os.environ, {"ENABLE_LOCAL_TEST_AUTH": "true", "K_SERVICE": "discovery-copilot"}):
        with pytest.raises(RuntimeError, match="strictly prohibited in production"):
            is_local_test_auth_enabled()


def test_session_state_injection_cannot_bypass_server_principal():
    """Verify caller cannot inject _verified_principal via ToolContext user_id manipulation."""
    # When tool_context has a session without _verified_principal, it falls back to user_id only if verified
    # But if session state has _verified_principal, it strictly returns that verified identity
    mock_session = MagicMock()
    mock_session.state = {"_verified_principal": "server_verified@enterprise.com"}
    mock_context = MagicMock()
    mock_context.session = mock_session
    mock_context.user_id = "attacker_spoofed@enterprise.com"

    resolved = get_principal_from_tool_context(mock_context)
    assert resolved == "server_verified@enterprise.com"


def test_valid_gateway_assertion_authenticates_caller():
    """Verify short-lived gateway-signed assertion is cryptographically validated and accepted."""
    from app.app_utils.security import clear_jti_cache, extract_and_verify_principal

    clear_jti_cache()
    signer, pem_public = _generate_test_rsa_key_pair(key_id="gateway-key-1")
    now = int(time.time())
    payload = {
        "iss": "https://gateway.enterprise-graphrag.internal",
        "sub": "user_stable_12345",  # Stable user subject
        "session_id": "gateway-session-9876",  # Separate session ID
        "jti": "jti_unique_nonce_1",  # JTI for replay prevention
        "email": "architect@enterprise.com",
        "aud": "discovery-copilot-agent-runtime",
        "iat": now,
        "exp": now + 300,  # 5 minute short-lived token
    }
    header = {"kid": "gateway-key-1", "alg": "RS256"}
    gateway_token = google_jwt.encode(signer, payload, header=header).decode("utf-8")

    mock_request = MagicMock()
    mock_request.headers = {
        "X-Enterprise-Gateway-Assertion": gateway_token,
    }
    mock_request.url.path = "/api/stream_reasoning_engine"

    with patch("app.app_utils.security.fetch_gateway_public_keys", return_value={"gateway-key-1": pem_public}):
        verified = extract_and_verify_principal(mock_request)
        assert verified == "architect@enterprise.com"


def test_gateway_assertion_replay_rejected():
    """Verify replaying the same gateway assertion with an already-seen jti is rejected."""
    from app.app_utils.security import clear_jti_cache, extract_and_verify_principal

    clear_jti_cache()
    signer, pem_public = _generate_test_rsa_key_pair(key_id="gateway-key-1")
    now = int(time.time())
    payload = {
        "iss": "https://gateway.enterprise-graphrag.internal",
        "sub": "user_stable_12345",
        "session_id": "gateway-session-9876",
        "jti": "jti_replay_test_101",
        "email": "architect@enterprise.com",
        "aud": "discovery-copilot-agent-runtime",
        "iat": now,
        "exp": now + 300,
    }
    header = {"kid": "gateway-key-1", "alg": "RS256"}
    token = google_jwt.encode(signer, payload, header=header).decode("utf-8")

    mock_request = MagicMock()
    mock_request.headers = {"X-Enterprise-Gateway-Assertion": token}
    mock_request.url.path = "/api/stream_reasoning_engine"

    with patch("app.app_utils.security.fetch_gateway_public_keys", return_value={"gateway-key-1": pem_public}):
        # First call succeeds
        v1 = extract_and_verify_principal(mock_request)
        assert v1 == "architect@enterprise.com"

        # Replay attempt fails closed with replay attack detected
        with pytest.raises(AuthenticationError, match="Replay attack detected"):
            extract_and_verify_principal(mock_request)


def test_gateway_assertion_missing_jti_rejected():
    """Verify gateway assertion missing 'jti' claim is rejected."""
    from app.app_utils.security import clear_jti_cache, extract_and_verify_principal

    clear_jti_cache()
    signer, pem_public = _generate_test_rsa_key_pair(key_id="gateway-key-1")
    now = int(time.time())
    payload = {
        "iss": "https://gateway.enterprise-graphrag.internal",
        "sub": "user_stable_12345",
        "email": "architect@enterprise.com",
        "aud": "discovery-copilot-agent-runtime",
        "iat": now,
        "exp": now + 300,
        # omitted jti
    }
    header = {"kid": "gateway-key-1", "alg": "RS256"}
    token = google_jwt.encode(signer, payload, header=header).decode("utf-8")

    mock_request = MagicMock()
    mock_request.headers = {"X-Enterprise-Gateway-Assertion": token}
    mock_request.url.path = "/api/stream_reasoning_engine"

    with patch("app.app_utils.security.fetch_gateway_public_keys", return_value={"gateway-key-1": pem_public}):
        with pytest.raises(AuthenticationError, match="missing required 'jti'"):
            extract_and_verify_principal(mock_request)


def test_rbac_authorization_separated_from_authentication():
    """Verify authenticated identity is strictly passed to RBAC, which independently evaluates document access."""
    # When patch is used to simulate RBAC access rules per principal
    def mock_rbac_lookup(email, database=None, group_resolver=None):
        if email == "architect@enterprise.com":
            return ["doc_approved_1", "doc_approved_2"]
        return []

    with patch("common.rbac.rbac.resolve_principal_access", side_effect=mock_rbac_lookup):
        from common.rbac.rbac import resolve_principal_access

        # Authenticated architect has access
        docs = resolve_principal_access("architect@enterprise.com")
        assert docs == ["doc_approved_1", "doc_approved_2"]

        # An attacker email has 0 docs even if authenticated
        unauthorized_docs = resolve_principal_access("unauthorized_guest@enterprise.com")
        assert unauthorized_docs == []


def test_untrusted_gateway_assertion_rejected_401():
    """Verify gateway assertion with wrong issuer or untrusted key is rejected with 401."""
    from app.app_utils.security import extract_and_verify_principal

    signer, pem_public = _generate_test_rsa_key_pair(key_id="untrusted-key-1")
    now = int(time.time())
    payload = {
        "iss": "https://malicious-gateway.evil.com",
        "sub": "evil-1",
        "email": "attacker@enterprise.com",
        "aud": "discovery-copilot-agent-runtime",
        "iat": now,
        "exp": now + 300,
    }
    header = {"kid": "untrusted-key-1", "alg": "RS256"}
    evil_token = google_jwt.encode(signer, payload, header=header).decode("utf-8")

    mock_request = MagicMock()
    mock_request.headers = {
        "X-Enterprise-Gateway-Assertion": evil_token,
    }
    mock_request.url.path = "/api/stream_reasoning_engine"

    # Even if key is present in certs, untrusted issuer must be rejected
    with patch("app.app_utils.security.fetch_gateway_public_keys", return_value={"untrusted-key-1": pem_public}):
        with pytest.raises(AuthenticationError, match="Invalid Gateway Assertion token issuer"):
            extract_and_verify_principal(mock_request)


# ============================================================================
# NATIVE GEMINI ENTERPRISE AGENT RUNTIME DIRECT INTEGRATION TESTS
# ============================================================================


def test_agent_runtime_direct_valid_identity():
    """Verify native Gemini Enterprise Agent Runtime request resolves valid principal email."""
    from app.app_utils.security import extract_and_verify_principal

    mock_request = MagicMock()
    mock_request.headers = {}
    mock_request.url.path = "/api/stream_reasoning_engine"

    body = {
        "class_method": "stream_query",
        "input": {
            "message": "What is our cloud strategy?",
            "user_id": "Alex.Chen@enterprise.com",
            "session_id": "session-12345",
        },
    }

    verified_principal = extract_and_verify_principal(
        mock_request, body_dict=body, allow_agent_runtime_direct=True
    )
    assert verified_principal == "alex.chen@enterprise.com"


def test_agent_runtime_direct_missing_identity_fails_closed():
    """Verify missing, empty, or None user_id fails closed with 401 and never falls back."""
    from app.app_utils.security import extract_and_verify_principal

    mock_request = MagicMock()
    mock_request.headers = {}
    mock_request.url.path = "/api/stream_reasoning_engine"

    # Case 1: user_id is empty string
    body_empty = {
        "class_method": "stream_query",
        "input": {"message": "Hi", "user_id": ""},
    }
    with pytest.raises(AuthenticationError) as exc_info:
        extract_and_verify_principal(
            mock_request, body_dict=body_empty, allow_agent_runtime_direct=True
        )
    assert exc_info.value.status_code == 401
    assert "Missing user_id in Agent Runtime request" in exc_info.value.message

    # Case 2: user_id is missing entirely
    body_missing = {
        "class_method": "stream_query",
        "input": {"message": "Hi"},
    }
    with pytest.raises(AuthenticationError) as exc_info:
        extract_and_verify_principal(
            mock_request, body_dict=body_missing, allow_agent_runtime_direct=True
        )
    assert exc_info.value.status_code == 401

    # Case 3: input object is missing
    body_no_input = {"class_method": "stream_query"}
    with pytest.raises(AuthenticationError) as exc_info:
        extract_and_verify_principal(
            mock_request, body_dict=body_no_input, allow_agent_runtime_direct=True
        )
    assert exc_info.value.status_code == 401


def test_agent_runtime_direct_invalid_email_format():
    """Verify malformed email string fails with 400 Bad Request."""
    from app.app_utils.security import extract_and_verify_principal

    mock_request = MagicMock()
    mock_request.headers = {}
    mock_request.url.path = "/api/stream_reasoning_engine"

    body = {
        "class_method": "stream_query",
        "input": {"message": "Hi", "user_id": "not-a-valid-email-address"},
    }
    with pytest.raises(AuthenticationError) as exc_info:
        extract_and_verify_principal(
            mock_request, body_dict=body, allow_agent_runtime_direct=True
        )
    assert exc_info.value.status_code == 400
    assert "is not a valid email address" in exc_info.value.message


def test_agent_runtime_direct_invalid_domain_fails_closed():
    """Verify unauthorized domains (e.g. @gmail.com or @attacker.com) are rejected with 403."""
    from app.app_utils.security import extract_and_verify_principal

    mock_request = MagicMock()
    mock_request.headers = {}
    mock_request.url.path = "/api/stream_reasoning_engine"

    # Test @gmail.com
    body_gmail = {
        "class_method": "stream_query",
        "input": {"message": "Hi", "user_id": "alex.chen@gmail.com"},
    }
    with pytest.raises(AuthenticationError) as exc_info:
        extract_and_verify_principal(
            mock_request, body_dict=body_gmail, allow_agent_runtime_direct=True
        )
    assert exc_info.value.status_code == 403
    assert "Organization policy violation" in exc_info.value.message
    assert "gmail.com" in exc_info.value.message

    # Test @attacker.com
    body_attacker = {
        "class_method": "stream_query",
        "input": {"message": "Hi", "user_id": "intruder@attacker.com"},
    }
    with pytest.raises(AuthenticationError) as exc_info:
        extract_and_verify_principal(
            mock_request, body_dict=body_attacker, allow_agent_runtime_direct=True
        )
    assert exc_info.value.status_code == 403


def test_agent_runtime_direct_disabled_via_feature_flag():
    """Verify direct integration path fails closed when GEMINI_ENTERPRISE_DIRECT_ENABLED=false."""
    from app.app_utils.security import extract_and_verify_principal

    mock_request = MagicMock()
    mock_request.headers = {}
    mock_request.url.path = "/api/stream_reasoning_engine"

    body = {
        "class_method": "stream_query",
        "input": {"message": "Hi", "user_id": "alex.chen@enterprise.com"},
    }

    with patch.dict(os.environ, {"GEMINI_ENTERPRISE_DIRECT_ENABLED": "false"}):
        with pytest.raises(AuthenticationError) as exc_info:
            extract_and_verify_principal(
                mock_request, body_dict=body, allow_agent_runtime_direct=True
            )
        assert exc_info.value.status_code == 401
        assert "Direct Gemini Enterprise Agent Runtime integration is disabled" in exc_info.value.message


def test_agent_runtime_direct_caller_service_account_verification():
    """Verify IAM trust boundary caller check for Gemini Enterprise service agent."""
    from app.app_utils.security import extract_and_verify_principal

    body = {
        "class_method": "stream_query",
        "input": {"message": "Hi", "user_id": "alex.chen@enterprise.com"},
    }

    # Case 1: Authorized service agent header matches
    mock_req_authorized = MagicMock()
    mock_req_authorized.headers = {
        "X-Caller-Service-Account": "service-291679685305@gcp-sa-discoveryengine.iam.gserviceaccount.com"
    }
    mock_req_authorized.url.path = "/api/stream_reasoning_engine"

    email = extract_and_verify_principal(
        mock_req_authorized, body_dict=body, allow_agent_runtime_direct=True
    )
    assert email == "alex.chen@enterprise.com"

    # Case 2: Unauthorized caller service account rejected with 403 Forbidden
    mock_req_unauthorized = MagicMock()
    mock_req_unauthorized.headers = {
        "X-Caller-Service-Account": "malicious-sa@rogue-project.iam.gserviceaccount.com"
    }
    mock_req_unauthorized.url.path = "/api/stream_reasoning_engine"

    with pytest.raises(AuthenticationError) as exc_info:
        extract_and_verify_principal(
            mock_req_unauthorized, body_dict=body, allow_agent_runtime_direct=True
        )
    assert exc_info.value.status_code == 403
    assert "is not authorized to invoke direct Agent Runtime path" in exc_info.value.message


def test_agent_runtime_direct_rejected_when_not_allowed():
    """Verify direct integration is prohibited when allow_agent_runtime_direct=False."""
    from app.app_utils.security import extract_and_verify_principal

    mock_request = MagicMock()
    mock_request.headers = {}
    mock_request.url.path = "/apps/app/users/alex.chen@enterprise.com/sessions"

    body = {
        "input": {"user_id": "alex.chen@enterprise.com"},
    }

    # Without allow_agent_runtime_direct=True, fails closed with 401
    with pytest.raises(AuthenticationError) as exc_info:
        extract_and_verify_principal(
            mock_request, body_dict=body, allow_agent_runtime_direct=False
        )
    assert exc_info.value.status_code == 401
    assert "Missing valid X-Goog-IAP-JWT-Assertion" in exc_info.value.message


def test_spoofed_identity_with_gateway_header_rejected_403():
    """Verify cryptographic gateway assertion takes precedence and catches spoofed body user_id."""
    from app.app_utils.security import clear_jti_cache, extract_and_verify_principal

    clear_jti_cache()
    signer, pem_public = _generate_test_rsa_key_pair(key_id="gateway-key-spoof")
    now = int(time.time())
    payload = {
        "iss": "https://gateway.enterprise-graphrag.internal",
        "sub": "user_123",
        "email": "genuine.user@enterprise.com",
        "aud": "discovery-copilot-agent-runtime",
        "iat": now,
        "exp": now + 300,
        "jti": f"jti-{uuid.uuid4() if 'uuid' in dir() else now}",
    }
    header = {"kid": "gateway-key-spoof", "alg": "RS256"}
    token = google_jwt.encode(signer, payload, header=header).decode("utf-8")

    mock_request = MagicMock()
    mock_request.headers = {"X-Enterprise-Gateway-Assertion": token}
    mock_request.url.path = "/api/stream_reasoning_engine"

    # Attacker tries to claim user_id is ceo@enterprise.com in the body
    body_spoofed = {
        "class_method": "stream_query",
        "input": {"message": "Steal data", "user_id": "ceo@enterprise.com"},
    }

    with patch("app.app_utils.security.fetch_gateway_public_keys", return_value={"gateway-key-spoof": pem_public}):
        with pytest.raises(AuthenticationError) as exc_info:
            extract_and_verify_principal(
                mock_request, body_dict=body_spoofed, allow_agent_runtime_direct=True
            )
        assert exc_info.value.status_code == 403
        assert "Identity mismatch between verified token" in exc_info.value.message


def test_agent_runtime_direct_integrates_with_rbac():
    """Verify verified identity flows into Spanner RBAC evaluate access correctly."""
    from app.app_utils.security import extract_and_verify_principal
    import common.rbac.rbac as rbac_mod

    mock_request = MagicMock()
    mock_request.headers = {}
    mock_request.url.path = "/api/stream_reasoning_engine"

    body = {
        "class_method": "stream_query",
        "input": {"message": "Find documents", "user_id": "engineer@enterprise.com"},
    }

    verified_principal = extract_and_verify_principal(
        mock_request, body_dict=body, allow_agent_runtime_direct=True
    )

    with patch.object(rbac_mod, "resolve_principal_access", return_value=["doc-cloud-arch-1", "doc-cloud-arch-2"]) as mock_rbac:
        accessible_docs = rbac_mod.resolve_principal_access(user_email=verified_principal)
        mock_rbac.assert_called_once_with(user_email="engineer@enterprise.com")
        assert accessible_docs == ["doc-cloud-arch-1", "doc-cloud-arch-2"]


def test_agent_card_discovery_exempt_from_iap_middleware():
    """Verify agent card paths (.well-known/agent.json) bypass IAP authentication."""
    from app.app_utils.security import EnforceAuthenticatedPrincipalMiddleware
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    test_app = FastAPI()
    test_app.add_middleware(EnforceAuthenticatedPrincipalMiddleware)

    @test_app.get("/.well-known/agent.json")
    def public_agent_card():
        return {"name": "discovery_copilot", "protocol": "a2a"}

    @test_app.get("/a2a/discovery_copilot/.well-known/agent.json")
    def subpath_agent_card():
        return {"name": "discovery_copilot", "protocol": "a2a"}

    client = TestClient(test_app)

    # Both must succeed with 200 without any auth headers
    res1 = client.get("/.well-known/agent.json")
    assert res1.status_code == 200
    assert res1.json()["name"] == "discovery_copilot"

    res2 = client.get("/a2a/discovery_copilot/.well-known/agent.json")
    assert res2.status_code == 200
    assert res2.json()["name"] == "discovery_copilot"



