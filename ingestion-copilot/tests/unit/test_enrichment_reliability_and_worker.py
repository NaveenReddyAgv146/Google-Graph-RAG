"""Tests for Task 1: Enrichment Reliability + Automatic Ingestion Integration.

Validates:
1. Successful automatic ingestion and enrichment.
2. Duplicate Pub/Sub delivery after successful completion.
3. Failure before Spanner commit.
4. Failure after Spanner commit but before canonical GCS promotion.
5. Successful staging-sidecar recovery without model calls.
6. Missing staging sidecar when Spanner committed.
7. SHA-256 mismatch detection between Spanner OkfUri and GCS.
8. Canonical sidecar verification failure preserves staging blob.
9. Zero SolutionCards with valid enrichment.
10. Existing APPROVED/REJECTED card protection.
11. Retry after partial failure.
"""

from __future__ import annotations

import base64
import hashlib
import json
from unittest.mock import MagicMock, patch

import pytest

from app.enrichment_orchestrator import (
    EnrichmentOrchestrator,
    EnrichmentOrchestratorResult,
    EnrichmentStage,
    EnrichmentStatus,
)
from app.orchestrator import (
    DocumentIngestionResult,
    IngestionStage,
    IngestionStatus,
    PdfIngestionOrchestrator,
)
from app.pubsub_worker import process_ingestion_event
from common.database.enrichment_writer import (
    EnrichmentPayload,
    EnrichmentWriteResult,
    EnrichmentWriter,
)
from common.embeddings.vertex_client import VertexEmbeddingsClient
from common.models.schema import ChunkNode
from common.parsers.metadata_extractor import (
    ExtractedMetadataResult,
    MetadataExtractor,
    ValidatedEntity,
    ValidatedSolutionCard,
    ValidatedTechnology,
)


# ============================================================================
# MOCK INFRASTRUCTURE FIXTURES
# ============================================================================


class MockBlob:
    """In-memory mock for Google Cloud Storage Blob."""

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
    """In-memory mock for Google Cloud Storage Bucket."""

    def __init__(self, name: str):
        self.name = name
        self.blobs: dict[str, MockBlob] = {}

    def blob(self, name: str) -> MockBlob:
        if name not in self.blobs:
            self.blobs[name] = MockBlob(name, self.name)
        return self.blobs[name]


class MockStorageClient:
    """In-memory mock for Google Cloud Storage Client."""

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

        if "FROM DOCUMENTS" in norm and "COUNT(1) FROM EVIDENCECHUNKS" in norm:
            doc_id = params.get("doc_id")
            for row in self.state.get("Documents", []):
                if row[0] == doc_id:
                    status = row[1]
                    chunk_count = len([c for c in self.state.get("EvidenceChunks", []) if c[1] == doc_id])
                    return [[status, chunk_count]]
            return []

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
            return [(r[0], r[1] if len(r) == 2 else r[8]) for r in rows]

        if "FROM SOLUTIONIMPLEMENTSDOC WHERE DOCUMENTID = @DOC_ID" in norm:
            doc_id = params.get("doc_id")
            rows = [r for r in self.state.get("SolutionImplementsDoc", []) if len(r) < 2 or r[1] == doc_id]
            return [(r[0], r[1] if len(r) > 1 else doc_id) for r in rows]

        if "FROM EVIDENCELINKS WHERE DOCUMENTID = @DOC_ID" in norm:
            doc_id = params.get("doc_id")
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
        self.last_transaction = None

    def snapshot(self, multi_use: bool = True):
        return MockSpannerSnapshot(self.state)

    def run_in_transaction(self, func):
        mock_txn = MagicMock()
        self.last_transaction = mock_txn
        return func(mock_txn)


VALID_VEC = [0.1] * 768


def make_valid_sidecar_bytes(doc_id: str, card_ids: list[str] | None = None) -> bytes:
    cards = card_ids or ["card_1"]
    data = {
        "document_id": doc_id,
        "solution_cards": [
            {
                "card_id": cid,
                "project_name": "Project " + cid,
                "business_problem": "Problem",
                "proposed_solution": "Proposed",
                "implemented_solution": "Implemented",
                "outcome": "Outcome",
                "chunk_id": "chk_1",
                "evidence_quote": "Evidence quote",
                "technologies": ["tech_spanner"],
            }
            for cid in cards
        ],
        "citations": [
            {
                "evidence_id": f"evl_{cid}_chk_1",
                "card_id": cid,
                "chunk_id": "chk_1",
                "evidence_quote": "Evidence quote",
            }
            for cid in cards
        ],
    }
    return json.dumps(data, indent=2, sort_keys=True).encode("utf-8")


# ============================================================================
# PHASE D TEST CASES
# ============================================================================


def test_1_successful_automatic_ingestion_and_enrichment():
    """1. Ingest PDF -> chunks -> automatic enrichment -> sidecar promotion -> SUCCESS."""
    doc_id = "doc_auto_success"
    gcs_uri = "gs://sales-discovery-mvp-mumbai/raw/sample.pdf"

    mock_db = MockSpannerDb({
        "Documents": [],
        "EvidenceChunks": [],
        "SolutionCards": [],
        "EvidenceLinks": [],
    })

    # Mock PDF orchestrator succeeds
    mock_pdf_orch = MagicMock(spec=PdfIngestionOrchestrator)
    mock_pdf_orch.ingest_pdf.return_value = DocumentIngestionResult(
        document_id=doc_id,
        gcs_uri=gcs_uri,
        status=IngestionStatus.SUCCESS,
        stage=IngestionStage.COMPLETED,
        chunks_count=5,
    )

    # Mock Enrichment orchestrator succeeds
    mock_enrich_orch = MagicMock(spec=EnrichmentOrchestrator)
    mock_enrich_orch.run.return_value = EnrichmentOrchestratorResult(
        document_id=doc_id,
        status=EnrichmentStatus.SUCCESS,
        stage=EnrichmentStage.COMPLETED,
        dry_run=False,
        duration_ms=45.0,
        eligible_chunks_count=5,
        excluded_image_chunks_count=0,
        embeddings_generated_count=5,
        entities_count=3,
        technologies_count=2,
        unresolved_technologies_count=0,
        solution_cards_count=1,
        relationships_count=1,
        evidence_links_count=1,
        projected_mutations=15,
        sidecar_uri=f"gs://sales-discovery-mvp-mumbai/metadata/{doc_id}.meta.json",
        sidecar_sha256="abc123hash",
    )

    payload = {
        "event_type": "DOCUMENT_UPLOADED",
        "document_id": doc_id,
        "gcs_uri": gcs_uri,
    }

    result = process_ingestion_event(
        event_payload=payload,
        database=mock_db,
        pdf_orchestrator=mock_pdf_orch,
        enrichment_orchestrator=mock_enrich_orch,
    )

    assert result["status"] == "SUCCESS"
    assert result["stage"] == "COMPLETED"
    assert result["document_id"] == doc_id
    assert result["chunks_count"] == 5
    assert result["enrichment_status"] == "SUCCESS"
    assert result["sidecar_sha256"] == "abc123hash"
    mock_pdf_orch.ingest_pdf.assert_called_once_with(gcs_uri)
    mock_enrich_orch.run.assert_called_once_with(doc_id)


def test_2_duplicate_pubsub_delivery_after_successful_completion():
    """2. Duplicate Pub/Sub delivery after full enrichment returns SKIPPED_DUPLICATE with zero model calls."""
    doc_id = "doc_duplicate_enriched"
    gcs_uri = "gs://sales-discovery-mvp-mumbai/raw/sample.pdf"

    mock_db = MockSpannerDb({
        "Documents": [(doc_id, "ENRICHED")],
        "EvidenceChunks": [("chk_1", doc_id, "TEXT", "Content", 1, None, None, VALID_VEC)],
    })

    mock_enrich_orch = MagicMock(spec=EnrichmentOrchestrator)
    mock_enrich_orch.check_completion_checkpoint.return_value = (True, {"all_points_passed": True})

    mock_pdf_orch = MagicMock(spec=PdfIngestionOrchestrator)

    payload = {
        "event_type": "DOCUMENT_UPLOADED",
        "document_id": doc_id,
        "gcs_uri": gcs_uri,
    }

    result = process_ingestion_event(
        event_payload=payload,
        database=mock_db,
        pdf_orchestrator=mock_pdf_orch,
        enrichment_orchestrator=mock_enrich_orch,
    )

    assert result["status"] == "SKIPPED_DUPLICATE"
    assert result["stage"] == "COMPLETED"
    assert result["document_id"] == doc_id
    mock_pdf_orch.ingest_pdf.assert_not_called()
    mock_enrich_orch.run.assert_not_called()


def test_3_failure_before_spanner_commit():
    """3. Failure during extraction/embedding before Spanner commit reports FAILED and allows retry."""
    doc_id = "doc_fail_before_commit"
    gcs_uri = "gs://sales-discovery-mvp-mumbai/raw/sample.pdf"

    mock_db = MockSpannerDb({
        "Documents": [],
        "EvidenceChunks": [],
    })

    mock_pdf_orch = MagicMock(spec=PdfIngestionOrchestrator)
    mock_pdf_orch.ingest_pdf.return_value = DocumentIngestionResult(
        document_id=doc_id,
        gcs_uri=gcs_uri,
        status=IngestionStatus.SUCCESS,
        stage=IngestionStage.COMPLETED,
        chunks_count=3,
    )

    mock_enrich_orch = MagicMock(spec=EnrichmentOrchestrator)
    mock_enrich_orch.run.return_value = EnrichmentOrchestratorResult(
        document_id=doc_id,
        status=EnrichmentStatus.FAILED,
        stage=EnrichmentStage.EXTRACTION,
        dry_run=False,
        duration_ms=20.0,
        eligible_chunks_count=3,
        excluded_image_chunks_count=0,
        embeddings_generated_count=0,
        entities_count=0,
        technologies_count=0,
        unresolved_technologies_count=0,
        solution_cards_count=0,
        relationships_count=0,
        evidence_links_count=0,
        projected_mutations=0,
        error_message="Gemini rate limit exceeded",
    )

    payload = {
        "event_type": "DOCUMENT_UPLOADED",
        "document_id": doc_id,
        "gcs_uri": gcs_uri,
    }

    result = process_ingestion_event(
        event_payload=payload,
        database=mock_db,
        pdf_orchestrator=mock_pdf_orch,
        enrichment_orchestrator=mock_enrich_orch,
    )

    assert result["status"] == "FAILED"
    assert result["stage"] == "EXTRACTION"
    assert result["error_message"] == "Gemini rate limit exceeded"


def test_4_failure_after_spanner_commit_before_canonical_gcs_promotion():
    """4. Failure after Spanner commit but during canonical promotion retains staging sidecar and fails."""
    doc_id = "doc_promo_failure"
    mock_storage = MockStorageClient()
    bucket = mock_storage.bucket("sales-discovery-mvp-mumbai")

    sidecar_data = make_valid_sidecar_bytes(doc_id, ["card_1"])
    sidecar_hash = hashlib.sha256(sidecar_data).hexdigest()
    sidecar_uri = f"gs://sales-discovery-mvp-mumbai/metadata/{doc_id}.meta.json"

    spanner_state = {
        "Documents": [(doc_id, "PARSED")],
        "EvidenceChunks": [("chk_1", doc_id, "TEXT", "Evidence text", 1, None, None, None)],
        "SolutionCards": [],
        "EvidenceLinks": [],
    }
    mock_db = MockSpannerDb(spanner_state)

    mock_embeddings = MagicMock(spec=VertexEmbeddingsClient)
    mock_embeddings.embed_texts.return_value = [VALID_VEC]

    mock_extractor = MagicMock(spec=MetadataExtractor)
    mock_extractor.extract_from_document.return_value = ExtractedMetadataResult(
        document_id=doc_id,
        entities=[],
        technologies=[],
        solution_cards=[
            ValidatedSolutionCard(
                card_id="card_1",
                document_id=doc_id,
                project_name="Project card_1",
                business_problem="Problem",
                proposed_solution="Proposed",
                implemented_solution="Implemented",
                outcome="Outcome",
                chunk_id="chk_1",
                evidence_quote="Evidence quote",
            )
        ],
        relationships=[],
        unresolved_technologies=[],
        rejected_candidates=[],
    )

    mock_writer = MagicMock(spec=EnrichmentWriter)
    mock_writer.persist_enrichment.return_value = EnrichmentWriteResult(
        document_id=doc_id,
        status="ENRICHED",
        total_mutations=3,
        chunks_embeddings_updated=1,
        chunks_embeddings_unchanged=0,
        entities_inserted=0,
        entities_updated=0,
        entities_unchanged=0,
        technologies_inserted=0,
        technologies_unchanged=0,
        solution_cards_inserted=1,
        solution_cards_updated=0,
        solution_cards_unchanged=0,
        solution_cards_protected=0,
        edges_inserted=1,
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

    # Force promotion verification failure
    def mock_verify(uri):
        if "staging" in uri:
            return orchestrator._fetch_and_verify_gcs_blob(uri)[:2]
        return False, None

    with patch.object(orchestrator, "_verify_gcs_sidecar", side_effect=mock_verify):
        result = orchestrator.run(doc_id, dry_run=False)

    assert result.status == EnrichmentStatus.FAILED
    assert result.stage == EnrichmentStage.PROMOTION
    assert "Canonical sidecar verification failed" in result.error_message

    # Crucial: Staging blob is NOT deleted and is retained for recovery
    staging_blob = bucket.blob(f"metadata/staging/{doc_id}.meta.json.tmp")
    assert staging_blob.exists() is True


def test_5_successful_staging_sidecar_recovery_without_model_calls():
    """5. Interrupted promotion is recovered from retained staging sidecar with 0 model calls."""
    doc_id = "doc_recover_zero_calls"
    sidecar_data = make_valid_sidecar_bytes(doc_id, ["card_1"])
    sidecar_hash = hashlib.sha256(sidecar_data).hexdigest()
    sidecar_uri = f"gs://sales-discovery-mvp-mumbai/metadata/{doc_id}.meta.json"

    mock_storage = MockStorageClient()
    bucket = mock_storage.bucket("sales-discovery-mvp-mumbai")
    bucket.blob(f"metadata/staging/{doc_id}.meta.json.tmp").upload_from_string(sidecar_data)

    mock_db = MockSpannerDb({
        "Documents": [(doc_id, "ENRICHED")],
        "EvidenceChunks": [("chk_1", doc_id, "TEXT", "Evidence text", 1, None, None, VALID_VEC)],
        "SolutionCards": [("card_1", f"{sidecar_uri}#sha256={sidecar_hash}")],
        "SolutionImplementsDoc": [("card_1", doc_id)],
        "EvidenceLinks": [("evl_card_1_chk_1", "card_1", "chk_1", doc_id, 1, None, "Evidence quote")],
    })

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

    result = orchestrator.run(doc_id)

    assert result.status == EnrichmentStatus.SUCCESS
    assert result.stage == EnrichmentStage.COMPLETED
    assert result.sidecar_sha256 == sidecar_hash

    # Canonical blob now exists, staging cleaned up
    canonical_blob = bucket.blob(f"metadata/{doc_id}.meta.json")
    assert canonical_blob.exists() is True
    assert bucket.blob(f"metadata/staging/{doc_id}.meta.json.tmp").exists() is False

    # Zero model calls
    mock_embeddings.embed_texts.assert_not_called()
    mock_extractor.extract_from_document.assert_not_called()
    mock_writer.persist_enrichment.assert_not_called()


def test_6_missing_staging_sidecar_when_spanner_committed():
    """6. When Spanner is committed but staging sidecar is missing, fails closed at CHECKPOINT without model calls."""
    doc_id = "doc_missing_staging"
    sidecar_hash = "f" * 64
    sidecar_uri = f"gs://sales-discovery-mvp-mumbai/metadata/{doc_id}.meta.json"

    mock_storage = MockStorageClient()  # Empty storage: no staging, no canonical
    mock_db = MockSpannerDb({
        "Documents": [(doc_id, "ENRICHED")],
        "EvidenceChunks": [("chk_1", doc_id, "TEXT", "Evidence text", 1, None, None, VALID_VEC)],
        "SolutionCards": [("card_1", f"{sidecar_uri}#sha256={sidecar_hash}")],
        "SolutionImplementsDoc": [("card_1", doc_id)],
        "EvidenceLinks": [("evl_card_1_chk_1", "card_1", "chk_1", doc_id, 1, None, "Evidence quote")],
    })

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

    result = orchestrator.run(doc_id)

    assert result.status == EnrichmentStatus.FAILED
    assert result.stage == EnrichmentStage.CHECKPOINT
    assert "recovery failed" in result.error_message
    mock_embeddings.embed_texts.assert_not_called()
    mock_extractor.extract_from_document.assert_not_called()


def test_7_sha256_mismatch_detected():
    """7. Corrupt sidecar hash mismatch is detected and rejected."""
    doc_id = "doc_hash_mismatch"
    committed_hash = "a" * 64
    sidecar_uri = f"gs://sales-discovery-mvp-mumbai/metadata/{doc_id}.meta.json"

    mock_storage = MockStorageClient()
    bucket = mock_storage.bucket("sales-discovery-mvp-mumbai")
    # Staging blob has different hash
    staging_data = b'{"corrupt": true}'
    bucket.blob(f"metadata/staging/{doc_id}.meta.json.tmp").upload_from_string(staging_data)

    mock_db = MockSpannerDb({
        "Documents": [(doc_id, "ENRICHED")],
        "EvidenceChunks": [("chk_1", doc_id, "TEXT", "Evidence text", 1, None, None, VALID_VEC)],
        "SolutionCards": [("card_1", f"{sidecar_uri}#sha256={committed_hash}")],
        "SolutionImplementsDoc": [("card_1", doc_id)],
        "EvidenceLinks": [("evl_card_1_chk_1", "card_1", "chk_1", doc_id, 1, None, "Evidence quote")],
    })

    orchestrator = EnrichmentOrchestrator(
        spanner_database=mock_db,
        storage_client=mock_storage,
    )

    recovered, reason, rec_result = orchestrator.recover_staging_sidecar(doc_id)
    assert recovered is False
    assert "mismatch" in reason.lower()
    assert rec_result is None

    passed, details = orchestrator.check_completion_checkpoint(doc_id)
    assert passed is False
    assert details["can_recover_from_staging"] is False


def test_8_canonical_sidecar_verification_failure():
    """8. If canonical GCS sidecar fails verification on upload, staging blob is preserved and error raised."""
    doc_id = "doc_canon_verify_fail"
    mock_storage = MockStorageClient()
    mock_db = MockSpannerDb({
        "Documents": [(doc_id, "PARSED")],
        "EvidenceChunks": [("chk_1", doc_id, "TEXT", "Evidence text", 1, None, None, None)],
        "SolutionCards": [],
        "EvidenceLinks": [],
    })

    mock_embeddings = MagicMock(spec=VertexEmbeddingsClient)
    mock_embeddings.embed_texts.return_value = [VALID_VEC]

    mock_extractor = MagicMock(spec=MetadataExtractor)
    mock_extractor.extract_from_document.return_value = build_mock_metadata(doc_id)

    mock_writer = MagicMock(spec=EnrichmentWriter)
    mock_writer.persist_enrichment.return_value = EnrichmentWriteResult(
        document_id=doc_id,
        status="ENRICHED",
        total_mutations=2,
        chunks_embeddings_updated=1,
        chunks_embeddings_unchanged=0,
        entities_inserted=0,
        entities_updated=0,
        entities_unchanged=0,
        technologies_inserted=0,
        technologies_unchanged=0,
        solution_cards_inserted=0,
        solution_cards_updated=0,
        solution_cards_unchanged=0,
        solution_cards_protected=0,
        edges_inserted=0,
        edges_unchanged=0,
        evidence_links_inserted=0,
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

    def mock_verify(uri):
        if "staging" in uri:
            return orchestrator._fetch_and_verify_gcs_blob(uri)[:2]
        return False, None

    with patch.object(orchestrator, "_verify_gcs_sidecar", side_effect=mock_verify):
        result = orchestrator.run(doc_id)

    assert result.status == EnrichmentStatus.FAILED
    assert result.stage == EnrichmentStage.PROMOTION
    assert "Canonical sidecar verification failed" in result.error_message


def test_9_zero_solution_cards_with_valid_enrichment():
    """9. Document producing 0 SolutionCards succeeds, publishes sidecar, and checkpoint passes without failure."""
    doc_id = "doc_zero_cards"
    mock_storage = MockStorageClient()
    bucket = mock_storage.bucket("sales-discovery-mvp-mumbai")

    spanner_state = {
        "Documents": [(doc_id, "PARSED")],
        "EvidenceChunks": [("chk_1", doc_id, "TEXT", "Technical spec only without solution cards.", 1, None, None, None)],
        "SolutionCards": [],
        "EvidenceLinks": [],
    }
    mock_db = MockSpannerDb(spanner_state)

    mock_embeddings = MagicMock(spec=VertexEmbeddingsClient)
    mock_embeddings.embed_texts.return_value = [VALID_VEC]

    mock_extractor = MagicMock(spec=MetadataExtractor)
    # Extracted metadata contains entities and technologies, but ZERO solution cards
    mock_extractor.extract_from_document.return_value = ExtractedMetadataResult(
        document_id=doc_id,
        entities=[
            ValidatedEntity(
                entity_id="ent_kernel",
                name="Linux Kernel",
                category="SUBSYSTEM",
                description="OS Kernel",
                chunk_id="chk_1",
                evidence_quote="Technical spec",
            )
        ],
        technologies=[
            ValidatedTechnology(
                raw_name="Linux",
                chunk_id="chk_1",
                evidence_quote="Technical spec",
                tech_id="tech_linux",
                canonical_name="Linux",
                category="OS",
                is_canonical=True,
                unresolved=False,
            )
        ],
        solution_cards=[],  # ZERO SolutionCards
        relationships=[],
        unresolved_technologies=[],
        rejected_candidates=[],
    )

    mock_writer = MagicMock(spec=EnrichmentWriter)
    mock_writer.persist_enrichment.return_value = EnrichmentWriteResult(
        document_id=doc_id,
        status="ENRICHED",
        total_mutations=4,
        chunks_embeddings_updated=1,
        chunks_embeddings_unchanged=0,
        entities_inserted=1,
        entities_updated=0,
        entities_unchanged=0,
        technologies_inserted=1,
        technologies_unchanged=0,
        solution_cards_inserted=0,
        solution_cards_updated=0,
        solution_cards_unchanged=0,
        solution_cards_protected=0,
        edges_inserted=1,
        edges_unchanged=0,
        evidence_links_inserted=0,
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

    result = orchestrator.run(doc_id)

    assert result.status == EnrichmentStatus.SUCCESS
    assert result.stage == EnrichmentStage.COMPLETED
    assert result.solution_cards_count == 0
    assert result.evidence_links_count == 0
    assert result.entities_count == 1
    assert result.technologies_count == 1

    # Canonical sidecar exists in GCS with 0 solution cards
    canonical_blob = bucket.blob(f"metadata/{doc_id}.meta.json")
    assert canonical_blob.exists() is True
    sidecar_json = json.loads(canonical_blob.content.decode("utf-8"))
    assert sidecar_json["solution_cards"] == []
    assert sidecar_json["citations"] == []

    # Update Spanner state to reflect commit for checkpoint test
    spanner_state["Documents"] = [(doc_id, "ENRICHED")]
    spanner_state["EvidenceChunks"] = [("chk_1", doc_id, "TEXT", "Technical spec", 1, None, None, VALID_VEC)]

    # Checkpoint validation must pass for 0-card document
    passed, details = orchestrator.check_completion_checkpoint(doc_id)
    assert passed is True
    assert details["solution_cards_count"] == 0
    assert details["all_points_passed"] is True


def test_10_approved_and_rejected_card_protection():
    """10. Existing APPROVED and REJECTED cards cannot be overwritten, updated, or reactivated."""
    doc_id = "doc_protection_eval"
    state = {
        "Documents": [(doc_id, "READY")],
        "EvidenceChunks": [("chk_1", "TEXT", "Modernized using Cloud Spanner", 1, None, None, doc_id)],
        "Entities": [],
        "Technologies": [],
        "SolutionCards": [
            (
                "card_appr",
                doc_id,
                "Audited Approved Project",
                "Audited Problem",
                "Audited Proposed",
                "Audited Implemented",
                "Audited Outcome",
                "APPROVED",
                f"gs://bucket/metadata/{doc_id}.json",
            ),
            (
                "card_rej",
                doc_id,
                "Audited Rejected Project",
                "Audited Problem",
                "Audited Proposed",
                "Audited Implemented",
                "Audited Outcome",
                "REJECTED",
                f"gs://bucket/metadata/{doc_id}.json",
            ),
        ],
        "SolutionImplementsDoc": [("card_appr", doc_id), ("card_rej", doc_id)],
        "ChunkReferencesEntity": [],
        "ChunkMentionsTech": [],
        "SolutionSupportsTech": [],
        "EntityReferencesEntity": [],
        "EvidenceLinks": [],
    }

    class MockProtectDb:
        def __init__(self, s):
            self.state = s
            self.last_txn = None

        def run_in_transaction(self, func):
            txn = MagicMock()
            self.last_txn = txn

            def mock_execute_sql(sql, params=None, param_types=None):
                if "FROM Documents" in sql:
                    return self.state["Documents"]
                if "FROM SolutionCards" in sql:
                    return self.state["SolutionCards"]
                if "FROM EvidenceChunks" in sql:
                    return self.state["EvidenceChunks"]
                return []

            txn.execute_sql.side_effect = mock_execute_sql
            return func(txn)

    db = MockProtectDb(state)
    writer = EnrichmentWriter(database=db)

    payload = EnrichmentPayload(
        document_id=doc_id,
        solution_cards=[
            ValidatedSolutionCard(
                card_id="card_appr",
                document_id=doc_id,
                project_name="Malicious Overwrite",
                business_problem="Overwritten Problem",
                proposed_solution="Overwritten Proposed",
                implemented_solution="Overwritten Implemented",
                outcome="Overwritten Outcome",
                chunk_id="chk_1",
                evidence_quote="Modernized using Cloud Spanner",
            ),
            ValidatedSolutionCard(
                card_id="card_rej",
                document_id=doc_id,
                project_name="Reactivation Attempt",
                business_problem="Reactivation Problem",
                proposed_solution="Reactivation Proposed",
                implemented_solution="Reactivation Implemented",
                outcome="Reactivation Outcome",
                chunk_id="chk_1",
                evidence_quote="Modernized using Cloud Spanner",
            ),
        ],
    )

    result = writer.persist_enrichment(payload, force_recompute=True)
    assert result.solution_cards_protected == 2
    assert result.solution_cards_updated == 0
    assert result.solution_cards_inserted == 0

    # Assert no updates were buffered for SolutionCards
    for call in db.last_txn.update.call_args_list:
        assert call[0][0] != "SolutionCards"


def test_11_retry_after_partial_failure():
    """11. Worker successfully completes on retry when initial attempt partially failed before completion."""
    doc_id = "doc_retry_flow"
    gcs_uri = "gs://sales-discovery-mvp-mumbai/raw/sample.pdf"

    mock_db = MockSpannerDb({
        "Documents": [],
        "EvidenceChunks": [],
        "SolutionCards": [],
        "EvidenceLinks": [],
    })

    mock_pdf_orch = MagicMock(spec=PdfIngestionOrchestrator)
    # First attempt: PDF download error
    mock_pdf_orch.ingest_pdf.side_effect = [
        DocumentIngestionResult(
            document_id=doc_id,
            gcs_uri=gcs_uri,
            status=IngestionStatus.FAILED,
            stage=IngestionStage.DOWNLOADING,
            error_message="Transient network glitch",
        ),
        # Second attempt: PDF download succeeds
        DocumentIngestionResult(
            document_id=doc_id,
            gcs_uri=gcs_uri,
            status=IngestionStatus.SUCCESS,
            stage=IngestionStage.COMPLETED,
            chunks_count=4,
        ),
    ]

    mock_enrich_orch = MagicMock(spec=EnrichmentOrchestrator)
    mock_enrich_orch.run.return_value = EnrichmentOrchestratorResult(
        document_id=doc_id,
        status=EnrichmentStatus.SUCCESS,
        stage=EnrichmentStage.COMPLETED,
        dry_run=False,
        duration_ms=30.0,
        eligible_chunks_count=4,
        excluded_image_chunks_count=0,
        embeddings_generated_count=4,
        entities_count=1,
        technologies_count=1,
        unresolved_technologies_count=0,
        solution_cards_count=1,
        relationships_count=0,
        evidence_links_count=1,
        projected_mutations=10,
        sidecar_uri=f"gs://sales-discovery-mvp-mumbai/metadata/{doc_id}.meta.json",
        sidecar_sha256="validsha256",
    )

    payload = {
        "event_type": "DOCUMENT_UPLOADED",
        "document_id": doc_id,
        "gcs_uri": gcs_uri,
    }

    # Attempt 1: Fails
    res1 = process_ingestion_event(
        event_payload=payload,
        database=mock_db,
        pdf_orchestrator=mock_pdf_orch,
        enrichment_orchestrator=mock_enrich_orch,
    )
    assert res1["status"] == "FAILED"
    mock_enrich_orch.run.assert_not_called()

    # Attempt 2: Retry succeeds
    res2 = process_ingestion_event(
        event_payload=payload,
        database=mock_db,
        pdf_orchestrator=mock_pdf_orch,
        enrichment_orchestrator=mock_enrich_orch,
    )
    assert res2["status"] == "SUCCESS"
    assert res2["stage"] == "COMPLETED"
    mock_enrich_orch.run.assert_called_once_with(doc_id)


def test_12_extraction_completed_with_persisted_chunks_invokes_enrichment_directly():
    """12. Document in EXTRACTION_COMPLETED with chunks invokes enrichment directly without PDF re-extraction."""
    doc_id = "doc_ext_completed"
    gcs_uri = "gs://sales-discovery-mvp-mumbai/raw/sample.pdf"

    mock_db = MockSpannerDb({
        "Documents": [(doc_id, "EXTRACTION_COMPLETED")],
        "EvidenceChunks": [("chk_1", doc_id, "TEXT", "Content 1", 1, None, None, None)],
    })

    mock_pdf_orch = MagicMock(spec=PdfIngestionOrchestrator)
    mock_enrich_orch = MagicMock(spec=EnrichmentOrchestrator)
    mock_enrich_orch.run.return_value = EnrichmentOrchestratorResult(
        document_id=doc_id,
        status=EnrichmentStatus.SUCCESS,
        stage=EnrichmentStage.COMPLETED,
        dry_run=False,
        duration_ms=25.0,
        eligible_chunks_count=1,
        excluded_image_chunks_count=0,
        embeddings_generated_count=1,
        entities_count=1,
        technologies_count=1,
        unresolved_technologies_count=0,
        solution_cards_count=1,
        relationships_count=0,
        evidence_links_count=1,
        projected_mutations=10,
        sidecar_uri=f"gs://sales-discovery-mvp-mumbai/metadata/{doc_id}.meta.json",
        sidecar_sha256="ext_hash_123",
    )

    payload = {
        "event_type": "DOCUMENT_UPLOADED",
        "document_id": doc_id,
        "gcs_uri": gcs_uri,
    }

    result = process_ingestion_event(
        event_payload=payload,
        database=mock_db,
        pdf_orchestrator=mock_pdf_orch,
        enrichment_orchestrator=mock_enrich_orch,
    )

    # Document AI / PDF processing must NOT be called
    mock_pdf_orch.ingest_pdf.assert_not_called()
    # Enrichment orchestrator must be called with doc_id
    mock_enrich_orch.run.assert_called_once_with(doc_id)
    assert result["status"] == "SUCCESS"
    assert result["stage"] == "COMPLETED"
    assert result["document_id"] == doc_id
    assert result["chunks_count"] == 1


def test_13_enrichment_failure_after_extraction_resumes_on_redelivery():
    """13. Worker crash/enrichment failure after extraction recovers on redelivery without re-extracting PDF."""
    doc_id = "doc_enrich_retry"
    gcs_uri = "gs://sales-discovery-mvp-mumbai/raw/sample.pdf"

    mock_db = MockSpannerDb({
        "Documents": [(doc_id, "EXTRACTION_COMPLETED")],
        "EvidenceChunks": [("chk_1", doc_id, "TEXT", "Content 1", 1, None, None, None)],
    })

    mock_pdf_orch = MagicMock(spec=PdfIngestionOrchestrator)
    mock_enrich_orch = MagicMock(spec=EnrichmentOrchestrator)

    # Attempt 1: Enrichment fails (e.g. rate limit or network glitch)
    mock_enrich_orch.run.side_effect = [
        EnrichmentOrchestratorResult(
            document_id=doc_id,
            status=EnrichmentStatus.FAILED,
            stage=EnrichmentStage.EXTRACTION,
            dry_run=False,
            duration_ms=10.0,
            eligible_chunks_count=1,
            excluded_image_chunks_count=0,
            embeddings_generated_count=0,
            entities_count=0,
            technologies_count=0,
            unresolved_technologies_count=0,
            solution_cards_count=0,
            relationships_count=0,
            evidence_links_count=0,
            projected_mutations=0,
            error_message="Vertex AI rate limit exceeded",
        ),
        # Attempt 2 (redelivery): Enrichment succeeds
        EnrichmentOrchestratorResult(
            document_id=doc_id,
            status=EnrichmentStatus.SUCCESS,
            stage=EnrichmentStage.COMPLETED,
            dry_run=False,
            duration_ms=30.0,
            eligible_chunks_count=1,
            excluded_image_chunks_count=0,
            embeddings_generated_count=1,
            entities_count=1,
            technologies_count=1,
            unresolved_technologies_count=0,
            solution_cards_count=1,
            relationships_count=0,
            evidence_links_count=1,
            projected_mutations=10,
            sidecar_uri=f"gs://sales-discovery-mvp-mumbai/metadata/{doc_id}.meta.json",
            sidecar_sha256="retry_hash",
        ),
    ]

    payload = {
        "event_type": "DOCUMENT_UPLOADED",
        "document_id": doc_id,
        "gcs_uri": gcs_uri,
    }

    # First delivery attempt
    res1 = process_ingestion_event(
        event_payload=payload,
        database=mock_db,
        pdf_orchestrator=mock_pdf_orch,
        enrichment_orchestrator=mock_enrich_orch,
    )
    assert res1["status"] == "FAILED"
    assert res1["error_message"] == "Vertex AI rate limit exceeded"
    mock_pdf_orch.ingest_pdf.assert_not_called()

    # Redelivery attempt after retry
    res2 = process_ingestion_event(
        event_payload=payload,
        database=mock_db,
        pdf_orchestrator=mock_pdf_orch,
        enrichment_orchestrator=mock_enrich_orch,
    )
    assert res2["status"] == "SUCCESS"
    assert res2["stage"] == "COMPLETED"
    assert res2["sidecar_sha256"] == "retry_hash"
    # PDF processing / Document AI was NEVER called across either attempt
    mock_pdf_orch.ingest_pdf.assert_not_called()
    assert mock_enrich_orch.run.call_count == 2


def test_14_enriched_invalid_checkpoint_triggers_recovery():
    """14. Document marked ENRICHED whose checkpoint fails is not skipped; recovery is invoked."""
    doc_id = "doc_enriched_invalid_cp"
    gcs_uri = "gs://sales-discovery-mvp-mumbai/raw/sample.pdf"

    mock_db = MockSpannerDb({
        "Documents": [(doc_id, "ENRICHED")],
        "EvidenceChunks": [("chk_1", doc_id, "TEXT", "Content 1", 1, None, None, VALID_VEC)],
    })

    mock_pdf_orch = MagicMock(spec=PdfIngestionOrchestrator)
    mock_enrich_orch = MagicMock(spec=EnrichmentOrchestrator)
    # Checkpoint fails (e.g. sidecar missing in GCS)
    mock_enrich_orch.check_completion_checkpoint.return_value = (False, {"all_points_passed": False, "reason": "missing_sidecar"})
    # Recovery run succeeds
    mock_enrich_orch.run.return_value = EnrichmentOrchestratorResult(
        document_id=doc_id,
        status=EnrichmentStatus.SUCCESS,
        stage=EnrichmentStage.COMPLETED,
        dry_run=False,
        duration_ms=18.0,
        eligible_chunks_count=1,
        excluded_image_chunks_count=0,
        embeddings_generated_count=0,
        entities_count=1,
        technologies_count=1,
        unresolved_technologies_count=0,
        solution_cards_count=1,
        relationships_count=0,
        evidence_links_count=1,
        projected_mutations=0,
        sidecar_uri=f"gs://sales-discovery-mvp-mumbai/metadata/{doc_id}.meta.json",
        sidecar_sha256="rec_hash_val",
    )

    payload = {
        "event_type": "DOCUMENT_UPLOADED",
        "document_id": doc_id,
        "gcs_uri": gcs_uri,
    }

    result = process_ingestion_event(
        event_payload=payload,
        database=mock_db,
        pdf_orchestrator=mock_pdf_orch,
        enrichment_orchestrator=mock_enrich_orch,
    )

    assert result["status"] == "SUCCESS"
    assert result["stage"] == "COMPLETED"
    mock_pdf_orch.ingest_pdf.assert_not_called()
    mock_enrich_orch.run.assert_called_once_with(doc_id)


def build_mock_metadata(doc_id: str) -> ExtractedMetadataResult:
    return ExtractedMetadataResult(
        document_id=doc_id,
        entities=[],
        technologies=[],
        solution_cards=[],
        relationships=[],
        unresolved_technologies=[],
        rejected_candidates=[],
    )
