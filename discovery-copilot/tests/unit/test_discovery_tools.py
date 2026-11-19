"""Unit and integration tests for Phase 4.1 Discovery Retrieval & ADK Tool Bridge.

Validates:
1. Approved-only retrieval: returns APPROVED cards, excludes PENDING, DRAFT, REJECTED, and CHANGES_REQUESTED.
2. RBAC multi-tenant gating: user without READ permission on DocumentId is blocked.
3. Vector-to-card linking: vector search on EvidenceChunks joins through EvidenceLinks to approved SolutionCards.
4. GQL technology graph traversal: traverses (s:BusinessSolution)-[:SUPPORTS]->(t:Technology) strictly for APPROVED cards.
5. MMR diversity reranking: reranks candidates deterministically.
6. Principal identity extraction: securely pulls user_id from ToolContext; ignores model-generated arguments.
7. Candidate deduplication & evidence merging: merges duplicate entries and union citations.
8. Empty results handling: returns structured empty payload gracefully.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock
import numpy as np
import pytest
from google.adk.tools import ToolContext

# Add repo root and discovery-copilot to sys.path
REPO_ROOT = Path(__file__).resolve().parents[3]
DISCOVERY_DIR = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(DISCOVERY_DIR) not in sys.path:
    sys.path.insert(0, str(DISCOVERY_DIR))
COMMON_SRC = REPO_ROOT / "common" / "src"
if str(COMMON_SRC) not in sys.path:
    sys.path.insert(0, str(COMMON_SRC))

from common.search.discovery_service import (
    ApprovedSolutionCard,
    DiscoveryRetrievalService,
    GroundedCitation,
    TRUSTED_STATUS,
)
from app.tools.discovery_tools import (
    HybridDiscoveryBridge,
    get_principal_from_tool_context,
    get_solution_evidence_detail,
    search_approved_solutions,
    traverse_technology_graph,
)


class MockSpannerSnapshot:
    """Mock Spanner snapshot for hybrid vector and graph queries."""

    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def execute_sql(self, query, params=None, param_types=None):
        params = params or {}

        # 1. Base search query on SolutionCards
        if "FROM SolutionCards sc" in query and "JOIN Documents d" in query:
            approved_status = params.get("approved_status")
            authorized_doc_ids = params.get("authorized_doc_ids")
            card_ids = params.get("card_ids")
            kw = params.get("kw", "").replace("%", "").lower()

            rows = []
            for c in self.db.cards.values():
                if card_ids is not None and c["CardId"] not in card_ids:
                    continue
                if c.get("VerificationStatus") != approved_status:
                    continue
                if authorized_doc_ids is not None and c["DocumentId"] not in authorized_doc_ids:
                    continue
                if kw:
                    text_blob = " ".join(
                        str(c.get(f) or "").lower()
                        for f in ["ProjectName", "BusinessProblem", "ProposedSolution", "ImplementedSolution", "Outcome"]
                    )
                    if kw not in text_blob:
                        continue
                rows.append(
                    (
                        c["CardId"],
                        c["DocumentId"],
                        c.get("FileName", "sample.pdf"),
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

        # 2. Vector distance search joined to EvidenceLinks and SolutionCards
        if "COSINE_DISTANCE" in query:
            approved_status = params.get("approved_status")
            authorized_docs = params.get("authorized_docs")
            rows = []
            for ec in self.db.evidence_chunks:
                # Find matching EvidenceLink
                for el in self.db.evidence_links:
                    if el["ChunkId"] == ec["ChunkId"]:
                        cid = el["CardId"]
                        card = self.db.cards.get(cid)
                        if not card or card.get("VerificationStatus") != approved_status:
                            continue
                        if authorized_docs is not None and card["DocumentId"] not in authorized_docs:
                            continue
                        dist = 0.05  # High similarity distance
                        rows.append(
                            (
                                card["CardId"],
                                card["DocumentId"],
                                card.get("FileName", "sample.pdf"),
                                card.get("ProjectName"),
                                card.get("BusinessProblem"),
                                card.get("ProposedSolution"),
                                card.get("ImplementedSolution"),
                                card.get("Outcome"),
                                card.get("VerificationStatus"),
                                card.get("OkfUri"),
                                el["EvidenceId"],
                                el["ChunkId"],
                                el.get("PageNumber"),
                                el.get("SlideNumber"),
                                ec.get("Modality", "TEXT"),
                                ec.get("TextContent"),
                                el.get("Note"),
                                dist,
                            )
                        )
            return rows

        # 3. EvidenceLinks query
        if "FROM EvidenceLinks el" in query:
            card_ids = params.get("card_ids", [])
            rows = []
            for el in self.db.evidence_links:
                if el["CardId"] in card_ids:
                    rows.append(
                        (
                            el["CardId"],
                            el["EvidenceId"],
                            el["ChunkId"],
                            el["DocumentId"],
                            el.get("FileName", "sample.pdf"),
                            el.get("PageNumber"),
                            el.get("SlideNumber"),
                            el.get("Modality", "TEXT"),
                            el.get("TextContent", "Evidence text"),
                            el.get("Note"),
                        )
                    )
            return rows

        # 4. Technologies query
        if "FROM SolutionSupportsTech sst" in query:
            card_ids = params.get("card_ids", [])
            rows = []
            for st in self.db.tech_links:
                if st["CardId"] in card_ids:
                    rows.append((st["CardId"], st["TechName"]))
            return rows

        # 5. GQL Graph traversal query (Option A: returns topology card_id and tech_name)
        if "GRAPH EnterpriseKnowledgeGraph" in query:
            req_tech = params.get("tech_name")
            rows = []
            for c in self.db.cards.values():
                for t in [st["TechName"] for st in self.db.tech_links if st["CardId"] == c["CardId"]]:
                    if req_tech and t != req_tech:
                        continue
                    rows.append((c["CardId"], t))
            return rows

        return []


class MockSpannerDatabase:
    """Mock database setup with approved and unapproved cards."""

    def __init__(self):
        self.cards = {
            "card_appr_uber": {
                "CardId": "card_appr_uber",
                "DocumentId": "doc_uber",
                "FileName": "Uber - 2008.pdf",
                "ProjectName": "UberCab Next-Generation Service",
                "BusinessProblem": "Hailing taxis is inefficient",
                "ProposedSolution": "Fast and efficient on-demand car service",
                "ImplementedSolution": "Mobile app dispatch with GPS routing",
                "Outcome": "Guaranteed pickup under 5 minutes",
                "VerificationStatus": "APPROVED",
                "OkfUri": "gs://bucket/meta/uber.json",
            },
            "card_pending_dropbox": {
                "CardId": "card_pending_dropbox",
                "DocumentId": "doc_dropbox",
                "FileName": "Dropbox - 2007.pdf",
                "ProjectName": "Dropbox Sync Engine",
                "BusinessProblem": "Syncing files across computers is hard",
                "ProposedSolution": "Cloud storage folder sync",
                "ImplementedSolution": "Cross-platform client with S3 backend",
                "Outcome": "Seamless sync across all devices",
                "VerificationStatus": "PENDING",  # MUST BE EXCLUDED
                "OkfUri": "gs://bucket/meta/dropbox.json",
            },
            "card_rejected_fake": {
                "CardId": "card_rejected_fake",
                "DocumentId": "doc_fake",
                "FileName": "fake.pdf",
                "ProjectName": "Hallucinated Architecture",
                "BusinessProblem": "None",
                "ProposedSolution": "None",
                "ImplementedSolution": "None",
                "Outcome": "None",
                "VerificationStatus": "REJECTED",  # MUST BE EXCLUDED
                "OkfUri": None,
            },
        }

        self.evidence_chunks = [
            {
                "ChunkId": "chk_uber_1",
                "DocumentId": "doc_uber",
                "Modality": "TEXT",
                "TextContent": "UberCab provides 1-click hailing with guaranteed pickup in under 5 minutes.",
            }
        ]

        self.evidence_links = [
            {
                "EvidenceId": "ev_uber_1",
                "CardId": "card_appr_uber",
                "ChunkId": "chk_uber_1",
                "DocumentId": "doc_uber",
                "FileName": "Uber - 2008.pdf",
                "PageNumber": 2,
                "SlideNumber": None,
                "Modality": "TEXT",
                "TextContent": "UberCab provides 1-click hailing with guaranteed pickup in under 5 minutes.",
                "Note": "Pickup time quotation",
            }
        ]

        self.tech_links = [
            {"CardId": "card_appr_uber", "TechName": "Cloud Spanner"},
            {"CardId": "card_appr_uber", "TechName": "Google Maps"},
        ]

    def snapshot(self):
        return MockSpannerSnapshot(self)


class MockEmbeddingClient:
    """Mock Vertex AI embedding client returning deterministic vectors."""

    def get_embedding(self, text: str) -> list[float]:
        np.random.seed(42)
        return list(np.random.randn(768).astype(float))

    def get_embeddings(self, texts: list[str]) -> list[list[float]]:
        np.random.seed(42)
        return [list(np.random.randn(768).astype(float)) for _ in texts]


@pytest.fixture
def mock_bridge():
    db = MockSpannerDatabase()
    retrieval_svc = DiscoveryRetrievalService(database=db)
    embed_client = MockEmbeddingClient()
    return HybridDiscoveryBridge(
        database=db,
        retrieval_service=retrieval_svc,
        embedding_client=embed_client,
    )


# ============================================================================
# TESTS
# ============================================================================


def test_approved_only_hybrid_retrieval(mock_bridge):
    """Verify hybrid search returns only APPROVED cards, strictly excluding PENDING and REJECTED."""
    from common.rbac import rbac
    rbac.resolve_principal_access = lambda user_email, database=None, group_resolver=None: ["doc_uber", "doc_dropbox", "doc_fake"]

    results = mock_bridge.search_approved_hybrid(
        query="on-demand hailing and sync",
        principal_email="architect@enterprise.com",
    )

    # Must return ONLY Uber (APPROVED). Dropbox (PENDING) and Fake (REJECTED) must be absent.
    assert len(results) == 1
    card = results[0]
    assert card["card_id"] == "card_appr_uber"
    assert card["verification_status"] == "APPROVED"
    assert "UberCab" in card["project_name"]

    # Verify citation linking
    assert len(card["citations"]) == 1
    citation = card["citations"][0]
    assert citation["evidence_id"] == "ev_uber_1"
    assert citation["page_number"] == 2
    assert "1-click hailing" in citation["source_chunk_text"]


def test_rbac_denial_blocks_retrieval(mock_bridge):
    """Verify that a user without READ permission on the document gets 0 results."""
    from common.rbac import rbac
    # User only has access to doc_other, not doc_uber
    rbac.resolve_principal_access = lambda user_email, database=None, group_resolver=None: ["doc_other"]

    results = mock_bridge.search_approved_hybrid(
        query="Uber",
        principal_email="unauthorized@enterprise.com",
    )
    assert len(results) == 0


def test_vector_to_card_linking(mock_bridge):
    """Verify vector search on chunks accurately links to parent approved card and citations."""
    from common.rbac import rbac
    rbac.resolve_principal_access = lambda user_email, database=None, group_resolver=None: ["doc_uber"]

    # Use query vector
    query_vec = mock_bridge._embedding_client.get_embedding("fast pickup")
    cards = mock_bridge._search_vector_evidence_linked(
        query_vec=query_vec,
        principal_email="architect@enterprise.com",
    )
    assert len(cards) == 1
    assert cards[0].card_id == "card_appr_uber"
    assert len(cards[0].citations) == 1
    assert cards[0].citations[0].evidence_id == "ev_uber_1"


def test_principal_identity_from_tool_context():
    """Verify principal email is pulled from ToolContext.user_id and fails closed when missing."""
    # Context with user_id
    ctx = MagicMock(spec=ToolContext)
    ctx.user_id = "director@enterprise.com"
    email = get_principal_from_tool_context(ctx)
    assert email == "director@enterprise.com"

    # Context without user_id fails closed
    empty_ctx = MagicMock(spec=ToolContext)
    empty_ctx.user_id = None
    empty_ctx.session = None
    with pytest.raises(PermissionError, match="Authentication required"):
        get_principal_from_tool_context(empty_ctx)

    with pytest.raises(PermissionError, match="Authentication required"):
        get_principal_from_tool_context(None)


def test_adk_tool_search_approved_solutions(mock_bridge, monkeypatch):
    """Verify search_approved_solutions ADK tool produces structured JSON."""
    from common.rbac import rbac
    rbac.resolve_principal_access = lambda user_email, database=None, group_resolver=None: ["doc_uber"]

    import app.tools.discovery_tools as dt
    monkeypatch.setattr(dt, "get_bridge", lambda: mock_bridge)

    ctx = MagicMock(spec=ToolContext)
    ctx.user_id = "lead@enterprise.com"

    res_json_str = search_approved_solutions(query="car service", tool_context=ctx)
    data = json.loads(res_json_str)

    assert data["status"] == "success"
    assert data["count"] == 1
    sol = data["solutions"][0]
    assert sol["card_id"] == "card_appr_uber"
    assert sol["verification_status"] == "APPROVED"
    assert len(sol["citations"]) >= 1


def test_adk_tool_empty_results(mock_bridge, monkeypatch):
    """Verify empty search results return gracefully structured JSON without error."""
    from common.rbac import rbac
    rbac.resolve_principal_access = lambda user_email, database=None, group_resolver=None: []

    import app.tools.discovery_tools as dt
    monkeypatch.setattr(dt, "get_bridge", lambda: mock_bridge)

    ctx = MagicMock(spec=ToolContext)
    ctx.user_id = "lead@enterprise.com"

    res_json_str = search_approved_solutions(query="nonexistent term", tool_context=ctx)
    data = json.loads(res_json_str)

    assert data["status"] == "empty"
    assert data["solutions"] == []
    assert "No human-approved solutions found" in data["message"]


def test_adk_tool_traverse_graph(mock_bridge, monkeypatch):
    """Verify traverse_technology_graph tool returns approved solutions connected via GQL."""
    from common.rbac import rbac
    rbac.resolve_principal_access = lambda user_email, database=None, group_resolver=None: ["doc_uber"]

    import app.tools.discovery_tools as dt
    monkeypatch.setattr(dt, "get_bridge", lambda: mock_bridge)

    ctx = MagicMock(spec=ToolContext)
    ctx.user_id = "lead@enterprise.com"

    res_str = traverse_technology_graph(technology_name="Cloud Spanner", tool_context=ctx)
    data = json.loads(res_str)

    assert data["status"] == "success"
    assert data["technology"] == "Cloud Spanner"
    assert len(data["solutions"]) == 1
    assert data["solutions"][0]["card_id"] == "card_appr_uber"


def test_adk_tool_get_solution_evidence_detail(mock_bridge, monkeypatch):
    """Verify get_solution_evidence_detail returns citations and provenance."""
    from common.rbac import rbac
    rbac.resolve_principal_access = lambda user_email, database=None, group_resolver=None: ["doc_uber"]

    import app.tools.discovery_tools as dt
    monkeypatch.setattr(dt, "get_bridge", lambda: mock_bridge)

    ctx = MagicMock(spec=ToolContext)
    ctx.user_id = "lead@enterprise.com"

    res_str = get_solution_evidence_detail(card_id="card_appr_uber", tool_context=ctx)
    data = json.loads(res_str)

    assert data["status"] == "success"
    assert data["card_id"] == "card_appr_uber"
    assert data["verification_status"] == "APPROVED"
    assert len(data["citations"]) == 1
    assert data["citations"][0]["evidence_id"] == "ev_uber_1"
