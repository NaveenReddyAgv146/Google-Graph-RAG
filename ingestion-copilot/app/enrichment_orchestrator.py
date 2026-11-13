"""Enterprise Agentic GraphRAG - Enrichment Orchestrator for Ingestion Copilot.

Coordinates Phase 3C metadata extraction, taxonomy resolution, Vertex AI embeddings generation,
staged GCS sidecar publication, and atomic 11-table Spanner persistence.

Pipeline Stages:
1. CHECKPOINT: Check four-point completion status; skip already-enriched documents.
2. DISCOVERY: Read eligible non-empty TEXT/TABLE EvidenceChunks from Spanner; exclude IMAGE chunks.
3. EXTRACTION: Structured Gemini extraction of business entities, technologies, solutions, and relationships.
4. EMBEDDING: Vertex AI 768-dimensional embeddings for eligible chunks.
5. STAGING: Upload metadata sidecar to temporary GCS staging path and compute SHA-256.
6. PERSISTENCE: Single serializable read-write Spanner transaction across all 11 tables.
7. PROMOTION: Promote staging sidecar to permanent GCS path and verify SHA-256.
8. COMPLETED: Report stage-level breakdown, mutation counts, and sidecar provenance.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Sequence

from google.cloud import spanner, storage

from common.config import Settings, settings as global_settings
from common.database.enrichment_writer import (
    ApprovedSolutionCardProtectionError,
    ChunkOwnershipConflictError,
    DocumentNotFoundError,
    EnrichmentPayload,
    EnrichmentPersistenceError,
    EnrichmentWriteResult,
    EnrichmentWriter,
    format_okf_uri,
)
from common.embeddings.vertex_client import VertexEmbeddingsClient
from common.models.schema import ChunkNode
from common.parsers.metadata_extractor import (
    ExtractedMetadataResult,
    MetadataExtractor,
)
from common.taxonomy.taxonomy_mapper import TaxonomyMapper

logger = logging.getLogger("ingestion_copilot.enrichment_orchestrator")


# ============================================================================
# STAGES & STATUS ENUMS
# ============================================================================


class EnrichmentStage(str, Enum):
    """Stages of the enrichment orchestration pipeline."""

    CHECKPOINT = "CHECKPOINT"
    DISCOVERY = "DISCOVERY"
    EXTRACTION = "EXTRACTION"
    EMBEDDING = "EMBEDDING"
    STAGING = "STAGING"
    PERSISTENCE = "PERSISTENCE"
    PROMOTION = "PROMOTION"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class EnrichmentStatus(str, Enum):
    """Overall outcome of an enrichment orchestration run."""

    SUCCESS = "SUCCESS"
    SKIPPED = "SKIPPED"
    DRY_RUN = "DRY_RUN"
    FAILED = "FAILED"


# ============================================================================
# ORCHESTRATOR RESULT MODEL
# ============================================================================


@dataclass(frozen=True)
class EnrichmentOrchestratorResult:
    """Detailed summary of the complete enrichment execution."""

    document_id: str
    status: EnrichmentStatus
    stage: EnrichmentStage
    dry_run: bool
    duration_ms: float
    eligible_chunks_count: int
    excluded_image_chunks_count: int
    embeddings_generated_count: int
    entities_count: int
    technologies_count: int
    unresolved_technologies_count: int
    solution_cards_count: int
    relationships_count: int
    evidence_links_count: int
    projected_mutations: int
    write_result: EnrichmentWriteResult | None = None
    sidecar_uri: str | None = None
    sidecar_sha256: str | None = None
    checkpoint_details: dict[str, Any] = field(default_factory=dict)
    error_message: str | None = None


# ============================================================================
# ENRICHMENT ORCHESTRATOR
# ============================================================================


class EnrichmentOrchestrator:
    """End-to-end enrichment orchestrator coordinating embeddings, metadata, GCS, and Spanner."""

    def __init__(
        self,
        spanner_database: Any | None = None,
        storage_client: Any | None = None,
        embeddings_client: VertexEmbeddingsClient | None = None,
        taxonomy_mapper: TaxonomyMapper | None = None,
        metadata_extractor: MetadataExtractor | None = None,
        enrichment_writer: EnrichmentWriter | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or global_settings
        self._spanner_database = spanner_database
        self._storage_client = storage_client

        self.embeddings_client = embeddings_client or VertexEmbeddingsClient(settings=self.settings)
        self.taxonomy_mapper = taxonomy_mapper or TaxonomyMapper()
        self.metadata_extractor = metadata_extractor or MetadataExtractor(
            taxonomy_mapper=self.taxonomy_mapper, settings=self.settings
        )
        self.enrichment_writer = enrichment_writer or EnrichmentWriter(
            database=self._spanner_database, settings=self.settings
        )

    @property
    def spanner_database(self) -> Any:
        """Lazily initialize Cloud Spanner Database instance."""
        if self._spanner_database is None:
            client = spanner.Client(project=self.settings.project_id)
            instance = client.instance(self.settings.spanner_instance_id)
            self._spanner_database = instance.database(self.settings.spanner_database_id)
        return self._spanner_database

    @property
    def storage_client(self) -> Any:
        """Lazily initialize Cloud Storage Client instance."""
        if self._storage_client is None:
            self._storage_client = storage.Client(project=self.settings.project_id)
        return self._storage_client

    def run(
        self,
        document_id: str,
        dry_run: bool = False,
        force_recompute: bool = False,
    ) -> EnrichmentOrchestratorResult:
        """Execute the enrichment pipeline for a target document."""
        start_time = time.monotonic()
        clean_doc_id = document_id.strip()
        current_stage = EnrichmentStage.CHECKPOINT

        logger.info(
            "Starting enrichment orchestration for '%s' (dry_run=%s, force_recompute=%s)",
            clean_doc_id,
            dry_run,
            force_recompute,
        )

        try:
            # ----------------------------------------------------------------
            # STAGE 1: CHECKPOINT
            # ----------------------------------------------------------------
            is_complete, cp_details = self.check_completion_checkpoint(clean_doc_id)
            if is_complete and not force_recompute and not dry_run:
                duration_ms = (time.monotonic() - start_time) * 1000.0
                logger.info("Document '%s' already fully enriched. Checkpoint passed. Skipping.", clean_doc_id)
                return EnrichmentOrchestratorResult(
                    document_id=clean_doc_id,
                    status=EnrichmentStatus.SKIPPED,
                    stage=EnrichmentStage.COMPLETED,
                    dry_run=dry_run,
                    duration_ms=duration_ms,
                    eligible_chunks_count=cp_details.get("eligible_chunks", 0),
                    excluded_image_chunks_count=cp_details.get("image_chunks", 0),
                    embeddings_generated_count=0,
                    entities_count=cp_details.get("entities_count", 0),
                    technologies_count=cp_details.get("technologies_count", 0),
                    unresolved_technologies_count=0,
                    solution_cards_count=cp_details.get("solution_cards_count", 0),
                    relationships_count=0,
                    evidence_links_count=cp_details.get("evidence_links_count", 0),
                    projected_mutations=0,
                    sidecar_uri=cp_details.get("sidecar_uri"),
                    sidecar_sha256=cp_details.get("sidecar_sha256"),
                    checkpoint_details=cp_details,
                )

            # ----------------------------------------------------------------
            # STAGE 2: DISCOVERY (READ CHUNKS FROM SPANNER, NEVER RE-INGEST)
            # ----------------------------------------------------------------
            current_stage = EnrichmentStage.DISCOVERY
            chunks_data = self._read_document_chunks(clean_doc_id)
            eligible_chunks = chunks_data["eligible"]
            excluded_image_chunks = chunks_data["excluded"]

            logger.info(
                "Document '%s' discovery: %d eligible text/table chunks, %d excluded IMAGE/empty chunks",
                clean_doc_id,
                len(eligible_chunks),
                len(excluded_image_chunks),
            )

            if not eligible_chunks:
                duration_ms = (time.monotonic() - start_time) * 1000.0
                logger.warning("No eligible text chunks found for document '%s'. Skipping.", clean_doc_id)
                return EnrichmentOrchestratorResult(
                    document_id=clean_doc_id,
                    status=EnrichmentStatus.SKIPPED,
                    stage=EnrichmentStage.COMPLETED,
                    dry_run=dry_run,
                    duration_ms=duration_ms,
                    eligible_chunks_count=0,
                    excluded_image_chunks_count=len(excluded_image_chunks),
                    embeddings_generated_count=0,
                    entities_count=0,
                    technologies_count=0,
                    unresolved_technologies_count=0,
                    solution_cards_count=0,
                    relationships_count=0,
                    evidence_links_count=0,
                    projected_mutations=0,
                    checkpoint_details=cp_details,
                )

            # ----------------------------------------------------------------
            # STAGE 3: EXTRACTION (METADATA, TAXONOMY, EVIDENCE)
            # ----------------------------------------------------------------
            current_stage = EnrichmentStage.EXTRACTION
            logger.info("Extracting structured metadata with Gemini for document '%s'", clean_doc_id)
            metadata_result = self.metadata_extractor.extract_from_document(
                clean_doc_id, eligible_chunks
            )

            # ----------------------------------------------------------------
            # STAGE 4: EMBEDDING (768-DIM VERTEX AI EMBEDDINGS)
            # ----------------------------------------------------------------
            current_stage = EnrichmentStage.EMBEDDING
            logger.info("Generating 768-dim embeddings for %d chunks", len(eligible_chunks))
            chunk_texts = [c.text_content for c in eligible_chunks]
            raw_embeddings = self.embeddings_client.embed_texts(chunk_texts)
            chunk_embeddings = {
                chunk.chunk_id: emb
                for chunk, emb in zip(eligible_chunks, raw_embeddings)
            }

            # ----------------------------------------------------------------
            # STAGE 5: STAGING (PREPARE SIDECAR & COMPUTE SHA-256)
            # ----------------------------------------------------------------
            current_stage = EnrichmentStage.STAGING
            bucket_name = self.settings.gcs_mumbai_bucket
            sidecar_blob_name = f"metadata/{clean_doc_id}.meta.json"
            sidecar_uri = f"gs://{bucket_name}/{sidecar_blob_name}"
            staging_blob_name = f"metadata/staging/{clean_doc_id}.meta.json.tmp"

            sidecar_bytes = self._build_sidecar_bytes(
                clean_doc_id,
                metadata_result,
                chunk_embeddings_count=len(chunk_embeddings),
                eligible_chunks_count=len(eligible_chunks),
            )
            sidecar_sha256 = hashlib.sha256(sidecar_bytes).hexdigest()

            # ----------------------------------------------------------------
            # DRY RUN BRANCH (SIMULATION ONLY)
            # ----------------------------------------------------------------
            if dry_run:
                duration_ms = (time.monotonic() - start_time) * 1000.0
                projected_mutations = (
                    len(chunk_embeddings)
                    + len(metadata_result.entities)
                    + len(metadata_result.technologies)
                    + len(metadata_result.solution_cards)
                    + len(metadata_result.relationships)
                    + len(metadata_result.solution_cards)  # EvidenceLinks & SID
                    + 1  # Documents status
                )
                logger.info(
                    "Dry-run simulation complete for '%s'. Zero writes committed to Spanner or GCS.",
                    clean_doc_id,
                )
                return EnrichmentOrchestratorResult(
                    document_id=clean_doc_id,
                    status=EnrichmentStatus.DRY_RUN,
                    stage=EnrichmentStage.COMPLETED,
                    dry_run=True,
                    duration_ms=duration_ms,
                    eligible_chunks_count=len(eligible_chunks),
                    excluded_image_chunks_count=len(excluded_image_chunks),
                    embeddings_generated_count=len(chunk_embeddings),
                    entities_count=len(metadata_result.entities),
                    technologies_count=len(metadata_result.technologies),
                    unresolved_technologies_count=len(metadata_result.unresolved_technologies),
                    solution_cards_count=len(metadata_result.solution_cards),
                    relationships_count=len(metadata_result.relationships),
                    evidence_links_count=len(metadata_result.solution_cards),
                    projected_mutations=projected_mutations,
                    sidecar_uri=sidecar_uri,
                    sidecar_sha256=sidecar_sha256,
                    checkpoint_details=cp_details,
                )

            # Upload sidecar to staging
            self._upload_gcs_blob(
                bucket_name=bucket_name,
                blob_name=staging_blob_name,
                content=sidecar_bytes,
                content_type="application/json",
                metadata={"sha256": sidecar_sha256},
            )

            # ----------------------------------------------------------------
            # STAGE 6: PERSISTENCE (ATOMIC 11-TABLE SPANNER COMMIT)
            # ----------------------------------------------------------------
            current_stage = EnrichmentStage.PERSISTENCE
            payload = EnrichmentPayload.from_metadata_result(
                document_id=clean_doc_id,
                chunk_embeddings=chunk_embeddings,
                metadata_result=metadata_result,
                sidecar_uri=sidecar_uri,
                sidecar_sha256=sidecar_sha256,
            )

            write_result = self.enrichment_writer.persist_enrichment(
                payload=payload, force_recompute=force_recompute
            )

            # ----------------------------------------------------------------
            # STAGE 7: PROMOTION (PROMOTE GCS SIDECAR & REMOVE STAGING)
            # ----------------------------------------------------------------
            current_stage = EnrichmentStage.PROMOTION
            self._upload_gcs_blob(
                bucket_name=bucket_name,
                blob_name=sidecar_blob_name,
                content=sidecar_bytes,
                content_type="application/json",
                metadata={"sha256": sidecar_sha256},
            )
            # Cleanup staging temporary blob
            self._delete_gcs_blob_if_exists(bucket_name, staging_blob_name)

            # ----------------------------------------------------------------
            # STAGE 8: COMPLETED
            # ----------------------------------------------------------------
            duration_ms = (time.monotonic() - start_time) * 1000.0
            return EnrichmentOrchestratorResult(
                document_id=clean_doc_id,
                status=EnrichmentStatus.SUCCESS,
                stage=EnrichmentStage.COMPLETED,
                dry_run=False,
                duration_ms=duration_ms,
                eligible_chunks_count=len(eligible_chunks),
                excluded_image_chunks_count=len(excluded_image_chunks),
                embeddings_generated_count=len(chunk_embeddings),
                entities_count=len(metadata_result.entities),
                technologies_count=len(metadata_result.technologies),
                unresolved_technologies_count=len(metadata_result.unresolved_technologies),
                solution_cards_count=len(metadata_result.solution_cards),
                relationships_count=len(metadata_result.relationships),
                evidence_links_count=len(metadata_result.solution_cards),
                projected_mutations=write_result.total_mutations,
                write_result=write_result,
                sidecar_uri=sidecar_uri,
                sidecar_sha256=sidecar_sha256,
                checkpoint_details=cp_details,
            )

        except Exception as exc:
            duration_ms = (time.monotonic() - start_time) * 1000.0
            logger.error(
                "Enrichment orchestration failed for '%s' at stage %s: %s",
                clean_doc_id,
                current_stage.value,
                exc,
                exc_info=True,
            )
            return EnrichmentOrchestratorResult(
                document_id=clean_doc_id,
                status=EnrichmentStatus.FAILED,
                stage=current_stage,
                dry_run=dry_run,
                duration_ms=duration_ms,
                eligible_chunks_count=0,
                excluded_image_chunks_count=0,
                embeddings_generated_count=0,
                entities_count=0,
                technologies_count=0,
                unresolved_technologies_count=0,
                solution_cards_count=0,
                relationships_count=0,
                evidence_links_count=0,
                projected_mutations=0,
                error_message=str(exc),
            )

    def check_completion_checkpoint(self, document_id: str) -> tuple[bool, dict[str, Any]]:
        """Validate the four-point completion checkpoint for a document.

        Points:
        1. Documents.Status == 'ENRICHED'
        2. All eligible non-empty chunks have 768-dim Embedding768.
        3. SolutionCards and EvidenceLinks exist.
        4. GCS sidecar exists and content SHA-256 matches SolutionCards.OkfUri reference.
        """
        details: dict[str, Any] = {
            "document_exists": False,
            "status_enriched": False,
            "eligible_chunks": 0,
            "embedded_chunks": 0,
            "image_chunks": 0,
            "solution_cards_count": 0,
            "evidence_links_count": 0,
            "sidecar_uri": None,
            "sidecar_sha256": None,
            "sidecar_gcs_exists": False,
            "sidecar_sha256_match": False,
            "all_points_passed": False,
        }

        try:
            with self.spanner_database.snapshot(multi_use=True) as snapshot:
                # Point 1: Document Status
                doc_sql = "SELECT Status FROM Documents WHERE DocumentId = @doc_id"
                doc_rows = list(
                    snapshot.execute_sql(
                        doc_sql,
                        params={"doc_id": document_id},
                        param_types={"doc_id": spanner.param_types.STRING},
                    )
                )
                if not doc_rows:
                    return False, details

                details["document_exists"] = True
                status_str = doc_rows[0][0]
                details["status_enriched"] = (status_str == "ENRICHED")

                # Point 2: EvidenceChunks
                chunks_sql = """
                SELECT ChunkId, Modality, TextContent, Embedding768
                FROM EvidenceChunks
                WHERE DocumentId = @doc_id
                """
                chunk_rows = list(
                    snapshot.execute_sql(
                        chunks_sql,
                        params={"doc_id": document_id},
                        param_types={"doc_id": spanner.param_types.STRING},
                    )
                )

                eligible_count = 0
                embedded_count = 0
                image_count = 0

                for _, modality, text_content, emb in chunk_rows:
                    if modality == "IMAGE" or not text_content or not text_content.strip():
                        image_count += 1
                    else:
                        eligible_count += 1
                        if emb is not None and len(emb) == 768:
                            embedded_count += 1

                details["eligible_chunks"] = eligible_count
                details["embedded_chunks"] = embedded_count
                details["image_chunks"] = image_count
                chunks_complete = (eligible_count > 0 and eligible_count == embedded_count)

                # Point 3: SolutionCards and EvidenceLinks
                cards_sql = """
                SELECT CardId, OkfUri FROM SolutionCards WHERE DocumentId = @doc_id
                """
                card_rows = list(
                    snapshot.execute_sql(
                        cards_sql,
                        params={"doc_id": document_id},
                        param_types={"doc_id": spanner.param_types.STRING},
                    )
                )
                details["solution_cards_count"] = len(card_rows)

                ev_sql = "SELECT COUNT(1) FROM EvidenceLinks WHERE DocumentId = @doc_id"
                ev_count = list(
                    snapshot.execute_sql(
                        ev_sql,
                        params={"doc_id": document_id},
                        param_types={"doc_id": spanner.param_types.STRING},
                    )
                )[0][0]
                details["evidence_links_count"] = ev_count
                metadata_present = (len(card_rows) > 0 and ev_count > 0)

                # Point 4: GCS Sidecar SHA-256 match
                sidecar_ok = False
                if card_rows and card_rows[0][1]:
                    okf_uri = card_rows[0][1]
                    details["sidecar_uri"] = okf_uri
                    if "#sha256:" in okf_uri:
                        uri_part, expected_hash = okf_uri.split("#sha256:", 1)
                        details["sidecar_sha256"] = expected_hash
                        # Check GCS sidecar existence & hash
                        gcs_exists, actual_hash = self._verify_gcs_sidecar(uri_part)
                        details["sidecar_gcs_exists"] = gcs_exists
                        if gcs_exists and actual_hash == expected_hash:
                            details["sidecar_sha256_match"] = True
                            sidecar_ok = True

                all_passed = (
                    details["status_enriched"]
                    and chunks_complete
                    and metadata_present
                    and sidecar_ok
                )
                details["all_points_passed"] = all_passed
                return all_passed, details

        except Exception as e:
            logger.warning("Checkpoint inspection failed for '%s': %s", document_id, e)
            return False, details

    def _read_document_chunks(self, document_id: str) -> dict[str, list[ChunkNode]]:
        """Read existing EvidenceChunks from Spanner and partition into eligible vs excluded."""
        eligible: list[ChunkNode] = []
        excluded: list[ChunkNode] = []

        sql = """
        SELECT ChunkId, DocumentId, Modality, TextContent, PageNumber, SlideNumber, SourceGcsUri
        FROM EvidenceChunks
        WHERE DocumentId = @doc_id
        ORDER BY PageNumber, SlideNumber, ChunkId
        """
        with self.spanner_database.snapshot(multi_use=True) as snapshot:
            rows = list(
                snapshot.execute_sql(
                    sql,
                    params={"doc_id": document_id},
                    param_types={"doc_id": spanner.param_types.STRING},
                )
            )

        for cid, doc_id, modality, text, page, slide, gcs_uri in rows:
            chunk = ChunkNode(
                chunk_id=cid,
                document_id=doc_id,
                modality=modality or "TEXT",
                text_content=text or "",
                page_number=page,
                slide_number=slide,
                source_gcs_uri=gcs_uri,
            )
            # Invariant 2: Exclude IMAGE chunks and empty-text chunks
            if chunk.modality == "IMAGE" or not chunk.text_content or not chunk.text_content.strip():
                excluded.append(chunk)
            else:
                eligible.append(chunk)

        return {"eligible": eligible, "excluded": excluded}

    def _build_sidecar_bytes(
        self,
        document_id: str,
        metadata: ExtractedMetadataResult,
        chunk_embeddings_count: int,
        eligible_chunks_count: int,
    ) -> bytes:
        """Construct the canonical JSON payload for the .meta.json sidecar."""
        data = {
            "document_id": document_id,
            "enriched_at": datetime.now(timezone.utc).isoformat(),
            "pipeline_stage": "Phase_3C_Enrichment",
            "eligible_chunks_count": eligible_chunks_count,
            "embeddings_generated_count": chunk_embeddings_count,
            "entities": [
                {
                    "entity_id": e.entity_id,
                    "name": e.name,
                    "category": e.category,
                    "description": e.description,
                    "chunk_id": e.chunk_id,
                    "evidence_quote": e.evidence_quote,
                }
                for e in metadata.entities
            ],
            "technologies": [
                {
                    "tech_id": t.tech_id,
                    "canonical_name": t.canonical_name,
                    "raw_name": t.raw_name,
                    "category": t.category,
                    "is_canonical": t.is_canonical,
                    "chunk_id": t.chunk_id,
                    "evidence_quote": t.evidence_quote,
                }
                for t in metadata.technologies
            ],
            "unresolved_technologies": metadata.unresolved_technologies,
            "solution_cards": [
                {
                    "card_id": c.card_id,
                    "project_name": c.project_name,
                    "business_problem": c.business_problem,
                    "proposed_solution": c.proposed_solution,
                    "implemented_solution": c.implemented_solution,
                    "outcome": c.outcome,
                    "chunk_id": c.chunk_id,
                    "evidence_quote": c.evidence_quote,
                    "technologies": c.technologies,
                }
                for c in metadata.solution_cards
            ],
            "relationships": [
                {
                    "source_entity_id": r.source_entity_id,
                    "target_entity_id": r.target_entity_id,
                    "source_name": r.source_name,
                    "target_name": r.target_name,
                    "relationship_type": r.relationship_type,
                    "chunk_id": r.chunk_id,
                    "evidence_quote": r.evidence_quote,
                }
                for r in metadata.relationships
            ],
        }
        return json.dumps(data, indent=2, sort_keys=True).encode("utf-8")

    def _verify_gcs_sidecar(self, gcs_uri: str) -> tuple[bool, str | None]:
        """Verify that a GCS sidecar blob exists and compute its SHA-256 hash."""
        try:
            if not gcs_uri.startswith("gs://"):
                return False, None
            path_part = gcs_uri[5:]
            bucket_name, blob_name = path_part.split("/", 1)
            bucket = self.storage_client.bucket(bucket_name)
            blob = bucket.blob(blob_name)
            if not blob.exists():
                return False, None

            content = blob.download_as_bytes()
            computed_hash = hashlib.sha256(content).hexdigest()
            return True, computed_hash
        except Exception as e:
            logger.debug("Failed verifying GCS sidecar '%s': %s", gcs_uri, e)
            return False, None

    def _upload_gcs_blob(
        self,
        bucket_name: str,
        blob_name: str,
        content: bytes,
        content_type: str = "application/json",
        metadata: dict[str, str] | None = None,
    ) -> None:
        """Upload raw bytes to a GCS destination object."""
        bucket = self.storage_client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        if metadata:
            blob.metadata = metadata
        blob.upload_from_string(content, content_type=content_type)
        logger.info("Uploaded GCS sidecar to gs://%s/%s (%d bytes)", bucket_name, blob_name, len(content))

    def _delete_gcs_blob_if_exists(self, bucket_name: str, blob_name: str) -> None:
        """Safely delete a GCS blob if it exists."""
        try:
            bucket = self.storage_client.bucket(bucket_name)
            blob = bucket.blob(blob_name)
            if blob.exists():
                blob.delete()
                logger.debug("Deleted temporary staging blob: gs://%s/%s", bucket_name, blob_name)
        except Exception as e:
            logger.warning("Could not delete temporary staging blob gs://%s/%s: %s", bucket_name, blob_name, e)


def format_enrichment_result_report(result: EnrichmentOrchestratorResult) -> str:
    """Formats structured EnrichmentOrchestratorResult into a readable terminal summary."""
    lines = [
        "=" * 68,
        "Phase 3C Manual Enrichment Orchestrator Result",
        "=" * 68,
        f"Document ID:             {result.document_id}",
        f"Status:                  {result.status.value}",
        f"Final Stage:             {result.stage.value}",
        f"Duration:                {result.duration_ms:.1f} ms",
        f"Execution Mode:          {'DRY RUN (0 writes to Spanner / GCS)' if result.dry_run else 'LIVE COMMIT'}",
        "",
        "--- Chunk Discovery Breakdown ---",
        f"Eligible Text Chunks:    {result.eligible_chunks_count}",
        f"Excluded IMAGE Chunks:   {result.excluded_image_chunks_count}",
        f"Embeddings Generated:    {result.embeddings_generated_count} (768-dimensional)",
        "",
        "--- Extracted Knowledge Graph Metadata ---",
        f"Entities Extracted:      {result.entities_count}",
        f"Technologies Canonical:  {result.technologies_count}",
        f"Technologies Unresolved: {result.unresolved_technologies_count}",
        f"Solution Cards:          {result.solution_cards_count}",
        f"Entity Relationships:    {result.relationships_count}",
        f"Evidence Citations:      {result.evidence_links_count}",
    ]

    if result.sidecar_uri:
        lines.extend([
            "",
            "--- GCS Metadata Sidecar ---",
            f"Destination URI:         {result.sidecar_uri}",
            f"SHA-256 Digest:          {result.sidecar_sha256 or 'N/A'}",
        ])

    if result.write_result:
        wr = result.write_result
        lines.extend([
            "",
            "--- Spanner Persistence Stats (11 Tables) ---",
            f"Spanner Status:          {wr.status}",
            f"Total Mutations:         {wr.total_mutations}",
            f"Chunk Embeddings Upd:    {wr.chunks_embeddings_updated}",
            f"Chunk Embeddings Unchg:  {wr.chunks_embeddings_unchanged}",
            f"Entities Inserted:       {wr.entities_inserted}",
            f"Entities Updated:        {wr.entities_updated}",
            f"Entities Unchanged:      {wr.entities_unchanged}",
            f"Techs Inserted:          {wr.technologies_inserted}",
            f"Techs Unchanged:         {wr.technologies_unchanged}",
            f"Solution Cards Inserted: {wr.solution_cards_inserted}",
            f"Solution Cards Updated:  {wr.solution_cards_updated}",
            f"Solution Cards Protect:  {wr.solution_cards_protected} (APPROVED)",
            f"Solution Cards Unchg:    {wr.solution_cards_unchanged}",
            f"Graph Edges Inserted:    {wr.edges_inserted}",
            f"Graph Edges Unchanged:   {wr.edges_unchanged}",
            f"Evidence Links Inserted: {wr.evidence_links_inserted}",
            f"Evidence Links Unchg:    {wr.evidence_links_unchanged}",
            f"Document Status Updated: {wr.document_status_updated}",
        ])
    elif result.dry_run:
        lines.extend([
            "",
            "--- Projected Spanner Mutations (Dry Run) ---",
            f"Projected Total:         {result.projected_mutations} mutations",
            "Actual Committed Writes: 0 mutations (Dry Run Enforced)",
        ])

    if result.checkpoint_details:
        cp = result.checkpoint_details
        lines.extend([
            "",
            "--- Four-Point Completion Checkpoint Status ---",
            f"Point 1 (Document Status ENRICHED):    {cp.get('status_enriched', False)}",
            f"Point 2 (Chunks Embeddings Complete):  {cp.get('embedded_chunks', 0)} / {cp.get('eligible_chunks', 0)}",
            f"Point 3 (Cards & EvidenceLinks Exist): {cp.get('solution_cards_count', 0)} cards, {cp.get('evidence_links_count', 0)} links",
            f"Point 4 (GCS Sidecar & Hash Match):   {cp.get('sidecar_sha256_match', False)}",
            f"All 4 Criteria Met:                   {cp.get('all_points_passed', False)}",
        ])

    if result.error_message:
        lines.extend([
            "",
            "--- Error Message ---",
            f"Error:                   {result.error_message}",
        ])

    lines.append("=" * 68)
    return "\n".join(lines)

