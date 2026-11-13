"""Offline unit tests for Enrichment Orchestrator and Manual CLI.

Tests dry-run, checkpoint skipping, IMAGE exclusion, failure recovery at each stage,
idempotency, force-recompute, approved-card protection, and GCS sidecar staging/promotion.
"""

import hashlib
import json
from unittest.mock import MagicMock
import pytest

from app.enrichment_orchestrator import (
    EnrichmentOrchestrator,
    EnrichmentOrchestratorResult,
    EnrichmentStage,
    EnrichmentStatus,
)
from common.database.enrichment_writer import (
    EnrichmentPersistenceError,
    EnrichmentWriteResult,
    EnrichmentWriter,
)
from common.embeddings.vertex_client import VertexEmbeddingsClient
from common.models.schema import ChunkNode
from common.parsers.metadata_extractor import (
    ExtractedMetadataResult,
    MetadataExtractor,
    ValidatedEntity,
    ValidatedEntityRelationship,
    ValidatedSolutionCard,
    ValidatedTechnology,
)
from common.taxonomy.taxonomy_mapper import TaxonomyMapper


# ============================================================================
# MOCK FIXTURES & HELPERS
# ============================================================================


class MockBlob:
    """Mock GCS storage Blob."""

    def __init__(self, name: str, bucket_name: str, exists: bool = False, content: bytes = b""):
        self.name = name
        self.bucket_name = bucket_name
        self._exists = exists
        self.content = content
        self.metadata: dict[str, str] = {}

    def exists(self) -> bool:
        return self._exists

    def upload_from_string(self, data: bytes, content_type: str = "application/json"):
        self.content = data
        self._exists = True

    def download_as_bytes(self) -> bytes:
        return self.content

    def delete(self):
        self._exists = False


class MockBucket:
    """Mock GCS storage Bucket."""

    def __init__(self, name: str):
        self.name = name
        self.blobs: dict[str, MockBlob] = {}

    def blob(self, name: str) -> MockBlob:
        if name not in self.blobs:
            self.blobs[name] = MockBlob(name, self.name)
        return self.blobs[name]


class MockStorageClient:
    """Mock GCS storage Client."""

    def __init__(self):
        self.buckets: dict[str, MockBucket] = {}

    def bucket(self, name: str) -> MockBucket:
        if name not in self.buckets:
            self.buckets[name] = MockBucket(name)
        return self.buckets[name]


class MockSpannerSnapshot:
    """Mock Spanner read-only snapshot for queries."""

    def __init__(self, state: dict[str, list[tuple]]):
        self.state = state

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def execute_sql(self, sql: str, params: dict | None = None, param_types: dict | None = None):
        norm = " ".join(sql.strip().split()).upper()

        if "FROM DOCUMENTS WHERE DOCUMENTID = @DOC_ID" in norm:
            doc_id = params.get("doc_id")
            for row in self.state.get("Documents", []):
                if row[0] == doc_id:
                    return [[row[1]]]  # Status column
            return []

        if "FROM EVIDENCECHUNKS WHERE DOCUMENTID = @DOC_ID" in norm:
            doc_id = params.get("doc_id")
            rows = [r for r in self.state.get("EvidenceChunks", []) if r[1] == doc_id]
            if "EMBEDDING768" in norm:
                # Query: SELECT ChunkId, Modality, TextContent, Embedding768
                return [(r[0], r[2], r[3], r[7] if len(r) > 7 else None) for r in rows]
            else:
                # Query: SELECT ChunkId, DocumentId, Modality, TextContent, PageNumber, SlideNumber, SourceGcsUri
                return [(r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for r in rows]

        if "FROM SOLUTIONCARDS WHERE DOCUMENTID = @DOC_ID" in norm:
            doc_id = params.get("doc_id")
            rows = [r for r in self.state.get("SolutionCards", []) if len(r) == 2 or r[1] == doc_id]
            return [(r[0], r[1] if len(r) == 2 else r[2]) for r in rows]

        if "FROM SOLUTIONIMPLEMENTSDOC WHERE DOCUMENTID = @DOC_ID" in norm:
            doc_id = params.get("doc_id")
            rows = [r for r in self.state.get("SolutionImplementsDoc", []) if len(r) < 2 or r[1] == doc_id]
            return [(r[0], r[1] if len(r) > 1 else doc_id) for r in rows]

        if "FROM EVIDENCELINKS WHERE DOCUMENTID = @DOC_ID" in norm:
            doc_id = params.get("doc_id")
            if "SELECT COUNT(1)" in norm:
                return [[len(self.state.get("EvidenceLinks", []))]]
            rows = [r for r in self.state.get("EvidenceLinks", []) if len(r) < 4 or r[3] == doc_id]
            out = []
            for r in rows:
                ev_id = r[0]
                c_id = r[1] if len(r) > 1 else "card_1"
                chk_id = r[2] if len(r) > 2 else "chk_1"
                note = r[6] if len(r) > 6 else (r[3] if len(r) == 4 else None)
                out.append((ev_id, c_id, chk_id, note))
            return out

        return []


class MockSpannerDb:
    """Mock Spanner database instance."""

    def __init__(self, state: dict[str, list[tuple]]):
        self.state = state

    def snapshot(self, multi_use: bool = True):
        return MockSpannerSnapshot(self.state)


VALID_VEC = [0.1] * 768


def build_valid_sidecar_json(
    doc_id: str,
    card_ids: list[str] | None = None,
    chunk_ids: list[str] | None = None,
) -> bytes:
    """Build valid JSON bytes matching the complete provenance manifest."""
    cards = card_ids or ["card_1"]
    chunks = chunk_ids or ["chk_1"] * len(cards)

    data = {
        "document_id": doc_id,
        "solution_cards": [
            {
                "card_id": cid,
                "project_name": "Test Modernization",
                "business_problem": "Latency",
                "proposed_solution": "Spanner",
                "implemented_solution": "Spanner + BigQuery",
                "outcome": "Success",
                "chunk_id": chk,
                "evidence_quote": f"Quote for {cid}",
                "technologies": ["tech_cloud_spanner"],
            }
            for cid, chk in zip(cards, chunks)
        ],
        "citations": [
            {
                "evidence_id": f"evl_{cid}_{chk}",
                "card_id": cid,
                "chunk_id": chk,
                "evidence_quote": f"Quote for {cid}",
            }
            for cid, chk in zip(cards, chunks)
        ],
    }
    return json.dumps(data).encode("utf-8")


def build_mock_metadata_result(doc_id: str) -> ExtractedMetadataResult:
    """Construct valid mock ExtractedMetadataResult."""
    return ExtractedMetadataResult(
        document_id=doc_id,
        entities=[
            ValidatedEntity(
                entity_id="ent_inventory_core",
                name="Inventory Core",
                category="SUBSYSTEM",
                description="Core inventory subsystem",
                chunk_id="chk_1",
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
                document_id=doc_id,
                project_name="Acme Modernization",
                business_problem="Latency",
                proposed_solution="Spanner",
                implemented_solution="Spanner + BigQuery",
                outcome="Reduced latency",
                chunk_id="chk_1",
                evidence_quote="Modernized using Cloud Spanner",
                technologies=["tech_cloud_spanner"],
            )
        ],
        relationships=[],
        unresolved_technologies=[],
        rejected_candidates=[],
    )


# ============================================================================
# 1. DRY RUN VERIFICATION
# ============================================================================


def test_enrichment_orchestrator_dry_run():
    """Verify dry-run simulates discovery, extraction, embeddings, and projections with 0 writes."""
    doc_id = "doc_test_dryrun"
    spanner_state = {
        "Documents": [(doc_id, "PARSED")],
        "EvidenceChunks": [
            ("chk_1", doc_id, "TEXT", "Modernized using Cloud Spanner for high availability.", 1, None, None),
            ("chk_2", doc_id, "TEXT", "Inventory Core service handles orders.", 2, None, None),
            ("chk_img", doc_id, "IMAGE", None, 1, None, None),  # Excluded IMAGE chunk
        ],
        "SolutionCards": [],
        "EvidenceLinks": [],
    }

    mock_db = MockSpannerDb(spanner_state)
    mock_storage = MockStorageClient()

    mock_embeddings = MagicMock(spec=VertexEmbeddingsClient)
    mock_embeddings.embed_texts.return_value = [VALID_VEC, VALID_VEC]

    mock_extractor = MagicMock(spec=MetadataExtractor)
    mock_extractor.extract_from_document.return_value = build_mock_metadata_result(doc_id)

    mock_writer = MagicMock(spec=EnrichmentWriter)

    orchestrator = EnrichmentOrchestrator(
        spanner_database=mock_db,
        storage_client=mock_storage,
        embeddings_client=mock_embeddings,
        metadata_extractor=mock_extractor,
        enrichment_writer=mock_writer,
    )

    result = orchestrator.run(doc_id, dry_run=True)

    assert result.status == EnrichmentStatus.DRY_RUN
    assert result.stage == EnrichmentStage.COMPLETED
    assert result.dry_run is True
    assert result.eligible_chunks_count == 2
    assert result.excluded_image_chunks_count == 1
    assert result.embeddings_generated_count == 2
    assert result.entities_count == 1
    assert result.technologies_count == 1
    assert result.solution_cards_count == 1
    assert result.projected_mutations > 0

    # Ensure zero writes to Spanner and GCS
    mock_writer.persist_enrichment.assert_not_called()
    bucket = mock_storage.bucket(orchestrator.settings.gcs_mumbai_bucket)
    assert len(bucket.blobs) == 0


# ============================================================================
# 2. FOUR-POINT COMPLETION CHECKPOINT SKIPPING
# ============================================================================


def test_checkpoint_skips_already_completed_document():
    """Verify already-enriched document passing all 4 checkpoint criteria is skipped without model calls."""
    doc_id = "doc_already_done"
    sidecar_data = build_valid_sidecar_json(doc_id, ["card_1"], ["chk_1"])
    sidecar_hash = hashlib.sha256(sidecar_data).hexdigest()
    sidecar_uri = f"gs://sales-discovery-mvp-mumbai/metadata/{doc_id}.meta.json"

    # Set up GCS blob with matching hash
    mock_storage = MockStorageClient()
    bucket = mock_storage.bucket("sales-discovery-mvp-mumbai")
    blob = bucket.blob(f"metadata/{doc_id}.meta.json")
    blob.upload_from_string(sidecar_data)

    spanner_state = {
        "Documents": [(doc_id, "ENRICHED")],
        "EvidenceChunks": [
            ("chk_1", doc_id, "TEXT", "Content 1", 1, None, None, VALID_VEC),
            ("chk_2", doc_id, "TEXT", "Content 2", 2, None, None, VALID_VEC),
            ("chk_img", doc_id, "IMAGE", None, 1, None, None, None),  # IMAGE excluded
        ],
        "SolutionCards": [
            ("card_1", f"{sidecar_uri}#sha256={sidecar_hash}")
        ],
        "SolutionImplementsDoc": [("card_1", doc_id)],
        "EvidenceLinks": [("evl_card_1_chk_1", "card_1", "chk_1", doc_id, 1, None, "Quote for card_1")],
    }

    mock_db = MockSpannerDb(spanner_state)
    mock_embeddings = MagicMock(spec=VertexEmbeddingsClient)
    mock_extractor = MagicMock(spec=MetadataExtractor)
    mock_writer = MagicMock(spec=EnrichmentWriter)

    orchestrator = EnrichmentOrchestrator(
        spanner_database=mock_db,
        storage_client=mock_storage,
        embeddings_client=mock_embeddings,
        metadata_extractor=mock_extractor,
        enrichment_writer=mock_writer,
    )

    result = orchestrator.run(doc_id, dry_run=False, force_recompute=False)

    assert result.status == EnrichmentStatus.SKIPPED
    assert result.stage == EnrichmentStage.COMPLETED
    assert result.checkpoint_details["all_points_passed"] is True

    # 0 model calls
    mock_embeddings.embed_texts.assert_not_called()
    mock_extractor.extract_from_document.assert_not_called()
    mock_writer.persist_enrichment.assert_not_called()


def test_force_recompute_bypasses_checkpoint():
    """Verify force_recompute=True re-enriches even when checkpoint is complete."""
    doc_id = "doc_force_recompute"
    sidecar_data = build_valid_sidecar_json(doc_id, ["card_1"], ["chk_1"])
    sidecar_hash = hashlib.sha256(sidecar_data).hexdigest()
    sidecar_uri = f"gs://sales-discovery-mvp-mumbai/metadata/{doc_id}.meta.json"

    mock_storage = MockStorageClient()
    bucket = mock_storage.bucket("sales-discovery-mvp-mumbai")
    blob = bucket.blob(f"metadata/{doc_id}.meta.json")
    blob.upload_from_string(sidecar_data)

    spanner_state = {
        "Documents": [(doc_id, "ENRICHED")],
        "EvidenceChunks": [
            ("chk_1", doc_id, "TEXT", "Content 1", 1, None, None, VALID_VEC),
        ],
        "SolutionCards": [
            ("card_1", f"{sidecar_uri}#sha256={sidecar_hash}")
        ],
        "SolutionImplementsDoc": [("card_1", doc_id)],
        "EvidenceLinks": [("evl_card_1_chk_1", "card_1", "chk_1", doc_id, 1, None, "Quote for card_1")],
    }

    mock_db = MockSpannerDb(spanner_state)
    mock_embeddings = MagicMock(spec=VertexEmbeddingsClient)
    mock_embeddings.embed_texts.return_value = [VALID_VEC]
    mock_extractor = MagicMock(spec=MetadataExtractor)
    mock_extractor.extract_from_document.return_value = build_mock_metadata_result(doc_id)

    mock_writer = MagicMock(spec=EnrichmentWriter)
    mock_writer.persist_enrichment.return_value = EnrichmentWriteResult(
        document_id=doc_id,
        status="ENRICHED",
        total_mutations=5,
        chunks_embeddings_updated=1,
        chunks_embeddings_unchanged=0,
        entities_inserted=1,
        entities_updated=0,
        entities_unchanged=0,
        technologies_inserted=1,
        technologies_unchanged=0,
        solution_cards_inserted=0,
        solution_cards_updated=1,
        solution_cards_unchanged=0,
        solution_cards_protected=0,
        edges_inserted=1,
        edges_unchanged=0,
        evidence_links_inserted=1,
        evidence_links_unchanged=0,
        document_status_updated=False,
    )

    orchestrator = EnrichmentOrchestrator(
        spanner_database=mock_db,
        storage_client=mock_storage,
        embeddings_client=mock_embeddings,
        metadata_extractor=mock_extractor,
        enrichment_writer=mock_writer,
    )

    result = orchestrator.run(doc_id, dry_run=False, force_recompute=True)

    assert result.status == EnrichmentStatus.SUCCESS
    assert result.stage == EnrichmentStage.COMPLETED
    mock_embeddings.embed_texts.assert_called_once()
    mock_extractor.extract_from_document.assert_called_once()
    mock_writer.persist_enrichment.assert_called_once()


# ============================================================================
# 3. STAGED GCS SIDECAR & FAILURE RECOVERY
# ============================================================================


def test_staged_gcs_sidecar_workflow_and_promotion():
    """Verify staging upload, Spanner commit, promotion, and staging cleanup."""
    doc_id = "doc_staged_test"
    spanner_state = {
        "Documents": [(doc_id, "PARSED")],
        "EvidenceChunks": [
            ("chk_1", doc_id, "TEXT", "Content 1", 1, None, None),
        ],
        "SolutionCards": [],
        "EvidenceLinks": [],
    }

    mock_db = MockSpannerDb(spanner_state)
    mock_storage = MockStorageClient()
    mock_embeddings = MagicMock(spec=VertexEmbeddingsClient)
    mock_embeddings.embed_texts.return_value = [VALID_VEC]
    mock_extractor = MagicMock(spec=MetadataExtractor)
    mock_extractor.extract_from_document.return_value = build_mock_metadata_result(doc_id)

    mock_writer = MagicMock(spec=EnrichmentWriter)
    mock_writer.persist_enrichment.return_value = EnrichmentWriteResult(
        document_id=doc_id,
        status="ENRICHED",
        total_mutations=8,
        chunks_embeddings_updated=1,
        chunks_embeddings_unchanged=0,
        entities_inserted=1,
        entities_updated=0,
        entities_unchanged=0,
        technologies_inserted=1,
        technologies_unchanged=0,
        solution_cards_inserted=1,
        solution_cards_updated=0,
        solution_cards_unchanged=0,
        solution_cards_protected=0,
        edges_inserted=3,
        edges_unchanged=0,
        evidence_links_inserted=1,
        evidence_links_unchanged=0,
        document_status_updated=True,
    )

    orchestrator = EnrichmentOrchestrator(
        spanner_database=mock_db,
        storage_client=mock_storage,
        embeddings_client=mock_embeddings,
        metadata_extractor=mock_extractor,
        enrichment_writer=mock_writer,
    )

    result = orchestrator.run(doc_id, dry_run=False)

    assert result.status == EnrichmentStatus.SUCCESS
    assert result.sidecar_uri is not None
    assert result.sidecar_sha256 is not None

    bucket = mock_storage.bucket(orchestrator.settings.gcs_mumbai_bucket)
    # Target blob promoted and exists
    final_blob = bucket.blob(f"metadata/{doc_id}.meta.json")
    assert final_blob.exists() is True
    # Staging blob deleted
    staging_blob = bucket.blob(f"metadata/staging/{doc_id}.meta.json.tmp")
    assert staging_blob.exists() is False


def test_failure_at_spanner_persistence_aborts_promotion():
    """Verify failure during Spanner persistence stage aborts pipeline before promotion."""
    doc_id = "doc_spanner_fail"
    spanner_state = {
        "Documents": [(doc_id, "PARSED")],
        "EvidenceChunks": [("chk_1", doc_id, "TEXT", "Content 1", 1, None, None)],
        "SolutionCards": [],
        "EvidenceLinks": [],
    }

    mock_db = MockSpannerDb(spanner_state)
    mock_storage = MockStorageClient()
    mock_embeddings = MagicMock(spec=VertexEmbeddingsClient)
    mock_embeddings.embed_texts.return_value = [VALID_VEC]
    mock_extractor = MagicMock(spec=MetadataExtractor)
    mock_extractor.extract_from_document.return_value = build_mock_metadata_result(doc_id)

    mock_writer = MagicMock(spec=EnrichmentWriter)
    mock_writer.persist_enrichment.side_effect = EnrichmentPersistenceError("Spanner lock timeout")

    orchestrator = EnrichmentOrchestrator(
        spanner_database=mock_db,
        storage_client=mock_storage,
        embeddings_client=mock_embeddings,
        metadata_extractor=mock_extractor,
        enrichment_writer=mock_writer,
    )

    result = orchestrator.run(doc_id, dry_run=False)

    assert result.status == EnrichmentStatus.FAILED
    assert result.stage == EnrichmentStage.PERSISTENCE
    assert "Spanner lock timeout" in result.error_message

    # Final blob was never promoted
    bucket = mock_storage.bucket(orchestrator.settings.gcs_mumbai_bucket)
    final_blob = bucket.blob(f"metadata/{doc_id}.meta.json")
    assert final_blob.exists() is False


# ============================================================================
# 4. CHUNKS FILTERING (IMAGE & EMPTY CHUNKS)
# ============================================================================


def test_no_eligible_chunks_skips_cleanly():
    """Verify document with only IMAGE chunks or empty chunks skips model calls cleanly."""
    doc_id = "doc_only_images"
    spanner_state = {
        "Documents": [(doc_id, "PARSED")],
        "EvidenceChunks": [
            ("chk_img_1", doc_id, "IMAGE", None, 1, None, None),
            ("chk_img_2", doc_id, "IMAGE", "   ", 2, None, None),
        ],
        "SolutionCards": [],
        "EvidenceLinks": [],
    }

    mock_db = MockSpannerDb(spanner_state)
    mock_storage = MockStorageClient()
    mock_embeddings = MagicMock(spec=VertexEmbeddingsClient)
    mock_extractor = MagicMock(spec=MetadataExtractor)
    mock_writer = MagicMock(spec=EnrichmentWriter)

    orchestrator = EnrichmentOrchestrator(
        spanner_database=mock_db,
        storage_client=mock_storage,
        embeddings_client=mock_embeddings,
        metadata_extractor=mock_extractor,
        enrichment_writer=mock_writer,
    )

    result = orchestrator.run(doc_id, dry_run=False)

    assert result.status == EnrichmentStatus.SKIPPED
    assert result.eligible_chunks_count == 0
    assert result.excluded_image_chunks_count == 2
    mock_embeddings.embed_texts.assert_not_called()
    mock_extractor.extract_from_document.assert_not_called()
    mock_writer.persist_enrichment.assert_not_called()


# ============================================================================
# 5. CHECKPOINT POINT 4 FAILURE & APPROVED-CARD PROTECTION
# ============================================================================


def test_checkpoint_fails_when_sidecar_missing_in_gcs():
    """Verify Point 4 fails when sidecar blob does not exist in GCS."""
    doc_id = "doc_missing_sidecar"
    valid_hash = "a" * 64
    sidecar_uri = f"gs://sales-discovery-mvp-mumbai/metadata/{doc_id}.meta.json#sha256={valid_hash}"

    mock_storage = MockStorageClient()  # Empty storage: blob does not exist
    spanner_state = {
        "Documents": [(doc_id, "ENRICHED")],
        "EvidenceChunks": [
            ("chk_1", doc_id, "TEXT", "Content", 1, None, None, VALID_VEC),
        ],
        "SolutionCards": [("card_1", sidecar_uri)],
        "SolutionImplementsDoc": [("card_1", doc_id)],
        "EvidenceLinks": [("evl_1", "card_1", "chk_1", doc_id, 1, None, "Content")],
    }

    mock_db = MockSpannerDb(spanner_state)
    orchestrator = EnrichmentOrchestrator(
        spanner_database=mock_db,
        storage_client=mock_storage,
    )

    passed, details = orchestrator.check_completion_checkpoint(doc_id)
    assert passed is False
    assert details["status_enriched"] is True
    assert details["sidecar_gcs_exists"] is False
    assert details["all_points_passed"] is False


def test_approved_card_protection_reported_in_orchestration():
    """Verify write_result reports solution_cards_protected when approved card exists."""
    doc_id = "doc_approved_card"
    spanner_state = {
        "Documents": [(doc_id, "PARSED")],
        "EvidenceChunks": [("chk_1", doc_id, "TEXT", "Content", 1, None, None, None)],
        "SolutionCards": [],
        "EvidenceLinks": [],
    }

    mock_db = MockSpannerDb(spanner_state)
    mock_storage = MockStorageClient()
    mock_embeddings = MagicMock(spec=VertexEmbeddingsClient)
    mock_embeddings.embed_texts.return_value = [VALID_VEC]
    mock_extractor = MagicMock(spec=MetadataExtractor)
    mock_extractor.extract_from_document.return_value = build_mock_metadata_result(doc_id)

    mock_writer = MagicMock(spec=EnrichmentWriter)
    mock_writer.persist_enrichment.return_value = EnrichmentWriteResult(
        document_id=doc_id,
        status="ENRICHED",
        total_mutations=1,
        chunks_embeddings_updated=1,
        chunks_embeddings_unchanged=0,
        entities_inserted=0,
        entities_updated=0,
        entities_unchanged=0,
        technologies_inserted=0,
        technologies_unchanged=0,
        solution_cards_inserted=0,
        solution_cards_updated=0,
        solution_cards_unchanged=1,
        solution_cards_protected=1,  # Protected
        edges_inserted=0,
        edges_unchanged=0,
        evidence_links_inserted=0,
        evidence_links_unchanged=0,
        document_status_updated=False,
    )

    orchestrator = EnrichmentOrchestrator(
        spanner_database=mock_db,
        storage_client=mock_storage,
        embeddings_client=mock_embeddings,
        metadata_extractor=mock_extractor,
        enrichment_writer=mock_writer,
    )

    result = orchestrator.run(doc_id, dry_run=False, force_recompute=True)
    assert result.status == EnrichmentStatus.SUCCESS
    assert result.write_result.solution_cards_protected == 1


def test_cli_report_formatter():
    """Verify format_enrichment_result_report generates readable report."""
    from app.enrichment_orchestrator import format_enrichment_result_report

    res = EnrichmentOrchestratorResult(
        document_id="doc_cli_test",
        status=EnrichmentStatus.DRY_RUN,
        stage=EnrichmentStage.COMPLETED,
        dry_run=True,
        duration_ms=125.0,
        eligible_chunks_count=5,
        excluded_image_chunks_count=2,
        embeddings_generated_count=5,
        entities_count=3,
        technologies_count=2,
        unresolved_technologies_count=1,
        solution_cards_count=1,
        relationships_count=1,
        evidence_links_count=1,
        projected_mutations=14,
        sidecar_uri="gs://b/metadata/doc_cli_test.meta.json",
        sidecar_sha256="hash123",
    )

    report = format_enrichment_result_report(res)
    assert "Phase 3C Manual Enrichment Orchestrator Result" in report
    assert "doc_cli_test" in report
    assert "DRY RUN" in report
    assert "Eligible Text Chunks:    5" in report
    assert "Excluded IMAGE Chunks:   2" in report
    assert "Projected Total:         14 mutations" in report


# ============================================================================
# 6. AUDIT REGRESSION TESTS (CHECKPOINT, RECOVERY, EVIDENCELINKS)
# ============================================================================


def test_checkpoint_fails_when_solution_implements_doc_missing():
    """Verify Point 3 fails when a SolutionCard lacks a SolutionImplementsDoc edge."""
    doc_id = "doc_missing_implements_doc"
    sidecar_data = build_valid_sidecar_json(doc_id, ["card_1"], ["chk_1"])
    sidecar_hash = hashlib.sha256(sidecar_data).hexdigest()
    sidecar_uri = f"gs://sales-discovery-mvp-mumbai/metadata/{doc_id}.meta.json"

    mock_storage = MockStorageClient()
    bucket = mock_storage.bucket("sales-discovery-mvp-mumbai")
    bucket.blob(f"metadata/{doc_id}.meta.json").upload_from_string(sidecar_data)

    spanner_state = {
        "Documents": [(doc_id, "ENRICHED")],
        "EvidenceChunks": [("chk_1", doc_id, "TEXT", "Content 1", 1, None, None, VALID_VEC)],
        "SolutionCards": [("card_1", f"{sidecar_uri}#sha256={sidecar_hash}")],
        "SolutionImplementsDoc": [],  # Missing edge!
        "EvidenceLinks": [("evl_card_1_chk_1", "card_1", "chk_1", doc_id, 1, None, "Quote for card_1")],
    }

    orchestrator = EnrichmentOrchestrator(
        spanner_database=MockSpannerDb(spanner_state),
        storage_client=mock_storage,
    )

    passed, details = orchestrator.check_completion_checkpoint(doc_id)
    assert passed is False
    assert details["solution_implements_doc_complete"] is False
    assert "card_1" in details["missing_implements_doc_cards"]
    assert details["all_points_passed"] is False


def test_checkpoint_fails_when_multiple_cards_have_missing_edges():
    """Verify Point 3 fails and reports missing card when one of multiple cards lacks SolutionImplementsDoc."""
    doc_id = "doc_multi_missing_edges"
    cards = ["card_1", "card_2"]
    chunks = ["chk_1", "chk_2"]
    sidecar_data = build_valid_sidecar_json(doc_id, cards, chunks)
    sidecar_hash = hashlib.sha256(sidecar_data).hexdigest()
    sidecar_uri = f"gs://sales-discovery-mvp-mumbai/metadata/{doc_id}.meta.json"

    mock_storage = MockStorageClient()
    bucket = mock_storage.bucket("sales-discovery-mvp-mumbai")
    bucket.blob(f"metadata/{doc_id}.meta.json").upload_from_string(sidecar_data)

    spanner_state = {
        "Documents": [(doc_id, "ENRICHED")],
        "EvidenceChunks": [
            ("chk_1", doc_id, "TEXT", "Content 1", 1, None, None, VALID_VEC),
            ("chk_2", doc_id, "TEXT", "Content 2", 2, None, None, VALID_VEC),
        ],
        "SolutionCards": [
            ("card_1", f"{sidecar_uri}#sha256={sidecar_hash}"),
            ("card_2", f"{sidecar_uri}#sha256={sidecar_hash}"),
        ],
        "SolutionImplementsDoc": [("card_1", doc_id)],  # card_2 missing!
        "EvidenceLinks": [
            ("evl_card_1_chk_1", "card_1", "chk_1", doc_id, 1, None, "Quote for card_1"),
            ("evl_card_2_chk_2", "card_2", "chk_2", doc_id, 2, None, "Quote for card_2"),
        ],
    }

    orchestrator = EnrichmentOrchestrator(
        spanner_database=MockSpannerDb(spanner_state),
        storage_client=mock_storage,
    )

    passed, details = orchestrator.check_completion_checkpoint(doc_id)
    assert passed is False
    assert details["solution_implements_doc_complete"] is False
    assert details["missing_implements_doc_cards"] == ["card_2"]
    assert details["all_points_passed"] is False


def test_rerun_recovery_when_spanner_committed_and_promotion_failed():
    """Verify rerun detects committed Spanner state, verifies staging sidecar, promotes it, and succeeds with 0 model calls."""
    doc_id = "doc_recovery_success"
    sidecar_data = build_valid_sidecar_json(doc_id, ["card_1"], ["chk_1"])
    sidecar_hash = hashlib.sha256(sidecar_data).hexdigest()
    sidecar_uri = f"gs://sales-discovery-mvp-mumbai/metadata/{doc_id}.meta.json"

    # Staging blob exists, canonical blob does NOT exist
    mock_storage = MockStorageClient()
    bucket = mock_storage.bucket("sales-discovery-mvp-mumbai")
    staging_blob = bucket.blob(f"metadata/staging/{doc_id}.meta.json.tmp")
    staging_blob.upload_from_string(sidecar_data)

    spanner_state = {
        "Documents": [(doc_id, "ENRICHED")],
        "EvidenceChunks": [("chk_1", doc_id, "TEXT", "Content 1", 1, None, None, VALID_VEC)],
        "SolutionCards": [("card_1", f"{sidecar_uri}#sha256={sidecar_hash}")],
        "SolutionImplementsDoc": [("card_1", doc_id)],
        "EvidenceLinks": [("evl_card_1_chk_1", "card_1", "chk_1", doc_id, 1, None, "Quote for card_1")],
    }

    mock_db = MockSpannerDb(spanner_state)
    mock_embeddings = MagicMock(spec=VertexEmbeddingsClient)
    mock_extractor = MagicMock(spec=MetadataExtractor)
    mock_writer = MagicMock(spec=EnrichmentWriter)

    orchestrator = EnrichmentOrchestrator(
        spanner_database=mock_db,
        storage_client=mock_storage,
        embeddings_client=mock_embeddings,
        metadata_extractor=mock_extractor,
        enrichment_writer=mock_writer,
    )

    # Before run: canonical does not exist, staging exists
    canonical_blob = bucket.blob(f"metadata/{doc_id}.meta.json")
    assert canonical_blob.exists() is False
    assert staging_blob.exists() is True

    result = orchestrator.run(doc_id, dry_run=False, force_recompute=False)

    assert result.status == EnrichmentStatus.SUCCESS
    assert result.stage == EnrichmentStage.COMPLETED
    assert result.sidecar_sha256 == sidecar_hash

    # Canonical now promoted, staging cleaned up
    assert canonical_blob.exists() is True
    assert staging_blob.exists() is False

    # Zero model calls and zero Spanner mutations
    mock_embeddings.embed_texts.assert_not_called()
    mock_extractor.extract_from_document.assert_not_called()
    mock_writer.persist_enrichment.assert_not_called()


def test_recovery_makes_zero_gemini_and_vertex_calls():
    """Verify recovery explicit verification makes 0 Gemini and 0 Vertex AI calls."""
    doc_id = "doc_recovery_zero_calls"
    sidecar_data = build_valid_sidecar_json(doc_id, ["card_1"], ["chk_1"])
    sidecar_hash = hashlib.sha256(sidecar_data).hexdigest()
    sidecar_uri = f"gs://sales-discovery-mvp-mumbai/metadata/{doc_id}.meta.json"

    mock_storage = MockStorageClient()
    bucket = mock_storage.bucket("sales-discovery-mvp-mumbai")
    bucket.blob(f"metadata/staging/{doc_id}.meta.json.tmp").upload_from_string(sidecar_data)

    spanner_state = {
        "Documents": [(doc_id, "ENRICHED")],
        "EvidenceChunks": [("chk_1", doc_id, "TEXT", "Content 1", 1, None, None, VALID_VEC)],
        "SolutionCards": [("card_1", f"{sidecar_uri}#sha256={sidecar_hash}")],
        "SolutionImplementsDoc": [("card_1", doc_id)],
        "EvidenceLinks": [("evl_card_1_chk_1", "card_1", "chk_1", doc_id, 1, None, "Quote for card_1")],
    }

    mock_embeddings = MagicMock(spec=VertexEmbeddingsClient)
    mock_extractor = MagicMock(spec=MetadataExtractor)
    mock_writer = MagicMock(spec=EnrichmentWriter)

    orchestrator = EnrichmentOrchestrator(
        spanner_database=MockSpannerDb(spanner_state),
        storage_client=mock_storage,
        embeddings_client=mock_embeddings,
        metadata_extractor=mock_extractor,
        enrichment_writer=mock_writer,
    )

    recovered, reason, rec_result = orchestrator.recover_staging_sidecar(doc_id)
    assert recovered is True
    assert rec_result is not None
    assert rec_result.status == EnrichmentStatus.SUCCESS
    assert rec_result.embeddings_generated_count == 0

    mock_embeddings.embed_texts.assert_not_called()
    mock_extractor.extract_from_document.assert_not_called()
    mock_writer.persist_enrichment.assert_not_called()


def test_recovery_handles_missing_or_corrupt_staging_sidecar():
    """Verify recovery safely fails and never marks document complete if staging is missing or corrupt."""
    doc_id = "doc_recovery_corrupt"
    valid_hash = "f" * 64
    sidecar_uri = f"gs://sales-discovery-mvp-mumbai/metadata/{doc_id}.meta.json"

    mock_storage = MockStorageClient()
    bucket = mock_storage.bucket("sales-discovery-mvp-mumbai")

    spanner_state = {
        "Documents": [(doc_id, "ENRICHED")],
        "EvidenceChunks": [("chk_1", doc_id, "TEXT", "Content 1", 1, None, None, VALID_VEC)],
        "SolutionCards": [("card_1", f"{sidecar_uri}#sha256={valid_hash}")],
        "SolutionImplementsDoc": [("card_1", doc_id)],
        "EvidenceLinks": [("evl_card_1_chk_1", "card_1", "chk_1", doc_id, 1, None, "Quote for card_1")],
    }

    orchestrator = EnrichmentOrchestrator(
        spanner_database=MockSpannerDb(spanner_state),
        storage_client=mock_storage,
    )

    # Subcase A: Staging blob missing
    recovered, reason, rec_result = orchestrator.recover_staging_sidecar(doc_id)
    assert recovered is False
    assert "missing" in reason.lower()
    assert rec_result is None
    passed, details = orchestrator.check_completion_checkpoint(doc_id)
    assert passed is False
    assert details["can_recover_from_staging"] is False
    assert details["all_points_passed"] is False

    # Subcase B: Staging blob corrupt (hash mismatch)
    corrupt_data = b'{"corrupt": true}'
    bucket.blob(f"metadata/staging/{doc_id}.meta.json.tmp").upload_from_string(corrupt_data)

    recovered, reason, rec_result = orchestrator.recover_staging_sidecar(doc_id)
    assert recovered is False
    assert "mismatch" in reason.lower()
    assert rec_result is None
    passed, details = orchestrator.check_completion_checkpoint(doc_id)
    assert passed is False
    assert details["can_recover_from_staging"] is False
    assert details["all_points_passed"] is False


def test_checkpoint_fails_when_evidence_links_missing_for_citation():
    """Verify checkpoint fails when provenance manifest declares citation missing from Spanner EvidenceLinks."""
    doc_id = "doc_missing_evidence"
    cards = ["card_1", "card_2"]
    chunks = ["chk_1", "chk_2"]
    sidecar_data = build_valid_sidecar_json(doc_id, cards, chunks)
    sidecar_hash = hashlib.sha256(sidecar_data).hexdigest()
    sidecar_uri = f"gs://sales-discovery-mvp-mumbai/metadata/{doc_id}.meta.json"

    mock_storage = MockStorageClient()
    bucket = mock_storage.bucket("sales-discovery-mvp-mumbai")
    bucket.blob(f"metadata/{doc_id}.meta.json").upload_from_string(sidecar_data)

    spanner_state = {
        "Documents": [(doc_id, "ENRICHED")],
        "EvidenceChunks": [
            ("chk_1", doc_id, "TEXT", "Content 1", 1, None, None, VALID_VEC),
            ("chk_2", doc_id, "TEXT", "Content 2", 2, None, None, VALID_VEC),
        ],
        "SolutionCards": [
            ("card_1", f"{sidecar_uri}#sha256={sidecar_hash}"),
            ("card_2", f"{sidecar_uri}#sha256={sidecar_hash}"),
        ],
        "SolutionImplementsDoc": [("card_1", doc_id), ("card_2", doc_id)],
        # Only card_1 has EvidenceLink; card_2 is missing!
        "EvidenceLinks": [("evl_card_1_chk_1", "card_1", "chk_1", doc_id, 1, None, "Quote for card_1")],
    }

    orchestrator = EnrichmentOrchestrator(
        spanner_database=MockSpannerDb(spanner_state),
        storage_client=mock_storage,
    )

    passed, details = orchestrator.check_completion_checkpoint(doc_id)
    assert passed is False
    assert details["spanner_evidence_complete"] is False
    assert "card_2" in details["missing_evidence_cards"]
    assert details["all_points_passed"] is False


def test_checkpoint_fails_when_multiple_cards_have_different_sidecar_hashes():
    """Verify checkpoint fails when SolutionCards have divergent sidecar SHA-256 hashes."""
    doc_id = "doc_divergent_hashes"
    hash_1 = "1" * 64
    hash_2 = "2" * 64
    sidecar_uri = f"gs://sales-discovery-mvp-mumbai/metadata/{doc_id}.meta.json"

    mock_storage = MockStorageClient()
    spanner_state = {
        "Documents": [(doc_id, "ENRICHED")],
        "EvidenceChunks": [
            ("chk_1", doc_id, "TEXT", "Content 1", 1, None, None, VALID_VEC),
            ("chk_2", doc_id, "TEXT", "Content 2", 2, None, None, VALID_VEC),
        ],
        "SolutionCards": [
            ("card_1", f"{sidecar_uri}#sha256={hash_1}"),
            ("card_2", f"{sidecar_uri}#sha256={hash_2}"),  # Divergent hash!
        ],
        "SolutionImplementsDoc": [("card_1", doc_id), ("card_2", doc_id)],
        "EvidenceLinks": [
            ("evl_card_1_chk_1", "card_1", "chk_1", doc_id, 1, None, "Quote 1"),
            ("evl_card_2_chk_2", "card_2", "chk_2", doc_id, 2, None, "Quote 2"),
        ],
    }

    orchestrator = EnrichmentOrchestrator(
        spanner_database=MockSpannerDb(spanner_state),
        storage_client=mock_storage,
    )

    passed, details = orchestrator.check_completion_checkpoint(doc_id)
    assert passed is False
    assert details["all_cards_okf_valid"] is False
    assert len(details["mismatched_card_uris"]) > 0
    assert details["all_points_passed"] is False


def test_fully_complete_checkpoint_skips_all_model_calls_and_writes():
    """Verify document meeting all enhanced criteria skips with 0 calls to Vertex AI, Gemini, or Spanner."""
    doc_id = "doc_multi_card_complete"
    cards = ["card_1", "card_2"]
    chunks = ["chk_1", "chk_2"]
    sidecar_data = build_valid_sidecar_json(doc_id, cards, chunks)
    sidecar_hash = hashlib.sha256(sidecar_data).hexdigest()
    sidecar_uri = f"gs://sales-discovery-mvp-mumbai/metadata/{doc_id}.meta.json"

    mock_storage = MockStorageClient()
    bucket = mock_storage.bucket("sales-discovery-mvp-mumbai")
    bucket.blob(f"metadata/{doc_id}.meta.json").upload_from_string(sidecar_data)

    spanner_state = {
        "Documents": [(doc_id, "ENRICHED")],
        "EvidenceChunks": [
            ("chk_1", doc_id, "TEXT", "Content 1", 1, None, None, VALID_VEC),
            ("chk_2", doc_id, "TEXT", "Content 2", 2, None, None, VALID_VEC),
        ],
        "SolutionCards": [
            ("card_1", f"{sidecar_uri}#sha256={sidecar_hash}"),
            ("card_2", f"{sidecar_uri}#sha256={sidecar_hash}"),
        ],
        "SolutionImplementsDoc": [("card_1", doc_id), ("card_2", doc_id)],
        "EvidenceLinks": [
            ("evl_card_1_chk_1", "card_1", "chk_1", doc_id, 1, None, "Quote for card_1"),
            ("evl_card_2_chk_2", "card_2", "chk_2", doc_id, 2, None, "Quote for card_2"),
        ],
    }

    mock_embeddings = MagicMock(spec=VertexEmbeddingsClient)
    mock_extractor = MagicMock(spec=MetadataExtractor)
    mock_writer = MagicMock(spec=EnrichmentWriter)

    orchestrator = EnrichmentOrchestrator(
        spanner_database=MockSpannerDb(spanner_state),
        storage_client=mock_storage,
        embeddings_client=mock_embeddings,
        metadata_extractor=mock_extractor,
        enrichment_writer=mock_writer,
    )

    result = orchestrator.run(doc_id, dry_run=False, force_recompute=False)

    assert result.status == EnrichmentStatus.SKIPPED
    assert result.stage == EnrichmentStage.COMPLETED
    assert result.checkpoint_details["all_points_passed"] is True
    assert result.checkpoint_details["solution_implements_doc_complete"] is True
    assert result.checkpoint_details["all_cards_okf_valid"] is True
    assert result.checkpoint_details["spanner_evidence_complete"] is True
    assert result.checkpoint_details["citation_manifest_verified"] is True

    # 0 model calls and 0 Spanner writes
    mock_embeddings.embed_texts.assert_not_called()
    mock_extractor.extract_from_document.assert_not_called()
    mock_writer.persist_enrichment.assert_not_called()


def test_checkpoint_supports_both_sha256_separator_formats():
    """Verify check_completion_checkpoint succeeds with both #sha256= and legacy #sha256: formats."""
    doc_id = "doc_dual_hash_format"
    cards = ["card_1"]
    chunks = ["chk_1"]
    sidecar_data = build_valid_sidecar_json(doc_id, cards, chunks)
    sidecar_hash = hashlib.sha256(sidecar_data).hexdigest()
    sidecar_uri = f"gs://sales-discovery-mvp-mumbai/metadata/{doc_id}.meta.json"

    # Test format A: #sha256=<hash>
    mock_storage_a = MockStorageClient()
    mock_storage_a.bucket("sales-discovery-mvp-mumbai").blob(f"metadata/{doc_id}.meta.json").upload_from_string(sidecar_data)
    spanner_state_a = {
        "Documents": [(doc_id, "ENRICHED")],
        "EvidenceChunks": [("chk_1", doc_id, "TEXT", "Content 1", 1, None, None, VALID_VEC)],
        "SolutionCards": [("card_1", f"{sidecar_uri}#sha256={sidecar_hash}")],
        "SolutionImplementsDoc": [("card_1", doc_id)],
        "EvidenceLinks": [("evl_card_1_chk_1", "card_1", "chk_1", doc_id, 1, None, "Quote for card_1")],
    }
    orch_a = EnrichmentOrchestrator(spanner_database=MockSpannerDb(spanner_state_a), storage_client=mock_storage_a)
    passed_a, details_a = orch_a.check_completion_checkpoint(doc_id)
    assert passed_a is True
    assert details_a["all_cards_okf_valid"] is True

    # Test format B: legacy #sha256:<hash>
    mock_storage_b = MockStorageClient()
    mock_storage_b.bucket("sales-discovery-mvp-mumbai").blob(f"metadata/{doc_id}.meta.json").upload_from_string(sidecar_data)
    spanner_state_b = {
        "Documents": [(doc_id, "ENRICHED")],
        "EvidenceChunks": [("chk_1", doc_id, "TEXT", "Content 1", 1, None, None, VALID_VEC)],
        "SolutionCards": [("card_1", f"{sidecar_uri}#sha256:{sidecar_hash}")],
        "SolutionImplementsDoc": [("card_1", doc_id)],
        "EvidenceLinks": [("evl_card_1_chk_1", "card_1", "chk_1", doc_id, 1, None, "Quote for card_1")],
    }
    orch_b = EnrichmentOrchestrator(spanner_database=MockSpannerDb(spanner_state_b), storage_client=mock_storage_b)
    passed_b, details_b = orch_b.check_completion_checkpoint(doc_id)
    assert passed_b is True
    assert details_b["all_cards_okf_valid"] is True


