"""Offline unit tests for Transactional Spanner Enrichment Writer.

Tests transaction rollback, idempotency, concurrent ownership conflicts,
approved-card protection, shared-node preservation, evidence validation,
all 11 tables, and failure before mutation buffering.
"""

from unittest.mock import MagicMock
import pytest

from common.database.enrichment_writer import (
    ApprovedSolutionCardProtectionError,
    ChunkOwnershipConflictError,
    DocumentNotFoundError,
    EnrichmentPayload,
    EnrichmentPersistenceError,
    EnrichmentWriteResult,
    EnrichmentWriter,
    ReferentialIntegrityError,
    SchemaConstraintError,
    are_embeddings_equal,
    format_okf_uri,
    generate_evidence_link_id,
)
from common.models.schema import (
    ChunkNode,
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


# ============================================================================
# IN-MEMORY SPANNER MOCK FIXTURE
# ============================================================================


class MockSpannerTransaction:
    """Mock Spanner read-write transaction tracking buffered mutations and sql queries."""

    def __init__(self, state: dict[str, list[tuple]]):
        self.state = state  # table_name -> list of row tuples
        self.inserts: dict[str, list[dict[str, Any]]] = {}
        self.updates: dict[str, list[dict[str, Any]]] = {}
        self.sql_queries: list[str] = []

    def execute_sql(self, sql: str, params: dict | None = None, param_types: dict | None = None):
        self.sql_queries.append(sql.strip())
        norm_sql = " ".join(sql.strip().split()).upper()

        if "FROM DOCUMENTS WHERE DOCUMENTID = @DOC_ID" in norm_sql:
            doc_id = params.get("doc_id")
            for row in self.state.get("Documents", []):
                if row[0] == doc_id:
                    return [row]
            return []

        if "FROM EVIDENCECHUNKS WHERE DOCUMENTID = @DOC_ID" in norm_sql:
            doc_id = params.get("doc_id")
            results = []
            for row in self.state.get("EvidenceChunks", []):
                # row format: (ChunkId, Modality, TextContent, PageNumber, SlideNumber, Embedding768, DocumentId)
                if len(row) > 6 and row[6] == doc_id:
                    results.append((row[0], row[1], row[2], row[3], row[4], row[5]))
                elif len(row) == 6:  # default test rows for this doc
                    results.append(row)
            return results

        if "FROM EVIDENCECHUNKS WHERE CHUNKID IN UNNEST(@CIDS)" in norm_sql:
            cids = set(params.get("cids", []))
            results = []
            for row in self.state.get("EvidenceChunks", []):
                if row[0] in cids:
                    doc_owner = row[6] if len(row) > 6 else "doc_acme"
                    results.append((row[0], doc_owner))
            return results

        if "FROM ENTITIES WHERE ENTITYID IN UNNEST(@EIDS)" in norm_sql:
            eids = set(params.get("eids", []))
            return [r for r in self.state.get("Entities", []) if r[0] in eids]

        if "FROM TECHNOLOGIES WHERE TECHID IN UNNEST(@TIDS)" in norm_sql:
            tids = set(params.get("tids", []))
            return [r for r in self.state.get("Technologies", []) if r[0] in tids]

        if "FROM SOLUTIONCARDS WHERE DOCUMENTID = @DOC_ID" in norm_sql:
            doc_id = params.get("doc_id")
            results = []
            for row in self.state.get("SolutionCards", []):
                if row[1] == doc_id:
                    results.append(row)
            return results

        if "FROM SOLUTIONIMPLEMENTSDOC WHERE DOCUMENTID = @DOC_ID" in norm_sql:
            doc_id = params.get("doc_id")
            return [r for r in self.state.get("SolutionImplementsDoc", []) if r[1] == doc_id]

        if "FROM CHUNKREFERENCESENTITY WHERE CHUNKID IN UNNEST(@CIDS)" in norm_sql:
            cids = set(params.get("cids", []))
            return [r for r in self.state.get("ChunkReferencesEntity", []) if r[0] in cids]

        if "FROM CHUNKMENTIONSTECH WHERE CHUNKID IN UNNEST(@CIDS)" in norm_sql:
            cids = set(params.get("cids", []))
            return [r for r in self.state.get("ChunkMentionsTech", []) if r[0] in cids]

        if "FROM SOLUTIONSUPPORTSTECH WHERE CARDID IN UNNEST(@CIDS)" in norm_sql:
            cids = set(params.get("cids", []))
            return [r for r in self.state.get("SolutionSupportsTech", []) if r[0] in cids]

        if "FROM ENTITYREFERENCESENTITY WHERE SOURCEENTITYID IN UNNEST(@SIDS)" in norm_sql:
            sids = set(params.get("sids", []))
            return [r for r in self.state.get("EntityReferencesEntity", []) if r[0] in sids]

        if "FROM EVIDENCELINKS WHERE EVIDENCEID IN UNNEST(@EV_IDS)" in norm_sql:
            ev_ids = set(params.get("ev_ids", []))
            return [r for r in self.state.get("EvidenceLinks", []) if r[0] in ev_ids]

        return []

    def insert(self, table: str, columns: list[str], values: list[tuple]):
        if table not in self.inserts:
            self.inserts[table] = []
        for val in values:
            self.inserts[table].append(dict(zip(columns, val)))

    def update(self, table: str, columns: list[str], values: list[tuple]):
        if table not in self.updates:
            self.updates[table] = []
        for val in values:
            self.updates[table].append(dict(zip(columns, val)))


class MockSpannerDatabase:
    """Mock database returning a MockSpannerTransaction on run_in_transaction."""

    def __init__(self, initial_state: dict[str, list[tuple]]):
        self.state = initial_state
        self.last_transaction: MockSpannerTransaction | None = None
        self.run_in_transaction_calls = 0

    def run_in_transaction(self, func, *args, **kwargs):
        self.run_in_transaction_calls += 1
        txn = MockSpannerTransaction(self.state)
        self.last_transaction = txn
        return func(txn, *args, **kwargs)


# ============================================================================
# HELPER TEST DATA
# ============================================================================

VALID_768_VEC_1 = [0.1] * 768
VALID_768_VEC_2 = [0.2] * 768


def make_initial_state(
    doc_id: str = "doc_acme",
    doc_status: str = "READY",
    card_status: str = "PENDING_REVIEW",
    has_embeddings: bool = False,
) -> dict[str, list[tuple]]:
    """Build a standard mock database state across required tables."""
    return {
        "Documents": [
            (doc_id, "READY" if doc_status == "READY" else doc_status),
        ],
        "EvidenceChunks": [
            # ChunkId, Modality, TextContent, PageNumber, SlideNumber, Embedding768, DocumentId
            (
                "chk_1",
                "TEXT",
                "Modernized using Cloud Spanner and BigQuery for real-time analytics.",
                1,
                None,
                VALID_768_VEC_1 if has_embeddings else None,
                doc_id,
            ),
            (
                "chk_2",
                "TEXT",
                "Payment Gateway subsystem integrates with Inventory Core service.",
                2,
                None,
                VALID_768_VEC_2 if has_embeddings else None,
                doc_id,
            ),
            (
                "chk_img",
                "IMAGE",
                None,  # No text
                1,
                None,
                None,
                doc_id,
            ),
        ],
        "SolutionCards": [
            # CardId, DocumentId, ProjectName, BusinessProblem, ProposedSolution, ImplementedSolution, Outcome, VerificationStatus, OkfUri
            (
                "card_acme",
                doc_id,
                "Acme Modernization",
                "Old architecture",
                "Cloud Spanner migration",
                "Spanner + BigQuery",
                "High availability",
                card_status,
                None,
            )
        ]
        if card_status != "NONE"
        else [],
        "Entities": [
            ("ent_shared", "Shared Enterprise Concept", "CONCEPT", "A pre-existing shared concept across documents.")
        ],
        "Technologies": [
            ("tech_bigquery", "BigQuery", "Data & Analytics", None)
        ],
        "SolutionImplementsDoc": [("card_acme", doc_id)] if card_status != "NONE" else [],
        "ChunkReferencesEntity": [],
        "ChunkMentionsTech": [],
        "SolutionSupportsTech": [],
        "EntityReferencesEntity": [],
        "EvidenceLinks": [],
    }


# ============================================================================
# 1. TRANSACTION ATOMICITY ACROSS ALL 11 TABLES
# ============================================================================


def test_persist_enrichment_all_11_tables_fresh_success():
    """Verify fresh enrichment buffers mutations across all 11 Spanner tables."""
    state = make_initial_state(doc_id="doc_acme", doc_status="PARSED", card_status="NONE")
    db = MockSpannerDatabase(state)
    writer = EnrichmentWriter(database=db)

    payload = EnrichmentPayload(
        document_id="doc_acme",
        chunk_embeddings={"chk_1": VALID_768_VEC_1, "chk_2": VALID_768_VEC_2},
        entities=[
            ValidatedEntity(
                entity_id="ent_inventory_core",
                name="Inventory Core",
                category="SUBSYSTEM",
                description="Core inventory subsystem",
                chunk_id="chk_2",
                evidence_quote="Inventory Core service",
            ),
            ValidatedEntity(
                entity_id="ent_payment_gateway",
                name="Payment Gateway",
                category="SUBSYSTEM",
                description="Payment processor",
                chunk_id="chk_2",
                evidence_quote="Payment Gateway subsystem",
            ),
        ],
        technologies=[
            ValidatedTechnology(
                raw_name="Cloud Spanner",
                chunk_id="chk_1",
                evidence_quote="Cloud Spanner",
                tech_id="tech_cloud_spanner",
                canonical_name="Cloud Spanner",
                category="Database",
                is_canonical=True,
                unresolved=False,
            ),
            ValidatedTechnology(
                raw_name="BigQuery",
                chunk_id="chk_1",
                evidence_quote="BigQuery",
                tech_id="tech_bigquery",
                canonical_name="BigQuery",
                category="Data & Analytics",
                is_canonical=True,
                unresolved=False,
            ),
        ],
        solution_cards=[
            ValidatedSolutionCard(
                card_id="card_acme",
                document_id="doc_acme",
                project_name="Acme Modernization",
                business_problem="Latency",
                proposed_solution="Spanner",
                implemented_solution="Spanner + BQ",
                outcome="Reduced latency",
                chunk_id="chk_1",
                evidence_quote="Modernized using Cloud Spanner",
                technologies=["tech_cloud_spanner", "tech_bigquery"],
            )
        ],
        relationships=[
            ValidatedEntityRelationship(
                source_entity_id="ent_payment_gateway",
                target_entity_id="ent_inventory_core",
                source_name="Payment Gateway",
                target_name="Inventory Core",
                relationship_type="INTEGRATES_WITH",
                chunk_id="chk_2",
                evidence_quote="Payment Gateway subsystem integrates with Inventory Core service",
            )
        ],
        sidecar_uri="gs://bucket/meta/doc_acme.meta.json",
        sidecar_sha256="abc123sha",
    )

    result = writer.persist_enrichment(payload)

    assert result.status == "ENRICHED"
    assert result.total_mutations > 0
    assert result.chunks_embeddings_updated == 2
    assert result.entities_inserted == 2
    assert result.technologies_inserted == 1  # BigQuery was in initial_state, Spanner inserted
    assert result.technologies_unchanged == 1
    assert result.solution_cards_inserted == 1
    assert result.edges_inserted == 8  # 1xSID + 2xCRE + 2xCMT + 2xSST + 1xERE = 8
    assert result.evidence_links_inserted == 1
    assert result.document_status_updated is True

    txn = db.last_transaction
    assert "EvidenceChunks" in txn.updates
    assert "Entities" in txn.inserts
    assert "Technologies" in txn.inserts
    assert "SolutionCards" in txn.inserts
    assert "SolutionImplementsDoc" in txn.inserts
    assert "ChunkReferencesEntity" in txn.inserts
    assert "ChunkMentionsTech" in txn.inserts
    assert "SolutionSupportsTech" in txn.inserts
    assert "EntityReferencesEntity" in txn.inserts
    assert "EvidenceLinks" in txn.inserts
    assert "Documents" in txn.updates


# ============================================================================
# 2. DETERMINISTIC IDEMPOTENCY & ZERO MUTATIONS
# ============================================================================


def test_idempotent_rerun_produces_zero_mutations():
    """Verify rerunning enrichment with identical records buffers 0 mutations."""
    # Setup state with everything already enriched
    state = make_initial_state(doc_id="doc_acme", doc_status="ENRICHED", card_status="PENDING_REVIEW", has_embeddings=True)
    # Add existing entities and tech to state
    state["Entities"].append(("ent_inventory_core", "Inventory Core", "SUBSYSTEM", "Core inventory subsystem"))
    state["Technologies"].append(("tech_cloud_spanner", "Cloud Spanner", "Database", None))
    state["ChunkReferencesEntity"].append(("chk_2", "ent_inventory_core"))
    state["ChunkMentionsTech"].append(("chk_1", "tech_cloud_spanner"))
    state["SolutionSupportsTech"].append(("card_acme", "tech_cloud_spanner"))
    state["SolutionImplementsDoc"].append(("card_acme", "doc_acme"))

    ev_id = generate_evidence_link_id("card_acme", "chk_1", "Modernized using Cloud Spanner")
    state["EvidenceLinks"].append((ev_id, "card_acme", "chk_1", "doc_acme", 1, None, "Modernized using Cloud Spanner"))

    db = MockSpannerDatabase(state)
    writer = EnrichmentWriter(database=db)

    payload = EnrichmentPayload(
        document_id="doc_acme",
        chunk_embeddings={"chk_1": VALID_768_VEC_1},
        entities=[
            ValidatedEntity(
                entity_id="ent_inventory_core",
                name="Inventory Core",
                category="SUBSYSTEM",
                description="Core inventory subsystem",
                chunk_id="chk_2",
                evidence_quote="Inventory Core service",
            )
        ],
        technologies=[
            ValidatedTechnology(
                raw_name="Cloud Spanner",
                chunk_id="chk_1",
                evidence_quote="Cloud Spanner",
                tech_id="tech_cloud_spanner",
                canonical_name="Cloud Spanner",
                category="Database",
                is_canonical=True,
                unresolved=False,
            )
        ],
        solution_cards=[
            ValidatedSolutionCard(
                card_id="card_acme",
                document_id="doc_acme",
                project_name="Acme Modernization",
                business_problem="Old architecture",
                proposed_solution="Cloud Spanner migration",
                implemented_solution="Spanner + BigQuery",
                outcome="High availability",
                chunk_id="chk_1",
                evidence_quote="Modernized using Cloud Spanner",
                technologies=["tech_cloud_spanner"],
            )
        ],
    )

    result = writer.persist_enrichment(payload, force_recompute=False)

    assert result.status == "UNCHANGED"
    assert result.total_mutations == 0
    assert result.chunks_embeddings_updated == 0
    assert result.chunks_embeddings_unchanged == 1
    assert result.entities_inserted == 0
    assert result.entities_unchanged == 1
    assert result.technologies_inserted == 0
    assert result.technologies_unchanged == 1
    assert result.solution_cards_inserted == 0
    assert result.solution_cards_updated == 0
    assert result.solution_cards_unchanged == 1
    assert result.edges_inserted == 0
    assert result.evidence_links_inserted == 0
    assert result.document_status_updated is False

    txn = db.last_transaction
    assert len(txn.inserts) == 0
    assert len(txn.updates) == 0


# ============================================================================
# 3. CONCURRENT OWNERSHIP CONFLICTS
# ============================================================================


def test_chunk_ownership_conflict_raises_error():
    """Verify attempting to enrich a chunk belonging to another document raises ChunkOwnershipConflictError."""
    state = make_initial_state(doc_id="doc_acme")
    # Add a chunk belonging to doc_other
    state["EvidenceChunks"].append(
        ("chk_foreign_99", "TEXT", "Some foreign text", 1, None, None, "doc_other")
    )

    db = MockSpannerDatabase(state)
    writer = EnrichmentWriter(database=db)

    payload = EnrichmentPayload(
        document_id="doc_acme",
        chunk_embeddings={"chk_foreign_99": VALID_768_VEC_1},
    )

    with pytest.raises(ChunkOwnershipConflictError) as exc_info:
        writer.persist_enrichment(payload)

    assert "chk_foreign_99" in str(exc_info.value)
    assert "doc_other" in str(exc_info.value)


# ============================================================================
# 4. APPROVED-CARD PROTECTION
# ============================================================================


def test_approved_solution_card_protected():
    """Verify human-approved SolutionCards (VerificationStatus='APPROVED') are never overwritten."""
    # Card is APPROVED in Spanner
    state = make_initial_state(doc_id="doc_acme", card_status="APPROVED")
    db = MockSpannerDatabase(state)
    writer = EnrichmentWriter(database=db)

    payload = EnrichmentPayload(
        document_id="doc_acme",
        solution_cards=[
            ValidatedSolutionCard(
                card_id="card_acme",
                document_id="doc_acme",
                project_name="Different Project Name",
                business_problem="Attempted overwrite of approved card",
                proposed_solution="Attempted overwrite",
                implemented_solution="Attempted overwrite",
                outcome="Attempted overwrite",
                chunk_id="chk_1",
                evidence_quote="Modernized using Cloud Spanner",
            )
        ],
    )

    result = writer.persist_enrichment(payload, force_recompute=True)

    assert result.solution_cards_protected == 1
    assert result.solution_cards_updated == 0
    assert result.solution_cards_inserted == 0

    txn = db.last_transaction
    # No updates to SolutionCards table
    assert "SolutionCards" not in txn.updates


# ============================================================================
# 5. SHARED-NODE PRESERVATION
# ============================================================================


def test_shared_nodes_preserved_never_deleted():
    """Verify existing shared entities and technologies in Spanner are preserved."""
    state = make_initial_state(doc_id="doc_acme")
    db = MockSpannerDatabase(state)
    writer = EnrichmentWriter(database=db)

    payload = EnrichmentPayload(
        document_id="doc_acme",
        entities=[
            ValidatedEntity(
                entity_id="ent_shared",
                name="Shared Enterprise Concept",
                category="CONCEPT",
                description="A pre-existing shared concept across documents.",
                chunk_id="chk_1",
                evidence_quote="Modernized using Cloud Spanner",
            )
        ],
        technologies=[
            ValidatedTechnology(
                raw_name="BigQuery",
                chunk_id="chk_1",
                evidence_quote="BigQuery",
                tech_id="tech_bigquery",
                canonical_name="BigQuery",
                category="Data & Analytics",
                is_canonical=True,
                unresolved=False,
            )
        ],
    )

    result = writer.persist_enrichment(payload)

    assert result.entities_unchanged == 1
    assert result.entities_inserted == 0
    assert result.technologies_unchanged == 1
    assert result.technologies_inserted == 0


# ============================================================================
# 6. EVIDENCE VALIDATION & IMMUTABILITY
# ============================================================================


def test_image_chunks_excluded_from_embeddings():
    """Verify IMAGE chunks or empty chunks are never updated with text embeddings."""
    state = make_initial_state(doc_id="doc_acme")
    db = MockSpannerDatabase(state)
    writer = EnrichmentWriter(database=db)

    # Provide embedding for IMAGE chunk 'chk_img'
    payload = EnrichmentPayload(
        document_id="doc_acme",
        chunk_embeddings={"chk_img": VALID_768_VEC_1},
    )

    result = writer.persist_enrichment(payload)
    assert result.chunks_embeddings_updated == 0

    txn = db.last_transaction
    assert "EvidenceChunks" not in txn.updates


def test_invalid_embedding_dimension_raises_error():
    """Verify vector with length != 768 raises SchemaConstraintError before buffering."""
    state = make_initial_state(doc_id="doc_acme")
    db = MockSpannerDatabase(state)
    writer = EnrichmentWriter(database=db)

    payload = EnrichmentPayload(
        document_id="doc_acme",
        chunk_embeddings={"chk_1": [0.1] * 512},  # 512 dimensions invalid
    )

    with pytest.raises(SchemaConstraintError) as exc_info:
        writer.persist_enrichment(payload)

    assert "Invalid embedding dimension (512)" in str(exc_info.value)


def test_document_not_found_raises_error():
    """Verify non-existent DocumentId raises DocumentNotFoundError before buffering."""
    state = make_initial_state(doc_id="doc_existing")
    db = MockSpannerDatabase(state)
    writer = EnrichmentWriter(database=db)

    payload = EnrichmentPayload(
        document_id="doc_non_existent",
    )

    with pytest.raises(DocumentNotFoundError):
        writer.persist_enrichment(payload)


def test_referential_integrity_unknown_chunk_in_edge():
    """Verify edge citing non-existent chunk raises ReferentialIntegrityError."""
    state = make_initial_state(doc_id="doc_acme")
    db = MockSpannerDatabase(state)
    writer = EnrichmentWriter(database=db)

    payload = EnrichmentPayload(
        document_id="doc_acme",
        entities=[
            ValidatedEntity(
                entity_id="ent_valid",
                name="Valid Entity",
                category="CONCEPT",
                chunk_id="chk_not_in_doc",
                evidence_quote="Quote",
            )
        ],
    )

    with pytest.raises(ReferentialIntegrityError):
        writer.persist_enrichment(payload)


def test_evidence_links_and_okf_uri_format():
    """Verify EvidenceLinks citation row is generated and OkfUri formatted correctly."""
    assert format_okf_uri("gs://b/meta.json", "sha123") == "gs://b/meta.json#sha256:sha123"
    assert format_okf_uri("gs://b/meta.json", None) == "gs://b/meta.json"
    assert format_okf_uri(None, "sha123") == "#sha256:sha123"
    assert format_okf_uri(None, None) is None

    ev_id1 = generate_evidence_link_id("card_1", "chk_1", "Quote text")
    ev_id2 = generate_evidence_link_id("card_1", "chk_1", "Quote text")
    assert ev_id1 == ev_id2
    assert ev_id1.startswith("evl_")
    assert len(ev_id1) == 52


def test_embeddings_comparison_tolerance():
    """Verify are_embeddings_equal handles floating point comparisons within tolerance."""
    vec1 = [0.1234567, 0.9876543]
    vec2 = [0.1234568, 0.9876542]
    vec3 = [0.123, 0.987]

    assert are_embeddings_equal(vec1, vec2, tol=1e-5) is True
    assert are_embeddings_equal(vec1, vec3, tol=1e-5) is False
    assert are_embeddings_equal(None, None) is True
    assert are_embeddings_equal(vec1, None) is False


def test_transaction_rollback_on_failure():
    """Verify any error inside transaction prevents commit and aborts cleanly."""
    state = make_initial_state(doc_id="doc_acme")
    db = MockSpannerDatabase(state)
    writer = EnrichmentWriter(database=db)

    # Payload with an invalid embedding dimension
    payload = EnrichmentPayload(
        document_id="doc_acme",
        chunk_embeddings={"chk_1": [0.1] * 100},  # Invalid
    )

    with pytest.raises(SchemaConstraintError):
        writer.persist_enrichment(payload)

    # Verify no inserts or updates were committed
    txn = db.last_transaction
    assert len(txn.inserts) == 0
    assert len(txn.updates) == 0


def test_force_recompute_updates_unapproved_solution_card():
    """Verify force_recompute=True updates fields on an unapproved (PENDING_REVIEW) card."""
    state = make_initial_state(doc_id="doc_acme", card_status="PENDING_REVIEW")
    db = MockSpannerDatabase(state)
    writer = EnrichmentWriter(database=db)

    payload = EnrichmentPayload(
        document_id="doc_acme",
        solution_cards=[
            ValidatedSolutionCard(
                card_id="card_acme",
                document_id="doc_acme",
                project_name="Acme Modernization v2",
                business_problem="Updated problem",
                proposed_solution="Updated proposed",
                implemented_solution="Updated implemented",
                outcome="Updated outcome",
                chunk_id="chk_1",
                evidence_quote="Modernized using Cloud Spanner",
            )
        ],
    )

    result = writer.persist_enrichment(payload, force_recompute=True)
    assert result.solution_cards_updated == 1
    assert result.solution_cards_protected == 0

    txn = db.last_transaction
    assert "SolutionCards" in txn.updates
    assert txn.updates["SolutionCards"][0]["ProjectName"] == "Acme Modernization v2"

