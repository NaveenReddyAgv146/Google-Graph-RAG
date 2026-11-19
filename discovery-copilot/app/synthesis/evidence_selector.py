"""Evidence Selector with Adaptive MMR and Provenance Protection (Phase 4.3).

Aggregates subtask outputs from Phase 4.2 query orchestration, deduplicates
cards and chunks, applies intent-adaptive MMR reranking, and constructs
the authoritative EvidencePack containing strictly APPROVED, authorized knowledge.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

from common.search.mmr import compute_mmr
from app.planning.models import QueryIntent
from app.synthesis.models import (
    AuthoritativeCardEvidence,
    AuthoritativeEvidenceChunk,
    EvidencePack,
)

logger = logging.getLogger("discovery_copilot.synthesis.evidence_selector")

# Intent-specific MMR diversity-relevance tradeoff parameters
INTENT_MMR_LAMBDAS: dict[QueryIntent, float] = {
    QueryIntent.COMPARISON: 0.45,               # Prioritize diversity across contrasting solutions
    QueryIntent.SOLUTION_DISCOVERY: 0.70,       # Balanced discovery
    QueryIntent.TECHNICAL_LOOKUP: 0.85,         # High relevance to the requested technology
    QueryIntent.ARCHITECTURE_EXPLORATION: 0.60, # Moderate diversity to capture multi-component topology
    QueryIntent.EVIDENCE_VERIFICATION: 0.90,    # Maximum relevance to target card/claim
    QueryIntent.FOLLOW_UP: 0.75,                # Contextually anchored relevance
    QueryIntent.CLARIFICATION_NEEDED: 1.0,      # No retrieval expected
}

DEFAULT_MMR_LAMBDA = 0.70


class EvidenceSelector:
    """Selects, deduplicates, and re-ranks multi-task retrieval results into an EvidencePack."""

    def __init__(
        self,
        embedding_client: Any | None = None,
        default_top_k: int = 5,
    ) -> None:
        self._embedding_client = embedding_client
        self._default_top_k = default_top_k

    def get_adaptive_lambda(self, intent: QueryIntent | str) -> float:
        """Returns the tuned MMR lambda parameter for the specified intent."""
        if isinstance(intent, str):
            try:
                intent_enum = QueryIntent(intent)
            except ValueError:
                return DEFAULT_MMR_LAMBDA
        else:
            intent_enum = intent
        return INTENT_MMR_LAMBDAS.get(intent_enum, DEFAULT_MMR_LAMBDA)

    def select_evidence(
        self,
        query: str,
        intent: QueryIntent | str,
        subtask_results: dict[str, Any],
        top_k: int | None = None,
        query_embedding: list[float] | np.ndarray | None = None,
        candidate_embeddings: list[list[float]] | np.ndarray | None = None,
    ) -> EvidencePack:
        """Processes raw subtask outputs, deduplicates, runs adaptive MMR, and builds EvidencePack."""
        effective_top_k = top_k or self._default_top_k
        intent_str = intent.value if isinstance(intent, QueryIntent) else str(intent)

        # 1. Aggregate and deduplicate solution cards strictly from successful outputs
        deduped_cards_by_id: dict[str, AuthoritativeCardEvidence] = {}

        for task_id, output in subtask_results.items():
            if not isinstance(output, dict):
                continue
            if output.get("status") not in ("success", None):
                continue

            solutions_raw = output.get("solutions", [])
            # Also handle single-card detail responses (e.g. get_solution_evidence_detail)
            if not solutions_raw and "card_id" in output:
                solutions_raw = [output]

            for s in solutions_raw:
                if not isinstance(s, dict):
                    continue
                cid = s.get("card_id")
                if not cid or not isinstance(cid, str):
                    continue

                # Safety check: VerificationStatus must strictly be APPROVED
                status = s.get("verification_status")
                if status != "APPROVED":
                    logger.warning("Dropped unapproved candidate '%s' (status=%s)", cid, status)
                    continue

                # Extract or infer customer name
                proj = s.get("project_name") or "Enterprise Solution"
                cust_name = self._extract_customer_name(proj, s.get("document_name"))

                # Parse and deduplicate citations/chunks attached to this card
                chunks_list: list[AuthoritativeEvidenceChunk] = []
                for cit in s.get("citations", []):
                    if not isinstance(cit, dict):
                        continue
                    chk_id = cit.get("chunk_id")
                    if not chk_id:
                        continue
                    chunk_text = cit.get("source_chunk_text") or cit.get("chunk_text") or ""
                    chunks_list.append(
                        AuthoritativeEvidenceChunk(
                            chunk_id=chk_id,
                            document_id=cit.get("document_id") or s.get("document_id", "unknown_doc"),
                            card_id=cid,
                            claim_type=cit.get("claim_type"),
                            document_name=cit.get("document_name") or s.get("document_name"),
                            customer_name=cust_name,
                            page_number=cit.get("page_number"),
                            slide_number=cit.get("slide_number"),
                            modality=cit.get("modality") or "TEXT",
                            text_content=chunk_text,
                            note=cit.get("note"),
                        )
                    )

                if cid not in deduped_cards_by_id:
                    deduped_cards_by_id[cid] = AuthoritativeCardEvidence(
                        card_id=cid,
                        document_id=s.get("document_id", "unknown_doc"),
                        project_name=proj,
                        customer_name=cust_name,
                        document_name=s.get("document_name"),
                        business_problem=s.get("business_problem"),
                        proposed_solution=s.get("proposed_solution"),
                        implemented_solution=s.get("implemented_solution"),
                        outcome=s.get("outcome"),
                        verification_status="APPROVED",
                        okf_uri=s.get("okf_uri"),
                        technologies=s.get("technologies") or [],
                        evidence_chunks=chunks_list,
                    )
                else:
                    # Merge any additional unique chunks if encountered in subsequent subtasks
                    existing_chunks = {c.chunk_id for c in deduped_cards_by_id[cid].evidence_chunks}
                    for chk in chunks_list:
                        if chk.chunk_id not in existing_chunks:
                            deduped_cards_by_id[cid].evidence_chunks.append(chk)
                            existing_chunks.add(chk.chunk_id)

        candidate_cards = list(deduped_cards_by_id.values())

        if not candidate_cards:
            return EvidencePack(
                query=query,
                intent=intent_str,
                cards=[],
                chunks_by_id={},
                total_cards=0,
                total_chunks=0,
            )

        # 2. MMR Reranking across aggregated candidates
        final_selected_cards = self._rerank_with_mmr(
            query=query,
            intent=intent,
            candidates=candidate_cards,
            top_k=effective_top_k,
            query_embedding=query_embedding,
            candidate_embeddings=candidate_embeddings,
        )

        # 3. Construct Authoritative EvidencePack
        chunks_map: dict[str, AuthoritativeEvidenceChunk] = {}
        for card in final_selected_cards:
            for chk in card.evidence_chunks:
                if chk.chunk_id not in chunks_map:
                    chunks_map[chk.chunk_id] = chk

        return EvidencePack(
            query=query,
            intent=intent_str,
            cards=final_selected_cards,
            chunks_by_id=chunks_map,
            total_cards=len(final_selected_cards),
            total_chunks=len(chunks_map),
        )

    def _rerank_with_mmr(
        self,
        query: str,
        intent: QueryIntent | str,
        candidates: list[AuthoritativeCardEvidence],
        top_k: int,
        query_embedding: list[float] | np.ndarray | None = None,
        candidate_embeddings: list[list[float]] | np.ndarray | None = None,
    ) -> list[AuthoritativeCardEvidence]:
        """Applies adaptive MMR reranking to candidate cards."""
        if len(candidates) <= 1:
            return candidates[:top_k]

        lambda_param = self.get_adaptive_lambda(intent)

        # Resolve query embedding
        q_vec = None
        if query_embedding is not None:
            q_vec = np.array(query_embedding, dtype=np.float32)
        elif self._embedding_client and query.strip():
            try:
                q_vec = np.array(self._embedding_client.get_embedding(query), dtype=np.float32)
            except Exception as e:
                logger.warning("Query embedding generation failed: %s", e)

        # Resolve candidate embeddings
        cand_vecs = None
        if candidate_embeddings is not None and len(candidate_embeddings) == len(candidates):
            cand_vecs = np.array(candidate_embeddings, dtype=np.float32)
        elif self._embedding_client and q_vec is not None:
            try:
                card_texts = [
                    f"{c.project_name} {c.business_problem or ''} {c.proposed_solution or ''} {c.implemented_solution or ''}"
                    for c in candidates
                ]
                cand_vecs = np.array(self._embedding_client.get_embeddings(card_texts), dtype=np.float32)
            except Exception as e:
                logger.warning("Candidate embeddings generation failed: %s", e)

        # Run compute_mmr if both query and candidate vectors are available
        if q_vec is not None and cand_vecs is not None and len(cand_vecs) == len(candidates):
            try:
                return compute_mmr(
                    query_embedding=q_vec,
                    candidate_embeddings=cand_vecs,
                    candidate_items=candidates,
                    lambda_param=lambda_param,
                    top_k=top_k,
                )
            except Exception as exc:
                logger.warning("MMR computation failed, falling back to top_k: %s", exc)

        # Fallback without MMR: preserve original candidate ordering up to top_k
        return candidates[:top_k]

    def _extract_customer_name(self, project_name: str, document_name: str | None) -> str:
        """Infers canonical customer name from project title or document filename."""
        text = f"{project_name} {document_name or ''}".lower()
        known_customers = [
            ("uber", "Uber"),
            ("canva", "Canva"),
            ("dropbox", "Dropbox"),
            ("youtube", "YouTube"),
            ("spotify", "Spotify"),
            ("netflix", "Netflix"),
        ]
        for key, canonical in known_customers:
            if key in text:
                return canonical
        # Fallback to first word of project name
        return project_name.split()[0] if project_name.split() else "Enterprise Customer"

