"""ADK-compatible Discovery Toolset and Hybrid Retrieval Bridge (Phase 4.1).

Integrates:
1. Approval-aware retrieval (DiscoveryRetrievalService)
2. Hybrid search:
   - Vector search on EvidenceChunks (COSINE_DISTANCE <= threshold)
   - Linking vector-matched chunks to parent approved SolutionCards via EvidenceLinks
   - GQL multi-hop graph traversal on EnterpriseKnowledgeGraph
   - Full-text SQL filtering
3. Deduplication & MMR diversity reranking
4. Strict RBAC principal access filtering
5. Principal identity extraction from ToolContext (never trusting model-generated identity)
6. Grounded citation and provenance metadata assembly
"""

from dataclasses import dataclass
import json
import logging
from typing import Any

from google.adk.tools import ToolContext
from google.cloud import spanner
import numpy as np

from common.config import settings
from common.database.review_router import get_spanner_database
from common.embeddings.vertex_client import VertexEmbeddingsClient
from common.models.schema import VerificationStatus
from common.search.discovery_service import (
    ApprovedSolutionCard,
    DiscoveryRetrievalService,
    GroundedCitation,
    TRUSTED_STATUS,
)
from common.search.mmr import compute_mmr

logger = logging.getLogger("discovery_copilot.tools")


# ============================================================================
# PRINCIPAL IDENTITY RESOLUTION
# ============================================================================


def get_principal_from_tool_context(tool_context: ToolContext | None) -> str:
    """Extracts authenticated principal email from the trusted ADK ToolContext.

    In production/IAP, user identity is propagated into the ADK Session user_id.
    Never accepts client-supplied or model-generated email strings.
    Fails closed if authentication context is missing or unauthenticated.
    """
    if tool_context is not None:
        # Check session state for verified principal if present
        session = getattr(tool_context, "session", None)
        if session and hasattr(session, "state") and isinstance(session.state, dict):
            verified_principal = session.state.get("_verified_principal")
            if verified_principal and isinstance(verified_principal, str):
                return verified_principal.lower()

        if hasattr(tool_context, "user_id") and tool_context.user_id:
            return tool_context.user_id.lower()
        if session and hasattr(session, "user_id") and session.user_id:
            return session.user_id.lower()

    # Fail closed: No implicit identity fallback allowed under strict security policy
    raise PermissionError("Authentication required: Missing trusted user identity in ToolContext")


# ============================================================================
# HYBRID RETRIEVAL BRIDGE
# ============================================================================


class HybridDiscoveryBridge:
    """Orchestrates hybrid vector-graph retrieval and MMR reranking for Discovery Copilot."""

    def __init__(
        self,
        database=None,
        retrieval_service: DiscoveryRetrievalService | None = None,
        embedding_client: VertexEmbeddingsClient | None = None,
    ):
        self._database = database or get_spanner_database()
        self._retrieval_service = retrieval_service or DiscoveryRetrievalService(database=self._database)
        self._embedding_client = embedding_client

    def search_approved_hybrid(
        self,
        query: str,
        principal_email: str,
        technologies: list[str] | None = None,
        top_k: int = 5,
        lambda_mmr: float = 0.7,
    ) -> list[dict[str, Any]]:
        """Executes hybrid retrieval: vector similarity + SQL filters + GQL graph traversal.

        Steps:
        1. Query approved solutions via DiscoveryRetrievalService (SQL + RBAC + VerificationStatus='APPROVED').
        2. Vector search: search EvidenceChunks via COSINE_DISTANCE, and join to approved SolutionCards via EvidenceLinks.
        3. Merge candidates, deduplicate by card_id.
        4. Optional GQL traversal to enrich tech and customer relationships.
        5. Apply MMR reranking if vector embeddings are available.
        """
        # Step 1: SQL/Taxonomy approved search
        tech_id = technologies[0] if technologies else None
        keyword = query if len(query.split()) <= 6 else None

        sql_candidates = self._retrieval_service.search_approved_solutions(
            principal_email=principal_email,
            keyword=keyword,
            tech_id=tech_id,
            limit=top_k * 2,
        )

        candidates_by_id: dict[str, ApprovedSolutionCard] = {c.card_id: c for c in sql_candidates}

        # Step 2: Vector Search & Chunk Linking (if embedding client provided)
        query_vec = None
        if self._embedding_client and query.strip():
            try:
                query_vec = self._embedding_client.get_embedding(query)
            except Exception as e:
                logger.warning("Vertex AI query embedding failed, continuing with graph/SQL: %s", e)

        if query_vec is not None:
            vector_linked_cards = self._search_vector_evidence_linked(
                query_vec=query_vec,
                principal_email=principal_email,
                limit=top_k * 2,
            )
            for card in vector_linked_cards:
                if card.card_id not in candidates_by_id:
                    candidates_by_id[card.card_id] = card
                else:
                    # Merge citations
                    existing_ev_ids = {cit.evidence_id for cit in candidates_by_id[card.card_id].citations}
                    for cit in card.citations:
                        if cit.evidence_id not in existing_ev_ids:
                            candidates_by_id[card.card_id].citations.append(cit)

        # Step 3: Graph Traversal enrichment (GQL)
        if technologies:
            for t_name in technologies:
                try:
                    graph_cards = self._retrieval_service.traverse_approved_solutions_graph(
                        principal_email=principal_email,
                        tech_name=t_name,
                        limit=top_k,
                    )
                    for gc in graph_cards:
                        if gc.card_id not in candidates_by_id:
                            candidates_by_id[gc.card_id] = gc
                        else:
                            # Union technologies
                            combined_techs = set(candidates_by_id[gc.card_id].technologies).union(gc.technologies)
                            candidates_by_id[gc.card_id].technologies = sorted(list(combined_techs))
                except Exception as e:
                    logger.warning("GQL graph traversal exception: %s", e)

        candidate_list = list(candidates_by_id.values())
        if not candidate_list:
            return []

        # Step 4 (Strict Step 6 Revalidation): Revalidate approval and document access on merged candidates
        revalidated_candidates = []
        from common.rbac import rbac
        current_authorized_docs = rbac.resolve_principal_access(
            user_email=principal_email, database=self._database
        )

        for c in candidate_list:
            # Must strictly be APPROVED
            if c.verification_status != TRUSTED_STATUS:
                logger.warning("Merged candidate '%s' dropped: status '%s' != '%s'", c.card_id, c.verification_status, TRUSTED_STATUS)
                continue
            # Must strictly be within authorized documents
            if current_authorized_docs is not None and c.document_id not in current_authorized_docs:
                logger.warning("Merged candidate '%s' dropped: unauthorized document '%s'", c.card_id, c.document_id)
                continue
            # Valid citations check: filter any citations from unauthorized documents
            if current_authorized_docs is not None:
                c.citations = [cit for cit in c.citations if cit.document_id in current_authorized_docs]
            revalidated_candidates.append(c)

        if not revalidated_candidates:
            return []

        # Step 5 (Strict Step 7): MMR Reranking
        final_cards = revalidated_candidates
        if query_vec is not None and len(revalidated_candidates) > 1 and self._embedding_client:
            try:
                card_texts = [
                    f"{c.project_name} {c.business_problem or ''} {c.proposed_solution or ''} {c.implemented_solution or ''}"
                    for c in revalidated_candidates
                ]
                card_embeddings = np.array(self._embedding_client.get_embeddings(card_texts))
                final_cards = compute_mmr(
                    query_embedding=np.array(query_vec),
                    candidate_embeddings=card_embeddings,
                    candidate_items=revalidated_candidates,
                    lambda_param=lambda_mmr,
                    top_k=top_k,
                )
            except Exception as e:
                logger.warning("MMR reranking fallback to score order: %s", e)
                final_cards = revalidated_candidates[:top_k]
        else:
            final_cards = revalidated_candidates[:top_k]

        # Step 5: Convert to structured dictionary candidates
        return [
            {
                "card_id": c.card_id,
                "document_id": c.document_id,
                "document_name": c.document_name,
                "project_name": c.project_name,
                "business_problem": c.business_problem,
                "proposed_solution": c.proposed_solution,
                "implemented_solution": c.implemented_solution,
                "outcome": c.outcome,
                "verification_status": c.verification_status,
                "okf_uri": c.okf_uri,
                "technologies": c.technologies,
                "citations": [
                    {
                        "evidence_id": cit.evidence_id,
                        "chunk_id": cit.chunk_id,
                        "document_id": cit.document_id,
                        "document_name": cit.document_name,
                        "page_number": cit.page_number,
                        "slide_number": cit.slide_number,
                        "modality": cit.modality,
                        "source_chunk_text": cit.source_chunk_text,
                        "note": cit.note,
                    }
                    for cit in c.citations
                ],
            }
            for c in final_cards
        ]

    def _search_vector_evidence_linked(
        self,
        query_vec: list[float],
        principal_email: str,
        limit: int = 5,
    ) -> list[ApprovedSolutionCard]:
        """Finds EvidenceChunks matching vector similarity, joins to EvidenceLinks,

        and loads parent SolutionCards strictly enforcing VerificationStatus = 'APPROVED'.
        """
        from common.rbac import rbac

        authorized_docs = rbac.resolve_principal_access(
            user_email=principal_email, database=self._database
        )

        with self._database.snapshot() as snapshot:
            # Query COSINE_DISTANCE on EvidenceChunks.Embedding768 joined to EvidenceLinks and SolutionCards
            vector_sql = """
                SELECT sc.CardId, sc.DocumentId, d.FileName, sc.ProjectName, sc.BusinessProblem,
                       sc.ProposedSolution, sc.ImplementedSolution, sc.Outcome, sc.VerificationStatus, sc.OkfUri,
                       el.EvidenceId, el.ChunkId, el.PageNumber, el.SlideNumber, ec.Modality, ec.TextContent, el.Note,
                       COSINE_DISTANCE(ec.Embedding768, @query_vec) AS distance
                FROM EvidenceChunks ec
                JOIN EvidenceLinks el ON ec.ChunkId = el.ChunkId
                JOIN SolutionCards sc ON el.CardId = sc.CardId
                JOIN Documents d ON sc.DocumentId = d.DocumentId
                WHERE sc.VerificationStatus = @approved_status
                  AND ec.Embedding768 IS NOT NULL
            """
            params: dict[str, Any] = {
                "approved_status": TRUSTED_STATUS,
                "query_vec": query_vec,
            }
            param_types: dict[str, Any] = {
                "approved_status": spanner.param_types.STRING,
                "query_vec": spanner.param_types.Array(spanner.param_types.FLOAT32),
            }

            if authorized_docs is not None:
                vector_sql += " AND sc.DocumentId IN UNNEST(@authorized_docs)"
                params["authorized_docs"] = authorized_docs
                param_types["authorized_docs"] = spanner.param_types.Array(spanner.param_types.STRING)

            vector_sql += " ORDER BY distance ASC LIMIT @limit"
            params["limit"] = limit
            param_types["limit"] = spanner.param_types.INT64

            rows = list(snapshot.execute_sql(vector_sql, params=params, param_types=param_types))
            cards_map: dict[str, ApprovedSolutionCard] = {}
            for r in rows:
                cid = r[0]
                if cid not in cards_map:
                    cards_map[cid] = ApprovedSolutionCard(
                        card_id=cid,
                        document_id=r[1],
                        document_name=r[2],
                        project_name=r[3] or "Untitled Solution",
                        business_problem=r[4],
                        proposed_solution=r[5],
                        implemented_solution=r[6],
                        outcome=r[7],
                        verification_status=r[8],
                        okf_uri=r[9],
                        technologies=[],
                        citations=[],
                    )

                # Add citation
                cards_map[cid].citations.append(
                    GroundedCitation(
                        evidence_id=r[10],
                        chunk_id=r[11],
                        document_id=r[1],
                        document_name=r[2],
                        page_number=r[12],
                        slide_number=r[13],
                        modality=r[14] or "TEXT",
                        source_chunk_text=r[15],
                        note=r[16],
                    )
                )

            return list(cards_map.values())


# ============================================================================
# ADK TOOLS (EXPOSED TO GEMINI AGENT)
# ============================================================================

_bridge_instance: HybridDiscoveryBridge | None = None


def get_bridge() -> HybridDiscoveryBridge:
    global _bridge_instance
    if _bridge_instance is None:
        _bridge_instance = HybridDiscoveryBridge()
    return _bridge_instance


def search_approved_solutions(
    query: str,
    technologies: list[str] | None = None,
    tool_context: ToolContext | None = None,
) -> str:
    """Searches verified, human-approved customer solutions in the enterprise knowledge graph.

    Args:
        query: Natural language question or architectural keywords (e.g. 'sub-10ms latency banking architecture').
        technologies: Optional list of relevant technology names (e.g. ['Cloud Spanner', 'Vertex AI']).
        tool_context: Trusted ADK execution context holding authenticated user identity.

    Returns:
        JSON string containing verified solutions, exact grounded citations, page/slide references, and provenance.
    """
    principal_email = get_principal_from_tool_context(tool_context)
    bridge = get_bridge()
    results = bridge.search_approved_hybrid(
        query=query,
        principal_email=principal_email,
        technologies=technologies,
        top_k=5,
    )
    if not results:
        return json.dumps(
            {
                "status": "empty",
                "message": "No human-approved solutions found matching query and security permissions.",
                "solutions": [],
            }
        )

    return json.dumps(
        {
            "status": "success",
            "count": len(results),
            "solutions": results,
        },
        indent=2,
    )


def traverse_technology_graph(
    technology_name: str,
    tool_context: ToolContext | None = None,
) -> str:
    """Traverses the EnterpriseKnowledgeGraph to discover verified solutions supporting a technology.

    Args:
        technology_name: Name of the technology to inspect (e.g. 'Cloud Spanner').
        tool_context: Trusted ADK execution context holding authenticated user identity.

    Returns:
        JSON string listing approved solutions connected to the technology via GQL SUPPORTS edges.
    """
    principal_email = get_principal_from_tool_context(tool_context)
    bridge = get_bridge()
    cards = bridge._retrieval_service.traverse_approved_solutions_graph(
        principal_email=principal_email,
        tech_name=technology_name,
        limit=5,
    )
    if not cards:
        return json.dumps(
            {
                "status": "empty",
                "message": f"No approved solutions found in graph connected to '{technology_name}'.",
                "solutions": [],
            }
        )

    return json.dumps(
        {
            "status": "success",
            "technology": technology_name,
            "solutions": [c.model_dump() for c in cards],
        },
        indent=2,
    )


def get_solution_evidence_detail(
    card_id: str,
    tool_context: ToolContext | None = None,
) -> str:
    """Retrieves verbatim source citations and slide/page grounding for an approved SolutionCard.

    Args:
        card_id: Unique identifier of the SolutionCard.
        tool_context: Trusted ADK execution context holding authenticated user identity.

    Returns:
        JSON string containing citations, chunk text, and sidecar provenance.
    """
    principal_email = get_principal_from_tool_context(tool_context)
    bridge = get_bridge()
    cards = bridge._retrieval_service.search_approved_solutions(
        principal_email=principal_email,
        limit=10,
    )
    matched = next((c for c in cards if c.card_id == card_id), None)
    if not matched:
        return json.dumps(
            {
                "status": "error",
                "message": f"Approved SolutionCard '{card_id}' not found or access denied.",
            }
        )

    return json.dumps(
        {
            "status": "success",
            "card_id": matched.card_id,
            "project_name": matched.project_name,
            "verification_status": matched.verification_status,
            "okf_uri": matched.okf_uri,
            "citations": [c.model_dump() for c in matched.citations],
        },
        indent=2,
    )

