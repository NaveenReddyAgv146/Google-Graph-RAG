"""Approval-Aware Graph Retrieval & Access Control Service (Phase 3D Step 4).

Enforces:
1. Approved-Knowledge Publication Contract:
   - ONLY SolutionCards with VerificationStatus = 'APPROVED' are eligible for Discovery retrieval.
   - PENDING, DRAFT, REJECTED, and CHANGES_REQUESTED are strictly excluded.
2. Immediate Revocation Enforcement:
   - Revoking an APPROVED card (to CHANGES_REQUESTED) immediately excludes it from
     subsequent vector search, graph traversal, and response assembly without cache lag.
3. Pre-Retrieval Multi-Tenant / Principal Access Filter:
   - Integrates with RBAC DocumentAccess filters so only cards whose parent DocumentId
     the principal has READ access to can be returned.
4. Grounded Citation & Provenance Assembly:
   - Every returned SolutionCard includes its EvidenceLinks, source chunk quotations,
     document references, and canonical sidecar URI.
5. Multi-Hop Spanner Graph Traversal:
   - Safely queries EnterpriseKnowledgeGraph (BusinessSolution -> Technology, Document)
     with mandatory WHERE s.VerificationStatus = 'APPROVED'.
"""

from dataclasses import dataclass
import logging
from typing import Any

from google.cloud import spanner
from pydantic import BaseModel, Field

from common.config import settings
from common.models.schema import VerificationStatus
import common.rbac.rbac as rbac_module

logger = logging.getLogger("common.search.discovery_service")

# Trusted status required for knowledge publication to discovery copilot
TRUSTED_STATUS = VerificationStatus.APPROVED.value


# ============================================================================
# DATA TRANSFER MODELS
# ============================================================================


class GroundedCitation(BaseModel):
    """Citation anchor linking approved solution to exact source evidence chunk."""

    evidence_id: str
    chunk_id: str
    document_id: str
    claim_type: str | None = None
    document_name: str | None = None
    page_number: int | None = None
    slide_number: int | None = None
    modality: str = "TEXT"
    source_chunk_text: str | None = None
    note: str | None = None


class ApprovedSolutionCard(BaseModel):
    """Verified, human-approved solution card returned to Discovery Copilot."""

    card_id: str
    document_id: str
    document_name: str | None = None
    project_name: str
    business_problem: str | None = None
    proposed_solution: str | None = None
    implemented_solution: str | None = None
    outcome: str | None = None
    verification_status: str = TRUSTED_STATUS
    okf_uri: str | None = None
    technologies: list[str] = Field(default_factory=list)
    citations: list[GroundedCitation] = Field(default_factory=list)


# ============================================================================
# DISCOVERY RETRIEVAL SERVICE
# ============================================================================


class DiscoveryRetrievalService:
    """Read-only service executing approval-aware graph queries and vector retrieval."""

    def __init__(self, database=None):
        self._database = database

    def search_approved_solutions(
        self,
        principal_email: str,
        keyword: str | None = None,
        tech_id: str | None = None,
        document_id: str | None = None,
        limit: int = 10,
    ) -> list[ApprovedSolutionCard]:
        """Retrieves approved business solutions matching query filters and RBAC access."""
        # 1. RBAC Access Resolution
        authorized_doc_ids = rbac_module.resolve_principal_access(
            user_email=principal_email, database=self._database
        )

        # Execute queries within a multi-use snapshot to allow subsequent citation and technology queries
        try:
            snapshot_ctx = self._database.snapshot(multi_use=True)
        except TypeError:
            snapshot_ctx = self._database.snapshot()

        with snapshot_ctx as snapshot:
            # Base query enforcing TRUSTED_STATUS ('APPROVED')
            query = """
                SELECT sc.CardId, sc.DocumentId, d.FileName, sc.ProjectName, sc.BusinessProblem,
                       sc.ProposedSolution, sc.ImplementedSolution, sc.Outcome, sc.VerificationStatus, sc.OkfUri
                FROM SolutionCards sc
                JOIN Documents d ON sc.DocumentId = d.DocumentId
                WHERE sc.VerificationStatus = @approved_status
            """
            params: dict[str, Any] = {"approved_status": TRUSTED_STATUS}
            param_types: dict[str, Any] = {"approved_status": spanner.param_types.STRING}

            # Principal access gating
            if authorized_doc_ids is not None:
                query += " AND sc.DocumentId IN UNNEST(@authorized_doc_ids)"
                params["authorized_doc_ids"] = authorized_doc_ids
                param_types["authorized_doc_ids"] = spanner.param_types.Array(spanner.param_types.STRING)

            # Optional document filter
            if document_id:
                query += " AND sc.DocumentId = @document_id"
                params["document_id"] = document_id
                param_types["document_id"] = spanner.param_types.STRING

            # Optional keyword search
            if keyword and keyword.strip():
                query += """
                    AND (
                        LOWER(sc.ProjectName) LIKE @kw
                        OR LOWER(sc.BusinessProblem) LIKE @kw
                        OR LOWER(sc.ProposedSolution) LIKE @kw
                        OR LOWER(sc.ImplementedSolution) LIKE @kw
                        OR LOWER(sc.Outcome) LIKE @kw
                    )
                """
                params["kw"] = f"%{keyword.strip().lower()}%"
                param_types["kw"] = spanner.param_types.STRING

            query += " ORDER BY sc.ProjectName ASC LIMIT @limit"
            params["limit"] = limit
            param_types["limit"] = spanner.param_types.INT64

            card_rows = list(snapshot.execute_sql(query, params=params, param_types=param_types))
            if not card_rows:
                return []

            card_ids = [r[0] for r in card_rows]

            # 2. Retrieve Grounded Evidence Citations
            ev_query_with_claim = """
                SELECT el.CardId, el.EvidenceId, el.ChunkId, el.DocumentId, d.FileName,
                       el.PageNumber, el.SlideNumber, ec.Modality, ec.TextContent, el.Note, el.ClaimType
                FROM EvidenceLinks el
                JOIN Documents d ON el.DocumentId = d.DocumentId
                JOIN EvidenceChunks ec ON el.ChunkId = ec.ChunkId
                WHERE el.CardId IN UNNEST(@card_ids)
                ORDER BY el.CardId, el.EvidenceId
            """
            ev_query_legacy = """
                SELECT el.CardId, el.EvidenceId, el.ChunkId, el.DocumentId, d.FileName,
                       el.PageNumber, el.SlideNumber, ec.Modality, ec.TextContent, el.Note
                FROM EvidenceLinks el
                JOIN Documents d ON el.DocumentId = d.DocumentId
                JOIN EvidenceChunks ec ON el.ChunkId = ec.ChunkId
                WHERE el.CardId IN UNNEST(@card_ids)
                ORDER BY el.CardId, el.EvidenceId
            """
            ev_params = {"card_ids": card_ids}
            ev_types = {"card_ids": spanner.param_types.Array(spanner.param_types.STRING)}

            try:
                ev_rows = list(snapshot.execute_sql(ev_query_with_claim, params=ev_params, param_types=ev_types))
                has_claim_type = True
            except Exception:
                ev_rows = list(snapshot.execute_sql(ev_query_legacy, params=ev_params, param_types=ev_types))
                has_claim_type = False

            citations_by_card: dict[str, list[GroundedCitation]] = {cid: [] for cid in card_ids}
            for er in ev_rows:
                citations_by_card[er[0]].append(
                    GroundedCitation(
                        evidence_id=er[1],
                        chunk_id=er[2],
                        document_id=er[3],
                        document_name=er[4],
                        page_number=er[5],
                        slide_number=er[6],
                        modality=er[7] or "TEXT",
                        source_chunk_text=er[8],
                        note=er[9],
                        claim_type=er[10] if (has_claim_type and len(er) > 10) else None,
                    )
                )

            # 3. Retrieve Associated Technologies
            tech_query = """
                SELECT sst.CardId, t.Name
                FROM SolutionSupportsTech sst
                JOIN Technologies t ON sst.TechId = t.TechId
                WHERE sst.CardId IN UNNEST(@card_ids)
                ORDER BY t.Name ASC
            """
            tech_rows = list(snapshot.execute_sql(tech_query, params=ev_params, param_types=ev_types))
            tech_by_card: dict[str, list[str]] = {cid: [] for cid in card_ids}
            for tr in tech_rows:
                tech_by_card[tr[0]].append(tr[1])

            # 4. Filter by tech_id if requested
            results = []
            for r in card_rows:
                cid = r[0]
                techs = tech_by_card.get(cid, [])
                if tech_id and tech_id not in techs:
                    continue

                results.append(
                    ApprovedSolutionCard(
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
                        technologies=techs,
                        citations=citations_by_card.get(cid, []),
                    )
                )

            return results

    def traverse_approved_solutions_graph(
        self,
        principal_email: str,
        tech_name: str | None = None,
        limit: int = 5,
    ) -> list[ApprovedSolutionCard]:
        """Executes multi-hop GQL traversal over EnterpriseKnowledgeGraph for APPROVED cards.

        Option A Schema Alignment:
        1. Query valid graph topology (CardId, Technology name) via GQL on EnterpriseKnowledgeGraph.
        2. Join returned CardIds back to relational SolutionCards in SQL.
        3. Enforce sc.VerificationStatus = 'APPROVED' and authorized DocumentIds in relational SQL.
        4. Revalidate authorization on final assembled candidates.
        """
        authorized_doc_ids = rbac_module.resolve_principal_access(
            user_email=principal_email, database=self._database
        )
        if authorized_doc_ids is not None and len(authorized_doc_ids) == 0:
            return []

        # Execute queries within a multi-use snapshot to allow subsequent citation and technology queries
        try:
            snapshot_ctx = self._database.snapshot(multi_use=True)
        except TypeError:
            snapshot_ctx = self._database.snapshot()

        with snapshot_ctx as snapshot:
            # Step 1: Query GQL topology returning only supported graph properties (CardId, t.Name)
            gql_query = f"""
                GRAPH {settings.graph_name}
                MATCH (s:BusinessSolution)-[sup:`SUPPORTS`]->(t:Technology)
            """
            gql_params: dict[str, Any] = {"gql_limit": max(limit * 5, 20)}
            gql_param_types: dict[str, Any] = {"gql_limit": spanner.param_types.INT64}

            if tech_name:
                gql_query += " WHERE t.Name = @tech_name"
                gql_params["tech_name"] = tech_name
                gql_param_types["tech_name"] = spanner.param_types.STRING

            gql_query += """
                RETURN s.CardId AS card_id, t.Name AS tech_name
                LIMIT @gql_limit
            """

            gql_rows = list(
                snapshot.execute_sql(
                    gql_query,
                    params=gql_params,
                    param_types=gql_param_types,
                )
            )

            # Map card_id to technologies encountered in graph
            card_to_techs: dict[str, set[str]] = {}
            for r in gql_rows:
                cid, tname = r[0], r[1]
                if cid not in card_to_techs:
                    card_to_techs[cid] = set()
                if tname:
                    card_to_techs[cid].add(tname)

            card_ids = list(card_to_techs.keys())
            if not card_ids:
                return []

            # Step 2: Join returned CardIds to relational SolutionCards enforcing APPROVED and RBAC
            rel_query = """
                SELECT sc.CardId, sc.DocumentId, d.FileName, sc.ProjectName, sc.BusinessProblem,
                       sc.ProposedSolution, sc.ImplementedSolution, sc.Outcome, sc.VerificationStatus, sc.OkfUri
                FROM SolutionCards sc
                JOIN Documents d ON sc.DocumentId = d.DocumentId
                WHERE sc.CardId IN UNNEST(@card_ids)
                  AND sc.VerificationStatus = @approved_status
            """
            rel_params: dict[str, Any] = {
                "card_ids": card_ids,
                "approved_status": TRUSTED_STATUS,
            }
            rel_param_types: dict[str, Any] = {
                "card_ids": spanner.param_types.Array(spanner.param_types.STRING),
                "approved_status": spanner.param_types.STRING,
            }

            if authorized_doc_ids is not None:
                rel_query += " AND sc.DocumentId IN UNNEST(@authorized_doc_ids)"
                rel_params["authorized_doc_ids"] = authorized_doc_ids
                rel_param_types["authorized_doc_ids"] = spanner.param_types.Array(spanner.param_types.STRING)

            rel_query += " ORDER BY sc.ProjectName ASC LIMIT @limit"
            rel_params["limit"] = limit
            rel_param_types["limit"] = spanner.param_types.INT64

            card_rows = list(
                snapshot.execute_sql(
                    rel_query,
                    params=rel_params,
                    param_types=rel_param_types,
                )
            )
            if not card_rows:
                return []

            approved_cids = [r[0] for r in card_rows]

            # Step 3: Retrieve Grounded Citations for approved cards
            ev_query_with_claim = """
                SELECT el.CardId, el.EvidenceId, el.ChunkId, ec.DocumentId, d.FileName,
                       ec.PageNumber, ec.SlideNumber, ec.Modality, ec.ChunkText, el.Note, el.ClaimType
                FROM EvidenceLinks el
                JOIN EvidenceChunks ec ON el.ChunkId = ec.ChunkId
                JOIN Documents d ON ec.DocumentId = d.DocumentId
                WHERE el.CardId IN UNNEST(@card_ids)
                ORDER BY ec.PageNumber ASC, ec.SlideNumber ASC
            """
            ev_query_legacy = """
                SELECT el.CardId, el.EvidenceId, el.ChunkId, ec.DocumentId, d.FileName,
                       ec.PageNumber, ec.SlideNumber, ec.Modality, ec.ChunkText, el.Note
                FROM EvidenceLinks el
                JOIN EvidenceChunks ec ON el.ChunkId = ec.ChunkId
                JOIN Documents d ON ec.DocumentId = d.DocumentId
                WHERE el.CardId IN UNNEST(@card_ids)
                ORDER BY ec.PageNumber ASC, ec.SlideNumber ASC
            """
            ev_params = {"card_ids": approved_cids}
            ev_types = {"card_ids": spanner.param_types.Array(spanner.param_types.STRING)}

            try:
                ev_rows = list(snapshot.execute_sql(ev_query_with_claim, params=ev_params, param_types=ev_types))
                has_claim_type = True
            except Exception:
                ev_rows = list(snapshot.execute_sql(ev_query_legacy, params=ev_params, param_types=ev_types))
                has_claim_type = False

            citations_by_card: dict[str, list[GroundedCitation]] = {cid: [] for cid in approved_cids}
            for er in ev_rows:
                cid = er[0]
                citations_by_card[cid].append(
                    GroundedCitation(
                        evidence_id=er[1],
                        chunk_id=er[2],
                        document_id=er[3],
                        document_name=er[4],
                        page_number=er[5],
                        slide_number=er[6],
                        modality=er[7] or "TEXT",
                        source_chunk_text=er[8],
                        note=er[9],
                        claim_type=er[10] if (has_claim_type and len(er) > 10) else None,
                    )
                )

            # Step 4: Union with full technology list from SolutionSupportsTech
            tech_query = """
                SELECT sst.CardId, t.Name
                FROM SolutionSupportsTech sst
                JOIN Technologies t ON sst.TechId = t.TechId
                WHERE sst.CardId IN UNNEST(@card_ids)
                ORDER BY t.Name ASC
            """
            tech_rows = list(snapshot.execute_sql(tech_query, params=ev_params, param_types=ev_types))
            for tr in tech_rows:
                cid, tname = tr[0], tr[1]
                if cid in card_to_techs and tname:
                    card_to_techs[cid].add(tname)

            # Step 5: Assemble and strictly revalidate
            results = []
            for r in card_rows:
                cid = r[0]
                doc_id = r[1]
                status = r[8]

                # Final revalidation
                if status != TRUSTED_STATUS:
                    continue
                if authorized_doc_ids is not None and doc_id not in authorized_doc_ids:
                    continue

                results.append(
                    ApprovedSolutionCard(
                        card_id=cid,
                        document_id=doc_id,
                        document_name=r[2],
                        project_name=r[3] or "Untitled Solution",
                        business_problem=r[4],
                        proposed_solution=r[5],
                        implemented_solution=r[6],
                        outcome=r[7],
                        verification_status=status,
                        okf_uri=r[9],
                        technologies=sorted(list(card_to_techs.get(cid, set()))),
                        citations=citations_by_card.get(cid, []),
                    )
                )

            return results
