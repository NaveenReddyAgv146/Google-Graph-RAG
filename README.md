# Enterprise Agentic GraphRAG

Enterprise-grade Agentic GraphRAG solution ingesting internal business knowledge from PDFs, Google Slides, and GitHub repositories into a connected Google Cloud Spanner property graph, enabling natural-language solution discovery with verifiable provenance, line-level code citations, and visual assets.

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
   - **Role:** Asynchronous document ingestion (PDF Document AI layout parsing, Google Slides text/image extraction, GitHub AST analysis), chunking, 768-dim vector embeddings, and Spanner Graph commits.
   - **Trigger:** Cloud Storage & GitHub webhooks via authenticated Pub/Sub.

2. **`discovery-copilot`**:
   - **Deployment Target:** Google Agent Runtime (`asia-south1`)
   - **Template:** Google ADK Python (`adk_python`)
   - **Role:** Multi-turn conversational planner coordinating graph traversal specialists, Spanner vector search, deterministic MMR reranking, RBAC filtering, and grounded citation synthesis.
   - **UI Interface:** A2UI declarative cards & A2A protocol.

3. **`common`**:
   - **Role:** Shared knowledge plane, Spanner Graph ISO GQL schema definitions, Pydantic node/edge models, dual-bucket storage resolver, and deterministic MMR reranking engine.

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

