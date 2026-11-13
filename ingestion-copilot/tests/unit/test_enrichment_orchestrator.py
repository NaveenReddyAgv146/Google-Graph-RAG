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
            return self.state.get("SolutionCards", [])

        if "SELECT COUNT(1) FROM EVIDENCELINKS WHERE DOCUMENTID = @DOC_ID" in norm:
            return [[len(self.state.get("EvidenceLinks", []))]]

        return []


class MockSpannerDb:
    """Mock Spanner database instance."""

    def __init__(self, state: dict[str, list[tuple]]):
        self.state = state

    def snapshot(self, multi_use: bool = True):
        return MockSpannerSnapshot(self.state)


VALID_VEC = [0.1] * 768


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
    sidecar_data = b'{"document_id": "doc_already_done"}'
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
            ("card_1", f"{sidecar_uri}#sha256:{sidecar_hash}")
        ],
        "EvidenceLinks": [("evl_1",)],
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
    sidecar_data = b'{"document_id": "doc_force_recompute"}'
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
            ("card_1", f"{sidecar_uri}#sha256:{sidecar_hash}")
        ],
        "EvidenceLinks": [("evl_1",)],
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
    sidecar_uri = f"gs://sales-discovery-mvp-mumbai/metadata/{doc_id}.meta.json#sha256:dummyhash"

    mock_storage = MockStorageClient()  # Empty storage: blob does not exist
    spanner_state = {
        "Documents": [(doc_id, "ENRICHED")],
        "EvidenceChunks": [
            ("chk_1", doc_id, "TEXT", "Content", 1, None, None, VALID_VEC),
        ],
        "SolutionCards": [("card_1", sidecar_uri)],
        "EvidenceLinks": [("evl_1",)],
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

