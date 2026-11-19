# Enterprise Agentic GraphRAG

Enterprise-grade Agentic GraphRAG solution ingesting internal business knowledge from PDFs and Google Slides (with GitHub repository AST ingestion deferred as a future extension outside current MVP scope) into a connected Google Cloud Spanner property graph, enabling natural-language solution discovery with verifiable provenance, line-level code citations, and visual assets.

---

## Architecture & Monorepo Topology

```
enterprise-graphrag/
├── common/                # Shared Knowledge Plane, Graph Models, MMR & RBAC
├── ingestion-copilot/     # Cloud Run Ambient Ingestion Copilot (Pub/Sub + HITL)
├── discovery-copilot/     # Agent Runtime Conversational Discovery Copilot (GQL + Vector)
├── .agents-cli-spec.md    # Approved Phase 0 Architecture Specification
└── pyproject.toml         # Root uv workspace configuration
```

### Components

1. **`ingestion-copilot`**:
   - **Deployment Target:** Cloud Run (`asia-south1`)
   - **Template:** Google ADK Python (`adk_python`)
   - **Role:** Asynchronous document ingestion (PDF Document AI layout parsing, Google Slides text/image extraction; GitHub AST analysis deferred as a future extension), chunking, 768-dim vector embeddings, and Spanner Graph commits.
   - **Trigger:** Cloud Storage & Google Slides events via authenticated Pub/Sub.

2. **`discovery-copilot`**:
   - **Deployment Target:** Google Agent Runtime (`asia-south1`)
   - **Template:** Google ADK Python (`adk_python`)
   - **Role:** Multi-turn conversational planner coordinating graph traversal specialists, Spanner vector search, deterministic MMR reranking, RBAC filtering, and grounded citation synthesis.
   - **UI Interface:** A2UI declarative cards & A2A protocol.

3. **`common`**:
   - **Role:** Shared knowledge plane, Spanner Graph ISO GQL schema definitions (preserving all Repository and code node/edge tables for future extension), Pydantic node/edge models, dual-bucket storage resolver, and deterministic MMR reranking engine.

---

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (`>=0.10.0`)
- [Google Agents CLI](https://github.com/google/agents-cli) (`v1.13.0+`)
- Google Cloud SDK (`gcloud`)

---

## Development Quickstart

```bash
# Clone and enter workspace
cd enterprise-graphrag

# Sync all workspace dependencies
uv sync

# Validate ingestion-copilot
cd ingestion-copilot
uv run pytest tests/unit

# Validate discovery-copilot
cd ../discovery-copilot
uv run pytest tests/unit
```

---

## Manual Ingestion Test Runners

### PDF Ingestion Runner (Phase 3A)

To manually ingest a single PDF from Cloud Storage through the Document AI layout parser, chunk normalizer, and transactional Spanner writer:

```bash
# Ingest single PDF with live Spanner persistence
uv run python scripts/ingest_pdf_manual.py gs://sales-discovery-mvp-mumbai/whitepapers/sample.pdf

# Dry run (executes download, Document AI layout parsing, chunk normalization; skips Spanner writes)
uv run python scripts/ingest_pdf_manual.py gs://sales-discovery-mvp-mumbai/whitepapers/sample.pdf --dry-run
```

### Google Slides Ingestion Runner (Phase 3B)

To manually ingest a Google Slides presentation through Google Slides API fetch, content extraction, GCS visual asset storage, and transactional Spanner writer:

```bash
# Ingest single presentation with live Spanner persistence and GCS visual assets
uv run python scripts/ingest_slides_manual.py <presentation_id_or_url>

# Dry run (executes presentation fetch, extraction, and normalization; skips GCS uploads and Spanner writes)
uv run python scripts/ingest_slides_manual.py <presentation_id_or_url> --dry-run
```

