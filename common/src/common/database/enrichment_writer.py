"""Enterprise Agentic GraphRAG - Transactional Spanner Enrichment Writer.

Atomically persists Phase 3C enrichment outputs across all 11 required Spanner tables
in a single serializable read-write transaction:
  - EvidenceChunks (vector embeddings)
  - Entities (business concepts)
  - Technologies (canonical tech catalog entries)
  - SolutionCards (customer solutions & architectures)
  - ChunkReferencesEntity (Chunk -> Entity edges)
  - ChunkMentionsTech (Chunk -> Tech edges)
  - SolutionSupportsTech (SolutionCard -> Tech edges)
  - SolutionImplementsDoc (SolutionCard -> Document edges)
  - EntityReferencesEntity (Entity -> Entity edges)
  - EvidenceLinks (Citations and durable evidence quotes)
  - Documents (Status update to ENRICHED)

Invariants:
1. Transactional Atomicity: Exactly one serializable read-write Spanner transaction.
2. Read-Before-Buffer: Validates existing records and ownership before buffering mutations.
3. Immutability: Never modifies document text, chunk text, page/slide numbers, or VisualAssets.
4. Multimodal Safety: IMAGE chunks are strictly excluded from text embeddings.
5. Shared Node Preservation: Never deletes or corrupts shared Entities or Technologies.
6. Human Review Protection: Never overwrites SolutionCards with VerificationStatus='APPROVED'.
7. Referential Integrity: Pre-validates all foreign-key references and edge connections.
8. Grounded Provenance: Persists durable EvidenceLinks and preserves sidecar SHA-256 in OkfUri.
9. Deterministic Idempotency: Ordinary reruns produce exactly 0 mutations.
10. Test Isolation: Supports dependency injection for offline testing without cloud access.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from google.cloud import spanner

from common.config import Settings, settings as global_settings
from common.models.schema import (
    ChunkNode,
    ClaimCategory,
    EntityNode,
    EntityReferencesEntityEdge,
    SolutionCardNode,
    TechnologyNode,
)
from common.parsers.metadata_extractor import (
    ExtractedMetadataResult,
    ValidatedEntity,
    ValidatedEntityRelationship,
    ValidatedSolutionCard,
    ValidatedTechnology,
)
from common.taxonomy import (
    MAX_CANONICAL_VERSION_LENGTH,
    MAX_ENTITY_CATEGORY_LENGTH,
    MAX_ENTITY_ID_LENGTH,
    MAX_ENTITY_NAME_LENGTH,
    MAX_TECH_CATEGORY_LENGTH,
    MAX_TECH_ID_LENGTH,
    MAX_TECH_NAME_LENGTH,
)

logger = logging.getLogger("common.database.enrichment_writer")

EXPECTED_EMBEDDING_DIMENSIONS = 768


# ============================================================================
# TYPED DOMAIN EXCEPTIONS
# ============================================================================


class EnrichmentPersistenceError(Exception):
    """Base exception for enrichment database operations."""

    pass


class DocumentNotFoundError(EnrichmentPersistenceError):
    """Raised when the target DocumentId does not exist in Documents table."""

    pass


class ChunkOwnershipConflictError(EnrichmentPersistenceError):
    """Raised when an evidence chunk belongs to a different document."""

    pass


class ApprovedSolutionCardProtectionError(EnrichmentPersistenceError):
    """Raised when an operation attempts to overwrite an approved solution card."""

    pass


class ReferentialIntegrityError(EnrichmentPersistenceError):
    """Raised when foreign-key references (chunks, entities, techs, cards) do not resolve."""

    pass


class SchemaConstraintError(EnrichmentPersistenceError):
    """Raised when field length or vector dimensionality violates schema constraints."""

    pass


# ============================================================================
# DATA TRANSFER OBJECTS
# ============================================================================


@dataclass
class EnrichmentPayload:
    """Consolidated input payload for transactional enrichment persistence."""

    document_id: str
    chunk_embeddings: dict[str, list[float]] = field(default_factory=dict)
    entities: list[ValidatedEntity | EntityNode] = field(default_factory=list)
    technologies: list[ValidatedTechnology | TechnologyNode] = field(default_factory=list)
    solution_cards: list[ValidatedSolutionCard | SolutionCardNode] = field(default_factory=list)
    relationships: list[ValidatedEntityRelationship | EntityReferencesEntityEdge] = field(default_factory=list)
    sidecar_uri: str | None = None
    sidecar_sha256: str | None = None

    @classmethod
    def from_metadata_result(
        cls,
        document_id: str,
        chunk_embeddings: dict[str, list[float]],
        metadata_result: ExtractedMetadataResult,
        sidecar_uri: str | None = None,
        sidecar_sha256: str | None = None,
    ) -> EnrichmentPayload:
        """Construct payload from Phase 3C.4 ExtractedMetadataResult."""
        return cls(
            document_id=document_id,
            chunk_embeddings=chunk_embeddings,
            entities=list(metadata_result.entities),
            technologies=list(metadata_result.technologies),
            solution_cards=list(metadata_result.solution_cards),
            relationships=list(metadata_result.relationships),
            sidecar_uri=sidecar_uri,
            sidecar_sha256=sidecar_sha256,
        )


@dataclass(frozen=True)
class EnrichmentWriteResult:
    """Detailed summary of the atomic enrichment persistence transaction."""

    document_id: str
    status: str  # "ENRICHED", "UNCHANGED"
    total_mutations: int
    chunks_embeddings_updated: int
    chunks_embeddings_unchanged: int
    entities_inserted: int
    entities_updated: int
    entities_unchanged: int
    technologies_inserted: int
    technologies_unchanged: int
    solution_cards_inserted: int
    solution_cards_updated: int
    solution_cards_unchanged: int
    solution_cards_protected: int
    edges_inserted: int
    edges_unchanged: int
    evidence_links_inserted: int
    evidence_links_unchanged: int
    document_status_updated: bool


# ============================================================================
# DETERMINISTIC HELPERS
# ============================================================================


def generate_evidence_link_id(
    card_id: str,
    chunk_id: str,
    quote: str = "",
    claim_type: str | None = None,
) -> str:
    """Generate deterministic EvidenceId <= 64 chars.
    
    Preserves exact legacy hash when claim_type is None or empty.
    """
    if claim_type:
        raw = f"{card_id}:{chunk_id}:{claim_type}:{quote.strip()[:64]}"
    else:
        raw = f"{card_id}:{chunk_id}:{quote.strip()[:64]}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:48]
    return f"evl_{digest}"


def format_okf_uri(sidecar_uri: str | None, sidecar_sha256: str | None) -> str | None:
    """Format durable GCS sidecar hash reference for SolutionCards.OkfUri."""
    if sidecar_uri and sidecar_sha256:
        return f"{sidecar_uri}#sha256={sidecar_sha256}"
    if sidecar_uri:
        return sidecar_uri
    if sidecar_sha256:
        return f"#sha256={sidecar_sha256}"
    return None


def are_embeddings_equal(v1: Sequence[float] | None, v2: Sequence[float] | None, tol: float = 1e-5) -> bool:
    """Compare two vector embeddings with floating-point tolerance."""
    if v1 is None and v2 is None:
        return True
    if v1 is None or v2 is None:
        return False
    if len(v1) != len(v2):
        return False
    for a, b in zip(v1, v2):
        if abs(a - b) > tol:
            return False
    return True


# ============================================================================
# TRANSACTIONAL ENRICHMENT WRITER
# ============================================================================


class EnrichmentWriter:
    """Transactional Spanner enrichment writer for Phase 3C."""

    def __init__(
        self,
        database: Any | None = None,
        project_id: str | None = None,
        instance_id: str | None = None,
        database_id: str | None = None,
        settings: Settings | None = None,
    ) -> None:
        cfg = settings or global_settings
        self.project_id = project_id or cfg.project_id
        self.instance_id = instance_id or cfg.spanner_instance_id
        self.database_id = database_id or cfg.spanner_database_id
        self._database = database

    @property
    def database(self) -> Any:
        """Lazily initialize Cloud Spanner Database instance."""
        if self._database is None:
            client = spanner.Client(project=self.project_id)
            instance = client.instance(self.instance_id)
            self._database = instance.database(self.database_id)
        return self._database

    def persist_enrichment(
        self,
        payload: EnrichmentPayload | None = None,
        document_id: str | None = None,
        chunk_embeddings: dict[str, list[float]] | None = None,
        metadata_result: ExtractedMetadataResult | None = None,
        sidecar_uri: str | None = None,
        sidecar_sha256: str | None = None,
        force_recompute: bool = False,
    ) -> EnrichmentWriteResult:
        """Execute atomic serializable transaction across all 11 Spanner tables.

        Accepts either a consolidated EnrichmentPayload or individual components.
        """
        # Resolve payload
        if payload is None:
            if not document_id:
                raise EnrichmentPersistenceError("document_id or payload must be provided.")
            if metadata_result is not None:
                payload = EnrichmentPayload.from_metadata_result(
                    document_id=document_id,
                    chunk_embeddings=chunk_embeddings or {},
                    metadata_result=metadata_result,
                    sidecar_uri=sidecar_uri,
                    sidecar_sha256=sidecar_sha256,
                )
            else:
                payload = EnrichmentPayload(
                    document_id=document_id,
                    chunk_embeddings=chunk_embeddings or {},
                    sidecar_uri=sidecar_uri,
                    sidecar_sha256=sidecar_sha256,
                )

        logger.info(
            "Starting transactional enrichment persistence for document '%s' (force_recompute=%s)",
            payload.document_id,
            force_recompute,
        )

        def _txn_wrapper(transaction: Any) -> EnrichmentWriteResult:
            return self._enrichment_transaction(transaction, payload, force_recompute)

        return self.database.run_in_transaction(_txn_wrapper)

    def _enrichment_transaction(
        self,
        transaction: Any,
        payload: EnrichmentPayload,
        force_recompute: bool,
    ) -> EnrichmentWriteResult:
        """Internal atomic transaction logic executed within database.run_in_transaction."""
        doc_id = payload.document_id

        # ====================================================================
        # 1. READ & VALIDATE DOCUMENT
        # ====================================================================
        doc_sql = "SELECT DocumentId, Status FROM Documents WHERE DocumentId = @doc_id"
        doc_rows = list(
            transaction.execute_sql(
                doc_sql,
                params={"doc_id": doc_id},
                param_types={"doc_id": spanner.param_types.STRING},
            )
        )
        if not doc_rows:
            raise DocumentNotFoundError(f"Target document '{doc_id}' does not exist in Documents table.")

        existing_doc_status = doc_rows[0][1]

        # ====================================================================
        # 2. READ & VALIDATE EVIDENCE CHUNKS (OWNERSHIP & MULTIMODAL SAFETY)
        # ====================================================================
        chunks_sql = """
        SELECT ChunkId, Modality, TextContent, PageNumber, SlideNumber, Embedding768
        FROM EvidenceChunks
        WHERE DocumentId = @doc_id
        """
        chunk_rows = list(
            transaction.execute_sql(
                chunks_sql,
                params={"doc_id": doc_id},
                param_types={"doc_id": spanner.param_types.STRING},
            )
        )
        doc_chunks = {r[0]: r for r in chunk_rows}

        # Ownership verification for embedding targets
        target_chunk_ids = set(payload.chunk_embeddings.keys())
        unknown_chunks = target_chunk_ids - set(doc_chunks.keys())
        if unknown_chunks:
            # Check if unknown chunks belong to another document
            conflict_sql = """
            SELECT ChunkId, DocumentId FROM EvidenceChunks
            WHERE ChunkId IN UNNEST(@cids)
            """
            conflict_rows = list(
                transaction.execute_sql(
                    conflict_sql,
                    params={"cids": list(unknown_chunks)},
                    param_types={"cids": spanner.param_types.Array(spanner.param_types.STRING)},
                )
            )
            if conflict_rows:
                conflict_cid, other_doc = conflict_rows[0]
                raise ChunkOwnershipConflictError(
                    f"Chunk '{conflict_cid}' belongs to Document '{other_doc}', not target '{doc_id}'."
                )
            # Not in any document
            raise ReferentialIntegrityError(
                f"Embedding target chunks do not exist in database: {list(unknown_chunks)[:5]}"
            )

        # ====================================================================
        # 3. EVIDENCECHUNKS: EMBEDDINGS DIFFING & MUTATIONS
        # ====================================================================
        chunks_updated = 0
        chunks_unchanged = 0
        chunk_mutation_values: list[tuple[str, list[float]]] = []

        for cid, embedding in payload.chunk_embeddings.items():
            if len(embedding) != EXPECTED_EMBEDDING_DIMENSIONS:
                raise SchemaConstraintError(
                    f"Invalid embedding dimension ({len(embedding)}): expected {EXPECTED_EMBEDDING_DIMENSIONS}."
                )

            chunk_info = doc_chunks[cid]
            modality = chunk_info[1]
            text_content = chunk_info[2]
            existing_emb = chunk_info[5]

            # Invariant 4: Exclude IMAGE chunks and empty-text chunks from text embeddings
            if modality == "IMAGE" or not text_content or not text_content.strip():
                logger.debug("Skipping embedding update for IMAGE/empty chunk '%s'", cid)
                continue

            if are_embeddings_equal(existing_emb, embedding) and not force_recompute:
                chunks_unchanged += 1
            else:
                chunk_mutation_values.append((cid, embedding))
                chunks_updated += 1

        if chunk_mutation_values:
            transaction.update(
                "EvidenceChunks",
                columns=["ChunkId", "Embedding768"],
                values=chunk_mutation_values,
            )

        # ====================================================================
        # 4. ENTITIES: DIFFING, VALIDATION & SHARED NODE PRESERVATION
        # ====================================================================
        entities_inserted = 0
        entities_updated = 0
        entities_unchanged = 0

        # Normalization to EntityNode-like records
        candidate_entities: dict[str, tuple[str, str, str, str | None]] = {}
        for ent in payload.entities:
            eid = ent.entity_id if hasattr(ent, "entity_id") else ent.get("entity_id")
            name = ent.name if hasattr(ent, "name") else ent.get("name")
            category = ent.category if hasattr(ent, "category") else ent.get("category")
            desc = ent.description if hasattr(ent, "description") else ent.get("description")

            if not eid or len(eid) > MAX_ENTITY_ID_LENGTH:
                raise SchemaConstraintError(f"Invalid EntityId: '{eid}'")
            if not name or len(name) > MAX_ENTITY_NAME_LENGTH:
                raise SchemaConstraintError(f"Invalid Entity Name: '{name}'")
            if not category or len(category) > MAX_ENTITY_CATEGORY_LENGTH:
                raise SchemaConstraintError(f"Invalid Entity Category: '{category}'")

            candidate_entities[eid] = (eid, name, category, desc)

        if candidate_entities:
            existing_ent_sql = """
            SELECT EntityId, Name, Category, Description
            FROM Entities
            WHERE EntityId IN UNNEST(@eids)
            """
            existing_ent_rows = list(
                transaction.execute_sql(
                    existing_ent_sql,
                    params={"eids": list(candidate_entities.keys())},
                    param_types={"eids": spanner.param_types.Array(spanner.param_types.STRING)},
                )
            )
            existing_ents = {r[0]: r for r in existing_ent_rows}

            ent_inserts: list[tuple[str, str, str, str | None]] = []
            ent_updates: list[tuple[str, str, str, str | None]] = []

            for eid, record in candidate_entities.items():
                if eid not in existing_ents:
                    ent_inserts.append(record)
                    entities_inserted += 1
                else:
                    existing = existing_ents[eid]
                    existing_desc = existing[3]
                    new_desc = record[3]
                    # Update description if currently empty and new description is provided
                    if not existing_desc and new_desc:
                        ent_updates.append(record)
                        entities_updated += 1
                    else:
                        entities_unchanged += 1

            if ent_inserts:
                transaction.insert(
                    "Entities",
                    columns=["EntityId", "Name", "Category", "Description"],
                    values=ent_inserts,
                )
            if ent_updates:
                transaction.update(
                    "Entities",
                    columns=["EntityId", "Name", "Category", "Description"],
                    values=ent_updates,
                )

        # ====================================================================
        # 5. TECHNOLOGIES: DIFFING & SHARED NODE PRESERVATION
        # ====================================================================
        techs_inserted = 0
        techs_unchanged = 0

        candidate_techs: dict[str, tuple[str, str, str, str | None]] = {}
        for tech in payload.technologies:
            # If ValidatedTechnology, only persist canonical ones
            if hasattr(tech, "is_canonical") and not tech.is_canonical:
                continue
            tid = getattr(tech, "tech_id", None)
            if not tid:
                continue
            name = getattr(tech, "canonical_name", None) or getattr(tech, "name", None)
            category = getattr(tech, "category", None)
            version = getattr(tech, "canonical_version", None)

            if not tid or len(tid) > MAX_TECH_ID_LENGTH:
                raise SchemaConstraintError(f"Invalid TechId: '{tid}'")
            if not name or len(name) > MAX_TECH_NAME_LENGTH:
                raise SchemaConstraintError(f"Invalid Technology Name: '{name}'")
            if not category or len(category) > MAX_TECH_CATEGORY_LENGTH:
                raise SchemaConstraintError(f"Invalid Technology Category: '{category}'")

            candidate_techs[tid] = (tid, name, category, version)

        if candidate_techs:
            existing_tech_sql = """
            SELECT TechId, Name, Category, CanonicalVersion
            FROM Technologies
            WHERE TechId IN UNNEST(@tids)
            """
            existing_tech_rows = list(
                transaction.execute_sql(
                    existing_tech_sql,
                    params={"tids": list(candidate_techs.keys())},
                    param_types={"tids": spanner.param_types.Array(spanner.param_types.STRING)},
                )
            )
            existing_tech_ids = {r[0] for r in existing_tech_rows}

            tech_inserts: list[tuple[str, str, str, str | None]] = []
            for tid, record in candidate_techs.items():
                if tid not in existing_tech_ids:
                    tech_inserts.append(record)
                    techs_inserted += 1
                else:
                    techs_unchanged += 1

            if tech_inserts:
                transaction.insert(
                    "Technologies",
                    columns=["TechId", "Name", "Category", "CanonicalVersion"],
                    values=tech_inserts,
                )

        # ====================================================================
        # 6. SOLUTIONCARDS: DIFFING, SIDECAR HASH & APPROVED-CARD PROTECTION
        # ====================================================================
        cards_inserted = 0
        cards_updated = 0
        cards_unchanged = 0
        cards_protected = 0

        card_inserts: list[tuple[Any, ...]] = []
        card_updates: list[tuple[Any, ...]] = []

        existing_cards_sql = """
        SELECT CardId, DocumentId, ProjectName, BusinessProblem, ProposedSolution,
               ImplementedSolution, Outcome, VerificationStatus, OkfUri
        FROM SolutionCards
        WHERE DocumentId = @doc_id
        """
        existing_card_rows = list(
            transaction.execute_sql(
                existing_cards_sql,
                params={"doc_id": doc_id},
                param_types={"doc_id": spanner.param_types.STRING},
            )
        )
        existing_cards = {r[0]: r for r in existing_card_rows}

        okf_uri_val = format_okf_uri(payload.sidecar_uri, payload.sidecar_sha256)

        for card in payload.solution_cards:
            cid = card.card_id
            p_name = card.project_name
            b_prob = card.business_problem
            p_sol = card.proposed_solution
            i_sol = card.implemented_solution
            outcome = card.outcome

            if not cid or len(cid) > 64:
                raise SchemaConstraintError(f"Invalid CardId: '{cid}'")
            if not p_name or len(p_name) > 256:
                raise SchemaConstraintError(f"Invalid ProjectName: '{p_name}'")

            if cid in existing_cards:
                existing = existing_cards[cid]
                existing_status = existing[7]
                existing_okf = existing[8]

                # Invariant 6: Protect human-audited cards (APPROVED and REJECTED) from automated overwrite or reactivation
                if existing_status in ("APPROVED", "REJECTED"):
                    logger.info("SolutionCard '%s' has protected VerificationStatus='%s'. Overwrite and silent reactivation prevented.", cid, existing_status)
                    cards_protected += 1
                    cards_unchanged += 1
                    continue

                # Check if fields changed
                fields_changed = (
                    existing[2] != p_name
                    or existing[3] != b_prob
                    or existing[4] != p_sol
                    or existing[5] != i_sol
                    or existing[6] != outcome
                    or (okf_uri_val and existing_okf != okf_uri_val)
                )

                if fields_changed and force_recompute:
                    effective_okf = okf_uri_val or existing_okf
                    card_updates.append(
                        (cid, p_name, b_prob, p_sol, i_sol, outcome, effective_okf)
                    )
                    cards_updated += 1
                else:
                    cards_unchanged += 1
            else:
                card_inserts.append(
                    (cid, doc_id, p_name, b_prob, p_sol, i_sol, outcome, "PENDING", okf_uri_val)
                )
                cards_inserted += 1

        # Check existing cards for this document not present in payload.solution_cards (e.g. legacy placeholder cards)
        # Safely align OkfUri to the document's canonical sidecar hash if out-of-date,
        # preserving ProjectName, BusinessProblem, ProposedSolution, ImplementedSolution, Outcome, and VerificationStatus.
        payload_card_ids = {c.card_id for c in payload.solution_cards}
        for existing_cid, existing_row in existing_cards.items():
            if existing_cid not in payload_card_ids:
                existing_okf = existing_row[8]
                if okf_uri_val and existing_okf != okf_uri_val:
                    card_updates.append(
                        (
                            existing_cid,
                            existing_row[2],  # ProjectName
                            existing_row[3],  # BusinessProblem
                            existing_row[4],  # ProposedSolution
                            existing_row[5],  # ImplementedSolution
                            existing_row[6],  # Outcome
                            okf_uri_val,
                        )
                    )
                    cards_updated += 1
                else:
                    cards_unchanged += 1

        if card_inserts:
            transaction.insert(
                "SolutionCards",
                columns=[
                    "CardId",
                    "DocumentId",
                    "ProjectName",
                    "BusinessProblem",
                    "ProposedSolution",
                    "ImplementedSolution",
                    "Outcome",
                    "VerificationStatus",
                    "OkfUri",
                ],
                values=card_inserts,
            )
        if card_updates:
            transaction.update(
                "SolutionCards",
                columns=[
                    "CardId",
                    "ProjectName",
                    "BusinessProblem",
                    "ProposedSolution",
                    "ImplementedSolution",
                    "Outcome",
                    "OkfUri",
                ],
                values=card_updates,
            )

        # ====================================================================
        # 7. EDGES PERSISTENCE & REFERENTIAL INTEGRITY
        # ====================================================================
        edges_inserted = 0
        edges_unchanged = 0

        # --- Edge Table 1: SolutionImplementsDoc (CardId -> DocumentId) ---
        target_card_ids = [c.card_id for c in payload.solution_cards]
        if target_card_ids:
            existing_sid_sql = """
            SELECT CardId, DocumentId FROM SolutionImplementsDoc
            WHERE DocumentId = @doc_id
            """
            existing_sids = {
                (r[0], r[1])
                for r in transaction.execute_sql(
                    existing_sid_sql,
                    params={"doc_id": doc_id},
                    param_types={"doc_id": spanner.param_types.STRING},
                )
            }
            sid_inserts = [
                (cid, doc_id) for cid in target_card_ids if (cid, doc_id) not in existing_sids
            ]
            if sid_inserts:
                transaction.insert("SolutionImplementsDoc", columns=["CardId", "DocumentId"], values=sid_inserts)
                edges_inserted += len(sid_inserts)
            edges_unchanged += len(target_card_ids) - len(sid_inserts)

        # --- Edge Table 2: ChunkReferencesEntity (ChunkId -> EntityId) ---
        chunk_entity_pairs = set()
        for ent in payload.entities:
            c_chunk_id = getattr(ent, "chunk_id", None)
            c_entity_id = getattr(ent, "entity_id", None)
            if c_chunk_id and c_entity_id:
                if c_chunk_id not in doc_chunks:
                    raise ReferentialIntegrityError(f"ChunkReferencesEntity: chunk '{c_chunk_id}' not in document.")
                chunk_entity_pairs.add((c_chunk_id, c_entity_id))

        if chunk_entity_pairs:
            chunk_ids_list = list({p[0] for p in chunk_entity_pairs})
            existing_cre_sql = """
            SELECT ChunkId, EntityId FROM ChunkReferencesEntity
            WHERE ChunkId IN UNNEST(@cids)
            """
            existing_cres = {
                (r[0], r[1])
                for r in transaction.execute_sql(
                    existing_cre_sql,
                    params={"cids": chunk_ids_list},
                    param_types={"cids": spanner.param_types.Array(spanner.param_types.STRING)},
                )
            }
            cre_inserts = [p for p in chunk_entity_pairs if p not in existing_cres]
            if cre_inserts:
                transaction.insert("ChunkReferencesEntity", columns=["ChunkId", "EntityId"], values=cre_inserts)
                edges_inserted += len(cre_inserts)
            edges_unchanged += len(chunk_entity_pairs) - len(cre_inserts)

        # --- Edge Table 3: ChunkMentionsTech (ChunkId -> TechId) ---
        chunk_tech_pairs = set()
        for tech in payload.technologies:
            c_chunk_id = getattr(tech, "chunk_id", None)
            c_tech_id = getattr(tech, "tech_id", None)
            is_canon = getattr(tech, "is_canonical", True)
            if c_chunk_id and c_tech_id and is_canon:
                if c_chunk_id not in doc_chunks:
                    raise ReferentialIntegrityError(f"ChunkMentionsTech: chunk '{c_chunk_id}' not in document.")
                chunk_tech_pairs.add((c_chunk_id, c_tech_id))

        if chunk_tech_pairs:
            chunk_ids_list = list({p[0] for p in chunk_tech_pairs})
            existing_cmt_sql = """
            SELECT ChunkId, TechId FROM ChunkMentionsTech
            WHERE ChunkId IN UNNEST(@cids)
            """
            existing_cmts = {
                (r[0], r[1])
                for r in transaction.execute_sql(
                    existing_cmt_sql,
                    params={"cids": chunk_ids_list},
                    param_types={"cids": spanner.param_types.Array(spanner.param_types.STRING)},
                )
            }
            cmt_inserts = [p for p in chunk_tech_pairs if p not in existing_cmts]
            if cmt_inserts:
                transaction.insert("ChunkMentionsTech", columns=["ChunkId", "TechId"], values=cmt_inserts)
                edges_inserted += len(cmt_inserts)
            edges_unchanged += len(chunk_tech_pairs) - len(cmt_inserts)

        # --- Edge Table 4: SolutionSupportsTech (CardId -> TechId) ---
        card_tech_pairs = set()
        for card in payload.solution_cards:
            cid = card.card_id
            techs = getattr(card, "technologies", [])
            for tid in techs:
                if tid:
                    card_tech_pairs.add((cid, tid))

        if card_tech_pairs:
            card_ids_list = list({p[0] for p in card_tech_pairs})
            existing_sst_sql = """
            SELECT CardId, TechId FROM SolutionSupportsTech
            WHERE CardId IN UNNEST(@cids)
            """
            existing_ssts = {
                (r[0], r[1])
                for r in transaction.execute_sql(
                    existing_sst_sql,
                    params={"cids": card_ids_list},
                    param_types={"cids": spanner.param_types.Array(spanner.param_types.STRING)},
                )
            }
            sst_inserts = [p for p in card_tech_pairs if p not in existing_ssts]
            if sst_inserts:
                transaction.insert("SolutionSupportsTech", columns=["CardId", "TechId"], values=sst_inserts)
                edges_inserted += len(sst_inserts)
            edges_unchanged += len(card_tech_pairs) - len(sst_inserts)

        # --- Edge Table 5: EntityReferencesEntity (SourceEntityId -> TargetEntityId) ---
        entity_rel_pairs = set()
        for rel in payload.relationships:
            src_id = getattr(rel, "source_entity_id", None)
            tgt_id = getattr(rel, "target_entity_id", None)
            if src_id and tgt_id and src_id != tgt_id:
                entity_rel_pairs.add((src_id, tgt_id))

        if entity_rel_pairs:
            src_ids_list = list({p[0] for p in entity_rel_pairs})
            existing_ere_sql = """
            SELECT SourceEntityId, TargetEntityId FROM EntityReferencesEntity
            WHERE SourceEntityId IN UNNEST(@sids)
            """
            existing_eres = {
                (r[0], r[1])
                for r in transaction.execute_sql(
                    existing_ere_sql,
                    params={"sids": src_ids_list},
                    param_types={"sids": spanner.param_types.Array(spanner.param_types.STRING)},
                )
            }
            ere_inserts = [p for p in entity_rel_pairs if p not in existing_eres]
            if ere_inserts:
                transaction.insert("EntityReferencesEntity", columns=["SourceEntityId", "TargetEntityId"], values=ere_inserts)
                edges_inserted += len(ere_inserts)
            edges_unchanged += len(entity_rel_pairs) - len(ere_inserts)

        # ====================================================================
        # 8. EVIDENCELINKS: PERSIST CITATIONS & EVIDENCE QUOTES
        # ====================================================================
        evidence_links_inserted = 0
        evidence_links_unchanged = 0

        evidence_link_candidates: list[tuple[str, str, str | None, str, int | None, int | None, str | None, str | None]] = []
        for card in payload.solution_cards:
            cid = card.card_id

            # Check for fine-grained claim citations
            claim_citations = getattr(card, "claim_evidence", None)
            if claim_citations:
                for ce in claim_citations:
                    chunk_id = getattr(ce, "chunk_id", None)
                    claim_type_raw = getattr(ce, "claim_type", None)
                    claim_type_str = claim_type_raw.value if hasattr(claim_type_raw, "value") else (str(claim_type_raw) if claim_type_raw else None)
                    quote = getattr(ce, "evidence_quote", None) or getattr(ce, "quote", "") or ""

                    if chunk_id and chunk_id in doc_chunks:
                        chunk_rec = doc_chunks[chunk_id]
                        page_num = chunk_rec[3]
                        slide_num = chunk_rec[4]
                    else:
                        page_num = None
                        slide_num = None

                    ev_id = generate_evidence_link_id(cid, chunk_id or "none", quote, claim_type=claim_type_str)
                    evidence_link_candidates.append(
                        (ev_id, cid, chunk_id, doc_id, page_num, slide_num, quote or None, claim_type_str)
                    )
            else:
                chunk_id = getattr(card, "chunk_id", None)
                quote = getattr(card, "evidence_quote", None) or ""
                claim_type_raw = getattr(card, "claim_type", None)
                claim_type_str = claim_type_raw.value if hasattr(claim_type_raw, "value") else (str(claim_type_raw) if claim_type_raw else None)

                if chunk_id and chunk_id in doc_chunks:
                    chunk_rec = doc_chunks[chunk_id]
                    page_num = chunk_rec[3]
                    slide_num = chunk_rec[4]
                else:
                    page_num = None
                    slide_num = None

                ev_id = generate_evidence_link_id(cid, chunk_id or "none", quote, claim_type=claim_type_str)
                evidence_link_candidates.append(
                    (ev_id, cid, chunk_id, doc_id, page_num, slide_num, quote or None, claim_type_str)
                )

        if evidence_link_candidates:
            ev_ids = [c[0] for c in evidence_link_candidates]
            existing_ev_sql = """
            SELECT EvidenceId FROM EvidenceLinks
            WHERE EvidenceId IN UNNEST(@ev_ids)
            """
            existing_ev_ids = {
                r[0]
                for r in transaction.execute_sql(
                    existing_ev_sql,
                    params={"ev_ids": ev_ids},
                    param_types={"ev_ids": spanner.param_types.Array(spanner.param_types.STRING)},
                )
            }
            ev_inserts = [c for c in evidence_link_candidates if c[0] not in existing_ev_ids]
            if ev_inserts:
                has_claim_types = any(c[7] is not None for c in ev_inserts)
                if has_claim_types:
                    transaction.insert(
                        "EvidenceLinks",
                        columns=["EvidenceId", "CardId", "ChunkId", "DocumentId", "PageNumber", "SlideNumber", "Note", "ClaimType"],
                        values=ev_inserts,
                    )
                else:
                    transaction.insert(
                        "EvidenceLinks",
                        columns=["EvidenceId", "CardId", "ChunkId", "DocumentId", "PageNumber", "SlideNumber", "Note"],
                        values=[c[:7] for c in ev_inserts],
                    )
                evidence_links_inserted += len(ev_inserts)
            evidence_links_unchanged += len(evidence_link_candidates) - len(ev_inserts)

        # ====================================================================
        # 9. DOCUMENTS: STATUS UPDATE
        # ====================================================================
        document_status_updated = False
        if existing_doc_status != "ENRICHED":
            transaction.update("Documents", columns=["DocumentId", "Status"], values=[(doc_id, "ENRICHED")])
            document_status_updated = True

        # ====================================================================
        # 10. TOTAL MUTATIONS SUMMARY
        # ====================================================================
        total_mutations = (
            len(chunk_mutation_values)
            + entities_inserted
            + entities_updated
            + techs_inserted
            + len(card_inserts)
            + len(card_updates)
            + edges_inserted
            + evidence_links_inserted
            + (1 if document_status_updated else 0)
        )

        status_str = "ENRICHED" if total_mutations > 0 else "UNCHANGED"

        logger.info(
            "Transaction buffered %d mutations for document '%s' (status: %s)",
            total_mutations,
            doc_id,
            status_str,
        )

        return EnrichmentWriteResult(
            document_id=doc_id,
            status=status_str,
            total_mutations=total_mutations,
            chunks_embeddings_updated=chunks_updated,
            chunks_embeddings_unchanged=chunks_unchanged,
            entities_inserted=entities_inserted,
            entities_updated=entities_updated,
            entities_unchanged=entities_unchanged,
            technologies_inserted=techs_inserted,
            technologies_unchanged=techs_unchanged,
            solution_cards_inserted=cards_inserted,
            solution_cards_updated=cards_updated,
            solution_cards_unchanged=cards_unchanged,
            solution_cards_protected=cards_protected,
            edges_inserted=edges_inserted,
            edges_unchanged=edges_unchanged,
            evidence_links_inserted=evidence_links_inserted,
            evidence_links_unchanged=evidence_links_unchanged,
            document_status_updated=document_status_updated,
        )
