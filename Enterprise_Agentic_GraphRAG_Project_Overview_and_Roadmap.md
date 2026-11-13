# Enterprise Agentic GraphRAG --- Project Overview & Roadmap

**Project:** Sales Solution Discovery MVP\
**Google Cloud project:** `xenon-broker-513704-s9`\
**Project number:** `291679685305` \| **Organization:** `815895343251`\
**Primary region:** `asia-south1` (Mumbai)\
**Spanner:** instance `sales-discovery-trial`, database
`sales-knowledge-db`\
**Last updated:** 13 November 2026

## 1. Executive Summary

This project is an enterprise Agentic GraphRAG solution for discovering
business solutions and technical evidence across PDFs and Google Slides,
with GitHub repository AST ingestion deferred as a future extension outside
current MVP scope.

The target solution has two independently deployable agents:

-   **Ingestion Copilot:** discovers, extracts, normalizes, enriches,
    reviews, and commits source knowledge into the graph.
-   **Discovery Copilot:** plans retrieval, combines vector search and
    graph traversal, validates evidence, and generates grounded answers
    with citations and source lineage.

The data foundation uses Google Cloud Spanner/Spanner Graph, Cloud
Storage, Vertex AI embeddings, Document AI, Google Slides API, Google ADK,
Gemini, Cloud Run, Pub/Sub, and the intended A2UI/Gemini Enterprise experience
(GitHub repository AST extraction deferred as a future extension).

**Current milestone:** Phases 0, 1, 2, 3A (PDF Ingestion), and 3B (Google Slides
Ingestion) are complete and verified. Phase 3C (Metadata, Taxonomy, and
Embeddings) is next.

## 2. Business Goal and Target Capabilities

The MVP aims to make enterprise knowledge discoverable across enterprise documents
(PDFs) and presentations (Google Slides), while preserving full schema and graph
scaffolding for future source-code repository ingestion.

Target capabilities:

-   Ingest PDFs and Google Slides presentations (GitHub repository AST ingestion deferred as a future extension).
-   Extract text, tables, layout, page/slide metadata, images, and visual assets.
-   Normalize content into common document, chunk, entity, technology,
    and solution metadata.
-   Generate 768-dimensional embeddings and support vector retrieval.
-   Maintain a unified Spanner Property Graph (preserving all repository and code node/edge tables).
-   Combine vector similarity and graph traversal for hybrid retrieval.
-   Enforce asset/user permissions before evidence enters LLM context.
-   Apply deterministic Maximal Marginal Relevance (MMR).
-   Validate evidence, citations, and source lineage.
-   Return grounded answers, business solution cards, and relevant
    visual assets.
-   Preserve source provenance and `.meta.json` sidecars in Cloud
    Storage.
-   Support human review before approved graph commits.

## 3. Approved Architecture

### 3.1 Ingestion Copilot

A separate ADK Python application, designed for asynchronous ingestion.

Pipeline:

1.  Discover source files and ingestion events.
2.  Route each source to the appropriate extraction worker.
3.  Extract content and source metadata.
4.  Normalize and chunk content.
5.  Extract entities, relationships, technologies, and solution
    metadata.
6.  Generate embeddings.
7.  Present metadata for human review where required.
8.  Apply deterministic taxonomy mapping and schema validation.
9.  Commit approved graph nodes and edges.
10. Store originals and `.meta.json` sidecars in GCS.

Cloud Run and Pub/Sub are part of the approved ingestion execution
design.

### 3.2 Discovery Copilot

A separate ADK Python application intended for interactive question
answering.

Pipeline:

1.  Interpret the question and plan retrieval.
2.  Decompose complex questions where needed.
3.  Apply permissions before loading evidence into model context.
4.  Run vector retrieval and graph traversal.
5.  Merge and deduplicate results.
6.  Apply deterministic MMR reranking.
7.  Validate evidence, citations, and lineage.
8.  Use Gemini to synthesize a grounded answer.
9.  Return solution cards, visual assets, citations, and lineage.

Discovery is designed for read-only access to the knowledge graph and
source artifacts.

### 3.3 Shared Components

The applications share common contracts and libraries for configuration,
data models, Spanner access, graph queries, RBAC, embeddings, retrieval,
MMR, Cloud Storage, metadata sidecars, and migration/audit lifecycle
handling.

### 3.4 Technology Stack

  -----------------------------------------------------------------------
  Area                                Technology
  ----------------------------------- -----------------------------------
  Agent framework                     Google ADK, Python

  Scaffolding                         Google Agents CLI

  LLM                                 Gemini

  Discovery runtime                   Vertex AI Agent Runtime

  Ingestion execution                 Cloud Run, Pub/Sub

  Graph and relational store          Cloud Spanner / Spanner Graph

  Vector retrieval                    Spanner vector index

  Embeddings                          Vertex AI Embeddings

  PDF extraction                      Document AI Layout Parser

  Slides extraction                   Google Slides API

  Code extraction                     GitHub repository APIs/tree and
                                      language-aware AST extraction
                                      (Deferred future extension; Spanner
                                      schemas and scaffolding preserved)

  Object storage                      Google Cloud Storage

  Container registry                  Artifact Registry

  UI target                           A2UI and Gemini Enterprise
                                      experience

  Evaluation/observability            Automated tests, evaluation
                                      datasets, logs, metrics, and audit
                                      evidence
  -----------------------------------------------------------------------

## 4. Google Cloud Environment

### 4.1 Project

-   Project ID: `xenon-broker-513704-s9`
-   Project name: `Sales Solution Discovery MVP`
-   Project number: `291679685305`
-   Organization ID: `815895343251`
-   Primary region: `asia-south1` (Mumbai)

### 4.2 Resource Inventory Recorded During Phase 2B

  -----------------------------------------------------------------------------------
  Resource                Name / identifier                   Recorded state
  ----------------------- ----------------------------------- -----------------------
  Spanner instance        `sales-discovery-trial`             Existing, Mumbai,
                                                              FREE_INSTANCE, 100
                                                              processing units,
                                                              Enterprise edition

  Spanner database        `sales-knowledge-db`                Existing GoogleSQL
                                                              database; migrated in
                                                              Phase 2C

  Other Spanner database  `cymbal`                            Existing; not modified

  Existing GCS bucket     `gs://sales-discovery-mvp`          Existing US
                                                              multi-region bucket;
                                                              preserved

  New GCS bucket          `gs://sales-discovery-mvp-mumbai`   Created in Mumbai

  Artifact Registry       `enterprise-graphrag`               Created in Mumbai,
                                                              Docker repository

  Pub/Sub topic           `ingestion-events`                  Created; message
                                                              persistence in
                                                              `asia-south1`; no
                                                              subscription created in
                                                              Phase 2B

  Document AI processor   `d79bb791cfba8a30`                  Layout Parser created
                                                              in `us`

  Secret Manager secret   `github-access-token`               Secret container
                                                              created in Mumbai; no
                                                              version recorded in
                                                              Phase 2B

  Service account         `ingestion-copilot-sa`              Created

  Service account         `discovery-copilot-sa`              Created

  Service account         `pubsub-invoker-sa`                 Created

  Custom IAM role         `spannerGraphDataReadWriter`        DML-oriented
                                                              permissions; no
                                                              `updateDdl` permission
  -----------------------------------------------------------------------------------

**Regional decision:** Document AI Layout Parser was not available in
Mumbai during the infrastructure assessment. The approved choice was to
retain Layout Parser and process through the US region, while keeping
the primary Spanner and new GCS data plane in Mumbai.

**Resource protection:** Do not delete or repurpose the existing
`cymbal` database or existing US GCS bucket. The existing bucket had
uniform bucket-level access, public access prevention, and a retention
lock recorded through February 2027.

**Do not assume:** Cloud Run services, deployed agents, Gemini
Enterprise registration, active ingestion workers, a Pub/Sub
subscription, or a GitHub token version exist merely because related
infrastructure was prepared.

## 5. Repository and Local Development

Repository path:

`/home/naveen/enterprise-graphrag`

Windows WSL path:

`\\wsl.localhost\Ubuntu\home\naveen\enterprise-graphrag`

Monorepo structure:

``` text
enterprise-graphrag/
├── .agents-cli-spec.md
├── pyproject.toml
├── common/
│   └── src/common/
│       ├── config.py
│       ├── models/schema.py
│       ├── database/spanner.py
│       ├── rbac/rbac.py
│       ├── search/mmr.py
│       └── storage/resolver.py
├── ingestion-copilot/
├── discovery-copilot/
├── deployment/terraform/
└── tests/
```

Two independent ADK Python applications were scaffolded using Google
Agents CLI v1.13.0:

-   `ingestion-copilot`: Cloud Run deployment target.
-   `discovery-copilot`: Agent Runtime deployment target.

Shared modules and project scaffolding exist, but their presence does
not mean all planned business functionality is implemented.

Recorded local environment: WSL2 Ubuntu, Python 3.12.3 (some
tool-managed runs reported 3.13.18), Node.js 22.23.4, npm 11.6.3, uv
0.13.7, Git 2.43.0, Google Cloud SDK 592.0.0, Google Agents CLI 1.13.0.

## 6. Work Completed

### Phase 0 --- Initialization and Configuration

**Status: Complete**

-   Initialized the monorepo and project specification.
-   Established the GCP project and Mumbai as primary region.
-   Prepared the initial project configuration and scaffolding
    requirements.

### Phase 1 --- Architecture and Technical Validation

**Status: Complete; six sub-phases accepted**

  Sub-phase   Work
  ----------- --------------------------------------------
  1A          Architecture and Google Cloud recipe study
  1B          Infrastructure assessment
  1C          GraphRAG schema design
  1D          Architecture corrections
  1E          Safety review
  1F          Executability and concurrency validation

Report: `PHASE_1F_EXECUTABILITY_REPORT.md`

Key Phase 1F decisions:

-   Migration lifecycle: preflight, schema migration, validation,
    acceptance, and ingestion activation.
-   Acceptance and rollback compete on the same
    `MigrationBatches(BatchId)` record in a serializable read-write
    transaction.
-   Only one terminal transition can win; retries inspect the terminal
    state.
-   Rollback after acceptance is prohibited.
-   Ingestion workers remain paused until acceptance is committed.
-   DDL rollback requires explicit inverse DDL; backfill rollback is
    separate transactional DML.

### Phase 2A --- Local Scaffolding

**Status: Complete and accepted**

-   Initialized Git monorepo on `main`; initial commit recorded as
    `83a14f0`.
-   Created root workspace linking `common`, `ingestion-copilot`, and
    `discovery-copilot`.
-   Scaffolded both ADK Python applications.
-   Created shared configuration, schema, Spanner, RBAC, MMR, and
    storage modules.
-   Added project guidance, READMEs, `.gitignore`, and tests.
-   Synchronized workspace dependencies.

Report: `PHASE_2A_FINAL_VERIFICATION.md`

Reported local checks: both agents imported/initialized; ingestion
FastAPI startup and `/docs` returned HTTP 200; Discovery Agent Runtime
adapter initialized; shared imports passed; `.env` was not tracked; Git
history secret scan was clean; Ruff passed; 14 tests passed.

These were local scaffold/smoke tests, not proof of deployed Gemini
interactions or live runtime behavior.

### Phase 2B --- GCP Infrastructure

**Status: Complete and accepted**

The read-only audit recorded the original database baseline:

  Existing table         Baseline rows
  -------------------- ---------------
  Documents                          5
  DocumentAccess                    10
  DriveSyncDocuments                 5
  EvidenceChunks                   195
  EvidenceLinks                      0
  SolutionCards                      5
  VisualAssets                     125
  Embeddings                        81
  **Total**                    **426**

Other baseline facts: 4 PDFs, 1 slide deck, 195 chunks, 5 solution
cards, 125 visual assets, and 81 legacy embeddings with 384 dimensions.
The source contents were not fully audited; do not assume this is a
complete production corpus.

Provisioning included required API enablement, service accounts and IAM,
custom Spanner role, new Mumbai GCS bucket and prefixes, Artifact
Registry, US Document AI Layout Parser processor, Mumbai Pub/Sub topic,
and Secret Manager secret container.

The final Phase 2B audit reported 14/14 checks passed with no
discrepancies.

Report: `PHASE_2B_1_INFRASTRUCTURE_AUDIT.md`

### Phase 2C --- Spanner Schema Migration and Graph Backfill

**Status: COMPLETE, VALIDATED AND ACCEPTED**

Target database: `sales-knowledge-db`\
Instance: `sales-discovery-trial`\
Property Graph: `EnterpriseKnowledgeGraph`

#### 2C.1 --- Migration Preflight

Passed. Inspected live schema and row counts, prepared six DDL batches,
identified 325 expected relationships, and confirmed the required
nullability change to `Documents.DriveFileId`. User explicitly
authorized DDL execution without a live backup because the existing
content was considered sample/disposable.

#### 2C.2 --- Six DDL Batches

All six batches passed:

  ---------------------------------------------------------------------------------------
  Batch                               Work
  ----------------------------------- ---------------------------------------------------
  1                                   Created `MigrationBatches` and `MigrationManifest`
                                      audit tables

  2                                   Added
                                      repository/file/commit/line-range/768-dimensional
                                      embedding/asset-type columns and relaxed
                                      `Documents.DriveFileId` nullability

  3                                   Created four graph node tables

  4                                   Created eleven graph edge tables and foreign-key
                                      constraints

  5                                   Created `EvidenceChunkVectorIndex`

  6                                   Created `EnterpriseKnowledgeGraph`
  ---------------------------------------------------------------------------------------

Vector index definition:

``` sql
CREATE VECTOR INDEX EvidenceChunkVectorIndex
ON EvidenceChunks(Embedding768)
STORING (DocumentId, FileId, PageNumber, SlideNumber)
WHERE Embedding768 IS NOT NULL
OPTIONS (distance_type = 'COSINE');
```

Index state was verified as `READ_WRITE`.

**Embedding distinction:** The new `Embedding768` column and vector
index were created, but the migration did not generate or convert
embeddings. The legacy `Embeddings` table contains 81 384-dimensional
embeddings. Actual 768-dimensional embedding generation and population
remain future implementation work.

#### 2C.3 --- Graph Structure Verification

Passed 5/5 checks:

-   All 8 node tables and 11 edge tables registered.
-   All 11 edge definitions use `NO PROPERTIES`.
-   Node and edge label traversals compiled.
-   Vector index remained `READ_WRITE`.
-   Original 426 records remained intact.

#### 2C.4 --- Property Graph Functional Validation

Passed 5/5 checks:

-   All 8 node labels and 8 distinct edge labels.
-   All 11 source-to-destination edge patterns.
-   Two-hop and branching traversals.
-   Scalar and array parameter filters, including RBAC-style
    `IN UNNEST(@authorized_doc_ids)`.
-   Graph properties aligned with underlying relational tables.

GQL syntax rules discovered:

1.  Use distinct variable names for graph nodes and edges.
2.  Give every `RETURN` expression an explicit alias.
3.  Escape reserved edge labels such as `` `CONTAINS` `` and
    `` `REFERENCES` `` with backticks.

#### 2C.5 --- Relationship Backfill

Committed successfully in one serializable read-write transaction.

Batch ID: `BATCH_BACKFILL_20261111_01`

  Edge table                  Inserted
  ------------------------- ----------
  DocumentContainsChunk            195
  VisualAssetBelongsToDoc          125
  SolutionImplementsDoc              5
  **Total**                    **325**

The transaction also created 325 `MigrationManifest` entries and one
batch audit record. No overwrites, deletions, duplicate keys, or orphan
references were reported. Original 426 records were preserved. The batch
remained `PENDING_VALIDATION` pending the acceptance gate.

#### 2C.6 --- End-to-End Validation

Passed 7/7 checks:

-   Exact edge counts.
-   One-to-one manifest mapping.
-   GQL single-hop traversals.
-   Multi-hop traversal and SQL consistency.
-   Referential integrity and duplicate checks.
-   Original table counts and vector-index readiness.
-   Migration lifecycle and rollback guard conditions.

Representative multi-hop GQL/SQL result comparisons matched:

-   Business Solution → Document → Chunk: 195.
-   Visual Asset → Document → Chunk: 6,669.
-   Business Solution → Document ← Visual Asset: 125.

#### Phase 2C Acceptance Gate

**COMMITTED AND ACCEPTED**

The Phase 1F `accept_migration_batch` lifecycle was executed for
`BATCH_BACKFILL_20261111_01`.

-   Previous state: `PENDING_VALIDATION`
-   Final state: `ACCEPTED` (terminal)
-   CreatedAt: `2026-11-11 12:11:57.046722+00:00`
-   AcceptedAt: `2026-11-11 12:19:19.882700+00:00`
-   Total inserted edges: 325

A rollback attempt against the accepted batch was rejected with
`RollbackRejectedError`. No rows were modified or deleted by that
rejected attempt.

### Phase 3A --- PDF / Document AI Ingestion

**Status: Complete and accepted**

-   Implemented Document AI Layout Parser extraction pipeline in `common/parsers`.
-   Normalized PDF pages, text blocks, tables, and bounding box layout into common models.
-   Uploaded page renders and visual assets to Mumbai bucket `sales-discovery-mvp-mumbai`.
-   Atomically persisted records across `Documents`, `EvidenceChunks`, `VisualAssets`, `DocumentContainsChunk`, and `VisualAssetBelongsToDoc`.
-   Verified live preflight, dry run, live ingestion execution, and live idempotency against approved PDF fixtures and sample documents.

### Phase 3B --- Google Slides Ingestion

**Status: Complete and accepted**

-   Implemented `GoogleSlidesClient` with Application Default Credentials impersonation (`ingestion-copilot-sa`).
-   Implemented `slides_extractor` for slide structure, titles, shapes, text, tables, speaker notes, and embedded images.
-   Implemented `VisualAssetUploader` with multi-asset transactional compensation and rollback.
-   Implemented `GoogleSlidesOrchestrator` and manual CLI `scripts/ingest_slides_manual.py`.
-   Conducted live read preflight, live read verification, dry run (31 projected mutations), live ingestion execution, and live idempotency rerun.
-   Ingested presentation `1oOHTB9O5s8nqMhgYEgp8inq2wqZSY1S9ysYgTn07-BQ` (4 slides, 10 chunks, 5 visual assets [4 renders + 1 embedded diagram], 31 Spanner mutations, 5 GCS objects).
-   Verified live idempotency: rerun reported 0 Spanner mutations and skipped all 5 GCS uploads with matching SHA-256 hashes.

## 7. Current Spanner Graph Foundation

### 7.1 Node Tables and Current Counts

  Node label         Table              Current nodes
  ------------------ ---------------- ---------------
  Document           Documents                      6
  Chunk              EvidenceChunks               205
  BusinessSolution   SolutionCards                  5
  VisualAsset        VisualAssets                 130
  Repository         Repositories                   0
  SourceFile         SourceFiles                    0
  Entity             Entities                       0
  Technology         Technologies                   0
  **Total**                                   **346**

### 7.2 Edge Tables and Current Counts

  Edge label   Edge table                  Current edges
  ------------ ------------------------- ---------------
  CONTAINS     DocumentContainsChunk                 205
  CONTAINS     RepoContainsFile                        0
  CONTAINS     SourceFileContainsChunk                 0
  REFERENCES   ChunkReferencesEntity                   0
  REFERENCES   EntityReferencesEntity                  0
  MENTIONS     ChunkMentionsTech                       0
  BELONGS_TO   VisualAssetBelongsToDoc               130
  SUPPORTS     SolutionSupportsTech                    0
  DEFINES      SourceFileDefinesEntity                 0
  CALLS        SourceFileCallsFile                     0
  IMPLEMENTS   SolutionImplementsDoc                   5
  **Total**                                      **340**

The graph structure is live and validated for all currently populated
records. All repository-related tables (`Repositories`, `SourceFiles`) and
edge tables (`RepoContainsFile`, `SourceFileDefinesEntity`, `SourceFileCallsFile`)
are fully preserved in the property graph schema and database for future AST
ingestion extension; they are not deleted or modified. Entity and technology
nodes and their related edges are scheduled for population in Phase 3C.

## 8. Approved Graph Data Model

### Node tables

1.  `Documents`
2.  `EvidenceChunks`
3.  `SolutionCards`
4.  `VisualAssets`
5.  `Repositories`
6.  `SourceFiles`
7.  `Entities`
8.  `Technologies`

### Edge tables

1.  `DocumentContainsChunk`
2.  `RepoContainsFile`
3.  `SourceFileContainsChunk`
4.  `ChunkReferencesEntity`
5.  `EntityReferencesEntity`
6.  `ChunkMentionsTech`
7.  `VisualAssetBelongsToDoc`
8.  `SolutionSupportsTech`
9.  `SourceFileDefinesEntity`
10. `SourceFileCallsFile`
11. `SolutionImplementsDoc`

The graph uses shared labels such as `CONTAINS` and `REFERENCES`. All
edge definitions use `NO PROPERTIES` to keep property definitions
consistent for shared labels.

## 9. Future Implementation Roadmap

### Phase 3 --- Ingestion Copilot Development (4 MVP sub-phases + Deferred Extension)

**3A --- PDF / Document AI ingestion [COMPLETE]**

-   Discover PDFs in approved GCS locations.
-   Process PDFs using the Document AI Layout Parser processor in the
    approved US region.
-   Extract text, tables, layout, page numbers, and source metadata.
-   Normalize output into the common document/chunk model.
-   Preserve source identifiers, GCS URIs, processor metadata, and
    provenance.
-   Implement retries, idempotency, failure handling, and representative
    tests.
-   *Status:* Completed, tested, and validated end-to-end with live idempotency.

**3B --- Google Slides ingestion [COMPLETE]**

-   Discover and read Google Slides presentations via Google Slides API v1.
-   Extract presentation, slide, text, shape, table, speaker notes, and image metadata.
-   Preserve slide numbers and visual asset references.
-   Normalize slide content into the common schema.
-   Upload slide renders and embedded images to Mumbai Cloud Storage bucket.
-   Persist presentation nodes, chunks, and visual assets atomically to Cloud Spanner.
-   Implement failure compensation rollback, dry-run mode, and deterministic idempotency.
-   *Status:* Completed, tested, and validated end-to-end with live idempotency.

**3C --- Metadata, taxonomy, and embeddings [NEXT]** *(Renumbered from previous 3D)*

-   Normalize and chunk content across ingested PDFs and Google Slides.
-   Extract entities, technologies, business problems, proposed
    solutions, and outcomes.
-   Apply deterministic taxonomy mapping against approved enterprise schemas.
-   Generate embeddings using Vertex AI Embeddings (768 dimensions).
-   Populate `EvidenceChunks.Embedding768`.
-   Validate embedding dimensions and vector-index compatibility (`EvidenceChunkVectorIndex`).
-   Populate `Entities` and `Technologies` nodes and their relationships (`ChunkReferencesEntity`, `ChunkMentionsTech`, `SolutionSupportsTech`).
-   Separate model-based extraction from deterministic validation.

**3D --- Human review and graph commit [PLANNED]** *(Renumbered from previous 3E)*

-   Implement the intended A2UI metadata review experience.
-   Show extracted metadata and source evidence.
-   Support approval/rejection before graph commit.
-   Validate approved records against the common schema.
-   Commit graph nodes and edges with provenance.
-   Store source originals and `.meta.json` sidecars in GCS.
-   Implement audit, idempotency, and recovery.
-   Keep ingestion activation controlled until tested and approved.

**Future Extension (Deferred): GitHub repository AST ingestion**

-   *Deferred outside current MVP scope.*
-   All repository-related Spanner tables (`Repositories`, `SourceFiles`), graph edge tables (`RepoContainsFile`, `SourceFileDefinesEntity`, `SourceFileCallsFile`), property graph schema definitions, and codebase scaffolding are fully preserved without modification.
-   When scheduled as a future extension: discover approved repositories and revisions, read repository trees, extract README/documentation, perform language-aware AST extraction for functions/classes/symbols/dependencies, and populate repository nodes and edges.

### Phase 4 --- Discovery Copilot Development (5 sub-phases)

**4A --- ADK planner** - Implement the Discovery Copilot planner using
ADK. - Interpret questions and identify retrieval intent. - Decompose
multi-part questions. - Select retrieval tools. - Keep planning separate
from deterministic retrieval and validation.

**4B --- Spanner Graph query tools** - Implement reusable graph query
tools. - Support node, edge, and multi-hop traversal. - Use
parameterized queries and validated GQL syntax. - Return structured
evidence, identifiers, and lineage. - Apply permission-aware filters
before exposing data.

**4C --- Hybrid vector and graph retrieval** - Implement vector
retrieval using the 768-dimensional index. - Implement graph traversal
and context expansion. - Merge and deduplicate results. - Preserve
source metadata and provenance. - Apply RBAC before evidence enters LLM
context.

**4D --- MMR, evidence validation, and citations** - Implement
deterministic MMR. - Balance relevance and diversity. - Validate
evidence references and lineage. - Reject or flag unsupported
citations. - Ensure answers are traceable to retrieved sources.

MMR objective:

``` text
MMR = argmax(d in R \ S) [
    lambda * Rel(d, q)
    - (1 - lambda) * max(Sim(d, s) for s in S)
]
```

Where `R` is the candidate set, `S` is the selected set, `Rel` is query
relevance, `Sim` is similarity to selected results, and `lambda`
controls the relevance/diversity trade-off.

**4E --- Gemini synthesis and A2UI solution cards** - Generate grounded
answers using Gemini. - Include citations and evidence references. -
Return relevant business solution cards. - Include visual assets and
source lineage. - Implement the intended interactive A2UI
presentation. - Evaluate unsupported-claim behavior.

### Phase 5 --- Integration, Security, and Evaluation (4 sub-phases)

**5A --- Agent-to-agent integration** - Integrate ingestion and
discovery through the approved agent communication pattern. - Validate
contracts, errors, timeouts, and handoffs. - Preserve separate agent
responsibilities and access boundaries.

**5B --- RBAC and identity** - Implement user- and asset-level
authorization. - Enforce permissions before retrieval content reaches
the LLM. - Validate read/write isolation. - Verify least-privilege
service identities. - Add authorization audit evidence.

**5C --- Evaluation and golden datasets** - Prepare representative
questions and expected evidence. - Evaluate retrieval relevance and
coverage. - Evaluate citation correctness and answer grounding. -
Evaluate graph and vector retrieval. - Track regressions.

**5D --- E2E tests, observability, audit, and recovery** - Test complete
ingestion-to-discovery workflows. - Add structured logs, metrics,
tracing, and audit records. - Test retries, failures, and recovery. -
Validate data integrity and idempotency. - Test authorization
boundaries. - Establish operational readiness checks.

### Phase 6 --- Deployment and Gemini Enterprise Launch (3 sub-phases)

**6A --- Deploy Cloud Run and Agent Runtime** - Deploy ingestion
services to Cloud Run. - Deploy Discovery Copilot to the approved Agent
Runtime target. - Configure runtime identities, permissions, environment
variables, secrets, and integrations. - Verify health, logs, and runtime
behavior.

**6B --- Agent Card validation and Gemini Enterprise registration** -
Validate agent metadata and Agent Cards. - Verify integration and
identity prerequisites. - Register agents with the intended Gemini
Enterprise experience. - Validate user interaction and access controls.

**6C --- E2E acceptance, demo, and handover** - Run complete ingestion
and discovery demonstrations. - Validate golden use cases. - Confirm
security, citations, lineage, and operations. - Document deployment,
support, and recovery. - Complete team handover and MVP acceptance.

## 10. Roadmap at a Glance

  ------------------------------------------------------------------------
  Phase            Description                 Sub-phases Status
  ---------------- ---------------- --------------------- ----------------
  0                Initialization                       1 Complete
                   and                                    
                   configuration                          

  1                Architecture and                     6 Complete
                   technical                              
                   validation                             

  2A               Local                                1 Complete
                   scaffolding                            

  2B               GCP                                  1 Complete
                   infrastructure                         

  2C               Spanner                              6 Complete and
                   migration and                          accepted
                   graph backfill                         

  3                Ingestion                            4 In Progress
                   Copilot                                (3A, 3B Complete;
                   (MVP)                                  3C Next; 3D Planned)
                   [GitHub AST deferred]

  4                Discovery                            5 Planned
                   Copilot                                

  5                Integration,                         4 Planned
                   security, and                          
                   evaluation                             

  6                Deployment and                       3 Planned
                   Gemini                                 
                   Enterprise                             
                   launch                                 
  ------------------------------------------------------------------------

The original delivery target was an accelerated MVP. This roadmap
preserves the approved functional scope; actual dates and remaining
effort should be updated by the team as implementation progresses.

## 11. Engineering Principles and Non-Negotiable Rules

1.  Preserve the approved scope. Do not silently remove source types,
    retrieval capabilities, security, human review, citations, or
    lineage.
2.  Work one sub-phase at a time. Implement, test, report, and review
    before proceeding.
3.  Separate LLM planning from deterministic schema, authorization,
    integrity, and evidence checks.
4.  Enforce RBAC before retrieved content is supplied to the LLM.
5.  Preserve source provenance for every ingested record and retrieved
    evidence item.
6.  Make ingestion and migration idempotent; retries must not create
    duplicates.
7.  Treat the accepted Phase 2C batch as terminal; automated rollback is
    prohibited.
8.  Keep ingestion workers paused until activation is explicitly
    approved.
9.  Do not modify unrelated existing resources, especially the `cymbal`
    database and existing US GCS bucket.
10. Do not assume an index means embeddings exist; 768-dimensional
    embeddings still need to be generated and populated.
11. Use least-privilege identities: ingestion has only required write
    access; discovery remains read-only.
12. Verify cloud changes by inspecting resulting resource state and
    data; command success alone is not acceptance.
13. Distinguish local tests from deployed tests.
14. Keep implementation reports concise: changes, tests, cloud
    mutations, failures, and blockers.

## 12. Current State and Immediate Next Step

### Confirmed current milestone

-   Phase 0 complete.
-   Phase 1 complete.
-   Phase 2A complete.
-   Phase 2B complete.
-   Phase 2C complete and formally accepted (`EnterpriseKnowledgeGraph` live with 330 baseline nodes and 325 baseline relationships).
-   `EvidenceChunkVectorIndex` is `READ_WRITE`.
-   Original 426 legacy records were preserved during migration.
-   Batch `BATCH_BACKFILL_20261111_01` is terminal `ACCEPTED`.
-   Phase 3A (PDF / Document AI Ingestion) complete and verified with live idempotency.
-   Phase 3B (Google Slides Ingestion) complete and verified with live idempotency (presentation `1oOHTB9O5s8nqMhgYEgp8inq2wqZSY1S9ysYgTn07-BQ`, 10 Chunks, 5 VisualAssets, 15 Edges, 31 Spanner mutations, 5 GCS assets).
-   Current live database contains 6 Documents, 205 EvidenceChunks, 130 VisualAssets, 5 SolutionCards, and associated graph edges (346 total nodes, 340 total edges).
-   GitHub repository AST ingestion deferred as a future extension outside current MVP scope; all repository Spanner tables, edges, schemas, and scaffolding preserved.

### Immediate next step: Phase 3C --- Metadata, Taxonomy, and Embeddings

Proceed to Phase 3C (renumbered from 3D):
-   Normalize and chunk content across ingested PDFs and Google Slides presentations.
-   Extract business and technical entities, technologies, business problems, proposed solutions, and outcomes.
-   Apply deterministic taxonomy mapping against approved enterprise schemas.
-   Generate 768-dimensional embeddings via Vertex AI Embeddings and populate `EvidenceChunks.Embedding768`.
-   Validate compatibility with `EvidenceChunkVectorIndex`.
-   Populate `Entities` and `Technologies` nodes along with their graph relationships (`ChunkReferencesEntity`, `ChunkMentionsTech`, `SolutionSupportsTech`).

## 13. Open Items --- Do Not Mistake These for Completed Functionality

-   PDF (Phase 3A) and Google Slides (Phase 3B) ingestion pipelines are fully implemented and verified. GitHub repository AST ingestion is deferred as a future extension outside current MVP scope.
-   The vector index exists, but the migration did not populate the new 768-dimensional embedding field; 768-dimensional embeddings will be generated in Phase 3C.
-   The 81 legacy embeddings are 384-dimensional and were preserved; they were not converted to 768 dimensions.
-   Repository and source-file node tables remain empty and preserved for future extension; Entity and Technology node tables will be populated in Phase 3C.
-   Edge tables for entities and technologies will be populated in Phase 3C; code-related edge tables remain preserved for future extension.
-   The Pub/Sub topic exists, but Phase 2B did not create its subscription.
-   The Secret Manager container exists, but Phase 2B recorded no secret version.
-   Cloud Run deployment, Agent Runtime deployment, A2A integration, end-to-end RBAC, Gemini Enterprise registration, and production launch remain future work (Phases 5 and 6).
-   A2UI review and solution-card experience are part of the approved target architecture (Phase 3D and Phase 4E).

------------------------------------------------------------------------

**Document purpose:** Provide engineering, product, and delivery teams
with a shared understanding of the project objective, approved
architecture, completed work, live data foundation, implementation
boundaries, and remaining roadmap.
